"""T-010 Credit Overdue Projection tests (T010-*). PostgreSQL only.

OVERDUE != DELINQUENCY CHARGE. An obligation is overdue when ``business_date > effective due_date`` AND its NET outstanding
is > 0 (business date in the FROZEN contract timezone). The loan status is the single projection paid > past_due > active.
No late fee, accrual, scheduler, batch, cash or accounting exists here.
"""

import re
import threading
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import event, text

from app.core.db import SessionLocal, engine
from app.modules.loans import allocation, ledger
from app.modules.loans import overdue as overdue_service
from app.modules.loans import payments as pay_service
from app.modules.loans import reversals as rev_service
from app.modules.loans import service as loan_service
from tests import pg_env  # noqa: F401  (must precede app imports)
from tests.test_t001_foundation import _alembic, scratch_db  # noqa: F401
from tests.test_t002_identity import (  # noqa: F401  (fixtures + helpers shared with the earlier suites)
    V2,
    admin_headers,
    client,
    create_role,
    fresh_db,
    h,
    login,
    sink,
    tenant_a,
    tenant_b,
)
from tests.test_t003_organization import mk_branch
from tests.test_t004_customers import ident
from tests.test_t004_customers import mk as mk_customer
from tests.test_t005_credit_products import P as PRODUCTS
from tests.test_t005_credit_products import flow
from tests.test_t005_engine import rules
from tests.test_t006_origination import (  # noqa: F401
    OPEN_POLICY,
    POLICY,
    _release_tracked_connections,
    approve,
    count,
    in_thread,
    post,
    review,
    tracked_connect,
    user_hdr,
    wait_blocked_n,
)
from tests.test_t007_disbursement import cash_for, disburse
from tests.test_t008_payments import LEGACY, LOAN_LOCK, balances, pay, schedule
from tests.test_t009_payment_reversal import audit, post_rev, rev, user_id

LOANS = f"{V2}/loans"
ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("America/Santo_Domingo")  # the product's frozen contract timezone in these tests


# ================================ helpers ============================================================
def world10(client, adm, tenant, *, managing=None, raw=None, code="PRD-10"):
    """T-007 world (disbursed loan) with an optional MANAGING branch different from the origin branch."""
    branch = mk_branch(client, adm, "B1")
    customer = mk_customer(client, adm)
    product, version = flow(client, adm, code, raw=raw)
    assert client.put(POLICY, headers=adm, json=OPEN_POLICY).status_code == 200
    w = SimpleNamespace(b=branch, c=customer, p=product, v=version)
    extra = {"managing_branch_id": managing["id"]} if managing else {}
    d = review(client, adm, w, amount="10000", **extra)
    approve(client, adm, d, amount="7000")
    w.app_id, w.f = d["id"], post(client, adm, d["id"], "formalize")
    w.cash = cash_for(tenant, branch["id"])
    w.loan = disburse(client, adm, w)
    return w


def at_local(day: date, hour=12, minute=0) -> datetime:
    """A UTC instant that is ``day hour:minute`` in the contract timezone."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ).astimezone(UTC)


def clock_at(monkeypatch, when: datetime):
    for mod in (pay_service, loan_service, rev_service, overdue_service):
        monkeypatch.setattr(mod, "now_utc", lambda when=when: when)


def dates(client, adm, w):
    return [date.fromisoformat(r["due_date"]) for r in schedule(client, adm, w.loan["id"])]


def assess(client, hdr, w, expect=200):
    r = client.post(f"{LOANS}/{w.loan['id']}/delinquency/assess", headers=hdr)
    assert r.status_code == expect, f"assess: {r.status_code} {r.text}"
    return r.json()


def detail(client, adm, w):
    return client.get(f"{LOANS}/{w.loan['id']}", headers=adm).json()


def stored(w):
    with SessionLocal() as db:
        return db.execute(text("SELECT status FROM credit_loans WHERE id = :i"), {"i": w.loan["id"]}).scalar()


def contractual_rows():
    with SessionLocal() as db:
        return [
            tuple(r)
            for r in db.execute(
                text(
                    "SELECT id, loan_id, sequence, contractual_date, due_date, delinquency_starts_on, principal_due, "
                    "interest_due, fees_due, delinquency_due, total_due FROM credit_loan_obligations ORDER BY id"
                )
            )
        ]


def money_counts():
    with SessionLocal() as db:
        return {
            t: db.execute(text(f"SELECT count(*) FROM {t}")).scalar()
            for t in (
                "credit_payments",
                "credit_payment_applications",
                "credit_payment_reversals",
                "credit_payment_reversal_applications",
                "cash_movements",
                "cash_audit",
            )
        }


def write_listener():
    statements: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        if re.match(r"\s*(INSERT|UPDATE|DELETE)", statement, re.I):
            statements.append(statement[:90])

    event.listen(engine, "before_cursor_execute", before)
    return statements, lambda: event.remove(engine, "before_cursor_execute", before)


def view(oid, seq, due, applied=None, **due_parts):
    comps = {"fee": 0, "delinquency": 0, "interest": 0, "principal": 0} | due_parts
    return allocation.ObligationView(oid, seq, due, {k: Decimal(v) for k, v in comps.items()}, applied or {})


# ================================ pure rules (no DB) =================================================
def test_overdue_is_strictly_after_the_effective_due_date_and_needs_net_debt():
    d = date(2026, 3, 10)
    ob = view(1, 1, d, interest=10, principal=90)
    assert not allocation.is_overdue(ob, d - timedelta(days=1))
    assert not allocation.is_overdue(ob, d)  # due TODAY is not overdue
    assert allocation.is_overdue(ob, d + timedelta(days=1))  # the first overdue day is the NEXT calendar day
    assert [allocation.days_overdue(ob, d + timedelta(days=n)) for n in (-3, 0, 1, 2, 5)] == [0, 0, 1, 2, 5]
    assert allocation.overdue_outstanding(ob, d + timedelta(days=5)) == 100
    paid = view(1, 1, d, applied={"interest": Decimal(10), "principal": Decimal(90)}, interest=10, principal=90)
    late = d + timedelta(days=30)
    assert not allocation.is_overdue(paid, late) and allocation.days_overdue(paid, late) == 0  # settled: no age
    assert allocation.overdue_outstanding(paid, late) == 0
    part = view(1, 1, d, applied={"principal": Decimal(30)}, interest=10, principal=90)
    assert allocation.overdue_outstanding(part, late) == 70  # NET of the applications


def test_net_outstanding_never_goes_negative_per_component():
    d = date(2026, 3, 10)
    over = view(1, 1, d, applied={"interest": Decimal(25), "principal": Decimal(10)}, interest=20, principal=90)
    assert allocation.net_outstanding(over) == 80  # interest clipped at 0 (it cannot offset the principal)
    assert allocation.overdue_outstanding(over, d + timedelta(days=1)) == 80


def test_the_loan_status_priority_is_paid_then_past_due_then_active():
    d = date(2026, 3, 10)
    settled = [view(1, 1, d, applied={"principal": Decimal(50)}, principal=50)]
    assert allocation.loan_status(settled, d + timedelta(days=90)) == "paid"  # settled wins even when long overdue
    mixed = [
        view(1, 1, d, applied={"principal": Decimal(50)}, principal=50),
        view(2, 2, d + timedelta(days=30), principal=50),
    ]
    assert allocation.loan_status(mixed, d + timedelta(days=10)) == "active"  # only future debt remains
    assert allocation.loan_status(mixed, d + timedelta(days=31)) == "past_due"  # a later obligation went overdue
    assert allocation.loan_status(mixed, d + timedelta(days=30)) == "active"  # due today is not overdue
    due_only = [view(1, 1, d, principal=50), view(2, 2, d + timedelta(days=30), principal=50)]
    assert allocation.loan_status(due_only, d + timedelta(days=5)) == "past_due"
    s = allocation.overdue_summary(due_only, d + timedelta(days=45))
    assert (s["overdue_obligations"], s["overdue_outstanding"], s["max_days_overdue"]) == (2, 100, 45)


# ================================ the overdue rule over real loans ====================================
def test_the_day_after_the_effective_due_date_is_the_first_overdue_day_in_the_contract_timezone(
    client, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    d1 = dates(client, adm, w)[0]
    first = schedule(client, adm, w.loan["id"])[0]
    cases = [
        (at_local(d1 - timedelta(days=3)), False, 0),
        (at_local(d1), False, 0),
        (at_local(d1, 23, 59), False, 0),  # still the due date in the contract timezone
        (at_local(d1 + timedelta(days=1), 0, 0), True, 1),  # the first minute of the next local day
        (at_local(d1 + timedelta(days=1)), True, 1),
        (at_local(d1 + timedelta(days=5)), True, 5),
    ]
    for when, is_late, days in cases:
        clock_at(monkeypatch, when)
        row = schedule(client, adm, w.loan["id"])[0]
        assert (row["is_overdue"], row["days_overdue"]) == (is_late, days), when
        bal = balances(client, adm, w.loan["id"])
        assert bal["overdue_obligations"] == int(is_late) and bal["max_days_overdue"] == days
        assert Decimal(bal["overdue_outstanding"]) == (first["total_due"] if is_late else 0)
        assert bal["projected_status"] == ("past_due" if is_late else "active")
    # only the first obligation is overdue; the others are future debt
    assert [r["is_overdue"] for r in schedule(client, adm, w.loan["id"])][:3] == [True, False, False]


def test_utc_and_local_midnight_edge_uses_the_frozen_contract_timezone(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    d1 = dates(client, adm, w)[0]
    nxt = d1 + timedelta(days=1)
    edge = datetime(nxt.year, nxt.month, nxt.day, 3, 0, tzinfo=UTC)  # 23:00 on d1 in Santo Domingo, already d1+1 in UTC
    clock_at(monkeypatch, edge)
    assert edge.date() == nxt  # the UTC calendar date WOULD say overdue ...
    bal = balances(client, adm, w.loan["id"])
    assert (
        bal["business_date"] == d1.isoformat() and bal["overdue_obligations"] == 0
    )  # ... the contract timezone says no
    assert assess(client, adm, w)["status"] == "active" and stored(w) == "active"
    clock_at(monkeypatch, datetime(nxt.year, nxt.month, nxt.day, 4, 0, tzinfo=UTC))  # 00:00 local: the next day begins
    assert balances(client, adm, w.loan["id"])["overdue_obligations"] == 1
    assert assess(client, adm, w)["status"] == "past_due" and stored(w) == "past_due"


def test_only_the_effective_due_date_counts_never_the_contractual_one(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    cal = {
        "source": "product",
        "timezone": "America/Santo_Domingo",
        "non_working_weekdays": [0, 1, 2, 3, 4, 5],  # only Sunday works: nearly every due date moves
        "holidays": [],
        "adjustment": "next_business_day",
        "delinquency_start_basis": "effective_due_date",
        "accrual_basis": "contractual_dates",
    }
    w = world10(client, adm, tenant_a, raw=rules(calendar=cal), code="PRD-CAL")
    rows = schedule(client, adm, w.loan["id"])
    moved = [r for r in rows if r["contractual_date"] != r["due_date"]]
    assert moved, "the calendar adjustment must move at least one due date"
    pick = moved[0]
    contractual, effective = date.fromisoformat(pick["contractual_date"]), date.fromisoformat(pick["due_date"])
    assert effective > contractual
    idx = rows.index(pick)
    # settle every EARLIER obligation so the picked one is the only candidate (field payments, due-to-date amounts)
    clock_at(monkeypatch, at_local(effective))  # due date: not overdue although past the contractual date
    assert schedule(client, adm, w.loan["id"])[idx]["is_overdue"] is False
    clock_at(monkeypatch, at_local(effective + timedelta(days=1)))
    row = schedule(client, adm, w.loan["id"])[idx]
    assert (row["is_overdue"], row["days_overdue"]) == (True, 1)  # measured from the EFFECTIVE date
    clock_at(monkeypatch, at_local(contractual + timedelta(days=1)))
    assert schedule(client, adm, w.loan["id"])[idx]["is_overdue"] is False  # past the contractual date: not enough


def test_delinquency_grace_does_not_delay_overdue_and_a_disabled_product_still_has_overdue_debt(
    client, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)  # default product: delinquency enabled, grace 3 days
    first = schedule(client, adm, w.loan["id"])[0]
    d1 = date.fromisoformat(first["due_date"])
    assert first["delinquency_starts_on"] == (d1 + timedelta(days=4)).isoformat()  # T-007: due + 1 + grace
    clock_at(monkeypatch, at_local(d1 + timedelta(days=1)))
    assert schedule(client, adm, w.loan["id"])[0]["is_overdue"] is True  # grace does NOT delay the overdue condition
    assert assess(client, adm, w)["status"] == "past_due"
    off = world10_disabled(client, adm, tenant_a)
    row = schedule(client, adm, off.loan["id"])[0]
    assert row["delinquency_starts_on"] is None  # delinquency disabled: no start date at all ...
    clock_at(monkeypatch, at_local(date.fromisoformat(row["due_date"]) + timedelta(days=1)))
    assert schedule(client, adm, off.loan["id"])[0]["is_overdue"] is True  # ... and the debt is still overdue
    assert assess(client, adm, off)["status"] == "past_due"


def world10_disabled(client, adm, tenant):
    branch = mk_branch(client, adm, "B-OFF")
    customer = mk_customer(client, adm, identity=ident(given="Maria", family="Gomez", doc=None))
    product, version = flow(client, adm, "PRD-OFF", raw=rules(delinquency={"enabled": False}))
    w = SimpleNamespace(b=branch, c=customer, p=product, v=version)
    d = review(client, adm, w, amount="10000")
    approve(client, adm, d, amount="7000")
    w.app_id, w.f = d["id"], post(client, adm, d["id"], "formalize")
    w.cash = cash_for(tenant, branch["id"])
    w.loan = disburse(client, adm, w, key="disb-key-off-0001")
    return w


def test_overdue_creates_no_money_and_never_touches_the_contractual_obligations(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    d1 = dates(client, adm, w)[0]
    rows_before, money_before = contractual_rows(), money_counts()
    assert {r[9] for r in rows_before} == {Decimal(0)}  # delinquency_due is contractual and zero
    clock_at(monkeypatch, at_local(d1 + timedelta(days=40)))
    for _ in range(2):
        assess(client, adm, w)
    assert contractual_rows() == rows_before  # nothing mutated: delinquency_due, total_due, dates, amounts
    assert money_counts() == money_before  # no payment, application, reversal, cash movement or fee
    with SessionLocal() as db:
        names = {r[0] for r in db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"))}
        total_due_ok = db.execute(
            text("SELECT count(*) FROM credit_loan_obligations WHERE delinquency_due <> 0")
        ).scalar()
    assert not {n for n in names if any(x in n for x in ("accrual", "late_fee", "ledger", "journal", "outbox", "gl_"))}
    assert total_due_ok == 0


# ================================ net debt: payments and reversals ===================================
def test_overdue_outstanding_is_net_of_payments_and_reversals(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    first = schedule(client, adm, w.loan["id"])[0]
    clock_at(monkeypatch, at_local(date.fromisoformat(first["due_date"]) + timedelta(days=5)))
    total = first["total_due"]
    assert Decimal(balances(client, adm, w.loan["id"])["overdue_outstanding"]) == total
    p = pay(client, adm, w, "10.00", origin="field")  # partial: the debt left is NET of the application
    row = schedule(client, adm, w.loan["id"])[0]
    assert row["is_overdue"] and Decimal(row["overdue_outstanding"]) == total - Decimal("10.00")
    assert Decimal(balances(client, adm, w.loan["id"])["overdue_outstanding"]) == total - Decimal("10.00")
    rev(client, adm, p["id"], w, session=None)  # the reversal gives the debt back
    assert Decimal(balances(client, adm, w.loan["id"])["overdue_outstanding"]) == total
    assert Decimal(schedule(client, adm, w.loan["id"])[0]["overdue_outstanding"]) == total
    p2 = pay(client, adm, w, total, origin="field")  # settle the whole overdue obligation
    row = schedule(client, adm, w.loan["id"])[0]
    assert (row["is_overdue"], row["days_overdue"], Decimal(row["overdue_outstanding"])) == (False, 0, 0)
    bal = balances(client, adm, w.loan["id"])
    assert bal["overdue_obligations"] == 0 and bal["projected_status"] == "active" and stored(w) == "active"
    assert p2["id"] != p["id"]


def test_projected_status_active_past_due_paid_from_the_net_ledger(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    rows = schedule(client, adm, w.loan["id"])
    d1 = date.fromisoformat(rows[0]["due_date"])
    clock_at(monkeypatch, at_local(d1 - timedelta(days=2)))
    assert balances(client, adm, w.loan["id"])["projected_status"] == "active"  # only future debt
    clock_at(monkeypatch, at_local(d1 + timedelta(days=2)))
    assert balances(client, adm, w.loan["id"])["projected_status"] == "past_due"  # overdue net debt
    clock_at(monkeypatch, at_local(d1 + timedelta(days=400)))
    pay(client, adm, w, sum(r["total_due"] for r in rows), origin="field")
    bal = balances(client, adm, w.loan["id"])
    assert bal["projected_status"] == "paid" and bal["overdue_obligations"] == 0 and stored(w) == "paid"  # paid wins


def test_loan_detail_exposes_the_derived_overdue_facts(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    d1 = dates(client, adm, w)[0]
    clock_at(monkeypatch, at_local(d1 + timedelta(days=7)))
    d = detail(client, adm, w)
    assert d["status"] == "active"  # the STORED status is stale: nobody projected it yet
    b = d["balances"]
    assert (b["overdue_obligations"], b["max_days_overdue"], b["projected_status"]) == (1, 7, "past_due")
    assert Decimal(b["overdue_outstanding"]) > 0


# ================================ the explicit assessment =============================================
def test_assessment_projects_the_stored_status_both_ways_and_audits_only_real_changes(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    d1 = dates(client, adm, w)[0]
    clock_at(monkeypatch, at_local(d1 + timedelta(days=5)))
    assert stored(w) == "active"  # stored status stale, derived overdue visible
    out = assess(client, adm, w)  # active -> past_due
    assert (out["previous_status"], out["status"], out["changed"]) == ("active", "past_due", True)
    assert (out["overdue_obligations"], out["max_days_overdue"]) == (1, 5) and stored(w) == "past_due"
    events = audit("loan.delinquency_assessed")
    assert len(events) == 1
    e = events[0]
    assert e.actor_id == tenant_a["admin_id"] and e.tenant_id == tenant_a["tenant_id"] and e.correlation_id
    d = e.details
    assert (d["loan_id"], d["previous_status"], d["resulting_status"]) == (w.loan["id"], "active", "past_due")
    assert (d["business_date"], d["overdue_obligations"], d["max_days_overdue"]) == (
        (d1 + timedelta(days=5)).isoformat(),
        1,
        5,
    )
    assert (
        d["overdue_outstanding"] == out["overdue_outstanding"]
        and d["managing_branch_id"] == w.loan["managing_branch_id"]
    )
    assert d["rules_digest"] == w.loan["rules_hash"] and d["contract_digest"] == w.loan["contract_hash"]
    assert "Perez" not in str(d) and "001-0000001-1" not in str(d)  # no PII
    # idempotent: the same state again writes NOTHING and audits nothing
    statements, stop = write_listener()
    try:
        again = assess(client, adm, w)
    finally:
        stop()
    assert again["changed"] is False and again["status"] == "past_due" and statements == []
    assert len(audit("loan.delinquency_assessed")) == 1
    # the date moves back (test clock): the projection follows the date -> active again
    clock_at(monkeypatch, at_local(d1 - timedelta(days=1)))
    back = assess(client, adm, w)
    assert (back["previous_status"], back["status"], back["changed"]) == ("past_due", "active", True)
    assert len(audit("loan.delinquency_assessed")) == 2


def test_payments_work_on_a_past_due_loan_and_reproject_every_outcome(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    rows = schedule(client, adm, w.loan["id"])
    d1 = date.fromisoformat(rows[0]["due_date"])
    clock_at(monkeypatch, at_local(d1 + timedelta(days=40)))  # obligations 1 and 2 are overdue (monthly)
    assert assess(client, adm, w)["status"] == "past_due" and stored(w) == "past_due"
    p1 = pay(client, adm, w, rows[0]["total_due"], origin="field")  # accepted while past_due
    assert (
        p1["status"] == "confirmed" and stored(w) == "past_due"
    )  # past_due -> past_due: obligation 2 is still overdue
    assert pay(client, adm, w, rows[1]["total_due"], origin="field")["status"] == "confirmed"
    assert stored(w) == "active"  # past_due -> active: only future debt remains
    ev = audit("payment.confirmed")
    assert [(x.details["previous_loan_status"], x.details["loan_status"]) for x in ev] == [
        ("past_due", "past_due"),
        ("past_due", "active"),
    ]
    clock_at(monkeypatch, at_local(d1 + timedelta(days=400)))  # everything is overdue now
    assert assess(client, adm, w)["status"] == "past_due"
    rest = sum(r["total_due"] for r in rows[2:])
    pay(client, adm, w, rest, origin="field")
    assert stored(w) == "paid"  # past_due -> paid
    assert Decimal(balances(client, adm, w.loan["id"])["total_outstanding"]) == 0


def test_reversals_reproject_with_the_common_rule_including_past_due_loans(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    rows = schedule(client, adm, w.loan["id"])
    d1 = date.fromisoformat(rows[0]["due_date"])
    total = sum(r["total_due"] for r in rows)
    clock_at(monkeypatch, at_local(d1 + timedelta(days=400)))
    p1 = pay(client, adm, w, total, origin="field")
    assert stored(w) == "paid"
    rev(client, adm, p1["id"], w, session=None)  # D3: the reopened debt is ALREADY overdue -> straight to past_due
    assert stored(w) == "past_due"
    assert audit("loan.delinquency_assessed") == []  # no later assessment was needed
    p2 = pay(client, adm, w, total, origin="field")
    assert stored(w) == "paid"
    clock_at(
        monkeypatch, at_local(d1 - timedelta(days=5))
    )  # (test clock) before every due date: the debt is all future
    rev(client, adm, p2["id"], w, session=None)
    assert stored(w) == "active"  # paid -> active: not overdue yet
    clock_at(monkeypatch, at_local(d1 + timedelta(days=400)))
    assert assess(client, adm, w)["status"] == "past_due"
    p3 = pay(client, adm, w, "10.00", origin="field")
    assert stored(w) == "past_due"
    r3 = rev(client, adm, p3["id"], w, session=None)  # a reversal on a stored past_due loan is accepted
    assert r3["reversal_number"] == "REV-000003" and stored(w) == "past_due"
    ev = audit("payment.reversed")
    assert [(x.details["previous_loan_status"], x.details["loan_status"]) for x in ev] == [
        ("paid", "past_due"),
        ("paid", "active"),
        ("past_due", "past_due"),
    ]


def test_the_stored_status_is_a_projection_rebuilt_from_the_net_ledger(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    rows = schedule(client, adm, w.loan["id"])
    d1 = date.fromisoformat(rows[0]["due_date"])
    clock_at(monkeypatch, at_local(d1 + timedelta(days=40)))
    p = pay(client, adm, w, rows[0]["total_due"] + Decimal("5.00"), origin="field")
    rev(client, adm, p["id"], w, session=None)
    pay(client, adm, w, "20.00", origin="field")
    with SessionLocal() as db:  # independent rebuild from the contractual rows + the NET ledger helper
        loan = db.get(ledger.CreditLoan, w.loan["id"])
        _o, views = ledger.views(db, loan.id)
        expected = allocation.loan_status(views, ledger.business_date(db, loan, loan_service.now_utc()))
        db.execute(text("UPDATE credit_loans SET status = 'active'"))  # corrupt the projection by hand
        db.commit()
    assert expected == "past_due"
    assert assess(client, adm, w)["status"] == "past_due" and stored(w) == "past_due"
    with SessionLocal() as db:  # neither obligation.status nor the stored loan.status is the source of truth
        db.execute(text("UPDATE credit_loan_obligations SET status = 'paid'"))
        db.execute(text("UPDATE credit_loans SET status = 'paid'"))
        db.commit()
    assert [r["is_overdue"] for r in schedule(client, adm, w.loan["id"])][:3] == [True, True, False]
    bal = balances(client, adm, w.loan["id"])
    assert (bal["overdue_obligations"], bal["projected_status"]) == (2, "past_due")
    out = assess(client, adm, w)
    assert (out["previous_status"], out["status"], out["changed"]) == ("paid", "past_due", True)


# ================================ GET purity ==========================================================
def test_every_get_derives_overdue_without_writing_even_when_the_stored_status_lags(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    d1 = dates(client, adm, w)[0]
    clock_at(monkeypatch, at_local(d1))  # due today: payable, not overdue
    p = pay(client, adm, w, "10.00", origin="field")
    clock_at(monkeypatch, at_local(d1 + timedelta(days=10)))
    assert stored(w) == "active"
    statements, stop = write_listener()
    try:
        urls = (
            LOANS,
            f"{LOANS}/{w.loan['id']}",
            f"{LOANS}/{w.loan['id']}/schedule",
            f"{LOANS}/{w.loan['id']}/balances",
            f"{LOANS}/{w.loan['id']}/payments",
            f"{V2}/payments/{p['id']}",
        )
        bodies = {u: client.get(u, headers=adm) for u in urls}
    finally:
        stop()
    assert all(r.status_code == 200 for r in bodies.values()) and statements == []
    assert bodies[f"{LOANS}/{w.loan['id']}/balances"].json()["projected_status"] == "past_due"  # visible ...
    assert stored(w) == "active"  # ... and NOT silently corrected by the read


# ================================ authorization and tenants ===========================================
def test_assessment_requires_its_permission_on_the_managing_branch_only(client, sink, tenant_a, tenant_b, monkeypatch):
    adm = admin_headers(client, tenant_a)
    b2 = mk_branch(client, adm, "B2-MANAGING")
    w = world10(client, adm, tenant_a, managing=b2)
    assert w.loan["managing_branch_id"] == b2["id"] and w.loan["origin_branch_id"] == w.b["id"] != b2["id"]
    d1 = dates(client, adm, w)[0]
    clock_at(monkeypatch, at_local(d1 + timedelta(days=5)))
    before = (stored(w), len(audit("loan.delinquency_assessed")))
    plain = user_hdr(client, sink, adm, tenant_a, "p@x.com", ["loans.read", "payments.read"])
    assert client.post(f"{LOANS}/{w.loan['id']}/delinquency/assess", headers=plain).status_code == 403
    at_origin = user_hdr(
        client,
        sink,
        adm,
        tenant_a,
        "o@x.com",
        ["loans.delinquency.assess", "loans.read"],
        scope="branch",
        branch_id=w.b["id"],
    )  # origin AND disbursement branch: not the managing one
    assert client.post(f"{LOANS}/{w.loan['id']}/delinquency/assess", headers=at_origin).status_code == 403
    assert client.post(f"{LOANS}/{w.loan['id']}/delinquency/assess").status_code == 401
    foreign = admin_headers(client, tenant_b)
    assert client.post(f"{LOANS}/{w.loan['id']}/delinquency/assess", headers=foreign).status_code == 404
    assert client.post(f"{LOANS}/999999/delinquency/assess", headers=adm).status_code == 404
    assert (stored(w), len(audit("loan.delinquency_assessed"))) == before  # nothing happened
    at_managing = user_hdr(
        client,
        sink,
        adm,
        tenant_a,
        "m@x.com",
        ["loans.delinquency.assess", "loans.read"],
        scope="branch",
        branch_id=b2["id"],
    )
    out = assess(client, at_managing, w)
    assert out["status"] == "past_due" and audit("loan.delinquency_assessed")[0].actor_id == user_id("m@x.com")
    # a loan WITHOUT managing branch needs tenant-wide scope: an origin-scoped grant is not enough
    w2 = world10_second(client, adm, tenant_a)
    assert w2.loan["managing_branch_id"] is None
    clock_at(
        monkeypatch,
        at_local(date.fromisoformat(schedule(client, adm, w2.loan["id"])[0]["due_date"]) + timedelta(days=5)),
    )
    at_w2 = user_hdr(
        client,
        sink,
        adm,
        tenant_a,
        "o2@x.com",
        ["loans.delinquency.assess", "loans.read"],
        scope="branch",
        branch_id=w2.b["id"],
    )
    assert client.post(f"{LOANS}/{w2.loan['id']}/delinquency/assess", headers=at_w2).status_code == 403
    assert assess(client, adm, w2)["status"] == "past_due"


def world10_second(client, adm, tenant):
    branch = mk_branch(client, adm, "B-SECOND")
    customer = mk_customer(client, adm, identity=ident(given="Luis", family="Almonte", doc=None))
    product, version = flow(client, adm, "PRD-SEC")
    w = SimpleNamespace(b=branch, c=customer, p=product, v=version)
    d = review(client, adm, w, amount="10000")
    approve(client, adm, d, amount="7000")
    w.app_id, w.f = d["id"], post(client, adm, d["id"], "formalize")
    w.cash = cash_for(tenant, branch["id"])
    w.loan = disburse(client, adm, w, key="disb-key-sec-0001")
    return w


def test_a_retired_product_after_formalization_changes_nothing(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    d1 = dates(client, adm, w)[0]

    assert (
        client.post(
            f"{PRODUCTS}/{w.p['id']}/versions/{w.v['id']}/retire", headers=adm, json={"reason": "obsoleta"}
        ).status_code
        == 200
    )
    assert (
        client.post(f"{PRODUCTS}/{w.p['id']}/deactivate", headers=adm, json={"reason": "fuera de oferta"}).status_code
        == 200
    )
    clock_at(monkeypatch, at_local(d1 + timedelta(days=5)))
    bal = balances(client, adm, w.loan["id"])
    assert (bal["overdue_obligations"], bal["max_days_overdue"], bal["projected_status"]) == (1, 5, "past_due")
    assert assess(client, adm, w)["status"] == "past_due"


# ================================ races (real PostgreSQL) =============================================
def test_two_assessments_racing_change_the_status_once(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    clock_at(monkeypatch, at_local(dates(client, adm, w)[0] + timedelta(days=5)))
    lock = tracked_connect()
    lock.execute(text("SELECT id FROM credit_loans WHERE id = :i FOR UPDATE"), {"i": w.loan["id"]})
    ts = [in_thread(lambda: client.post(f"{LOANS}/{w.loan['id']}/delinquency/assess", headers=adm)) for _ in range(3)]
    wait_blocked_n(LOAN_LOCK, 3)  # all queued behind the loan row: the first step of the lock order
    lock.rollback()
    lock.close()
    for t, _ in ts:
        t.join(60)
    assert [o["resp"].status_code for _, o in ts] == [200, 200, 200]
    assert sorted(o["resp"].json()["changed"] for _, o in ts) == [False, False, True]
    assert stored(w) == "past_due" and len(audit("loan.delinquency_assessed")) == 1


def test_assessment_racing_a_payment_that_settles_the_overdue_debt(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    first = schedule(client, adm, w.loan["id"])[0]
    clock_at(monkeypatch, at_local(date.fromisoformat(first["due_date"]) + timedelta(days=5)))
    lock = tracked_connect()
    lock.execute(text("SELECT id FROM credit_loans WHERE id = :i FOR UPDATE"), {"i": w.loan["id"]})
    a_t = in_thread(lambda: client.post(f"{LOANS}/{w.loan['id']}/delinquency/assess", headers=adm))
    p_t = in_thread(
        lambda: client.post(
            f"{LOANS}/{w.loan['id']}/payments",
            headers=adm,
            json={
                "idempotency_key": "race-pay-key-0001",
                "amount": str(first["total_due"]),
                "currency_code": "DOP",
                "origin": "field",
                "receiving_branch_id": w.b["id"],
            },
        )
    )
    wait_blocked_n(LOAN_LOCK, 2)
    lock.rollback()
    lock.close()
    a_t[0].join(60), p_t[0].join(60)
    assert (a_t[1]["resp"].status_code, p_t[1]["resp"].status_code) == (200, 200)  # either order is consistent
    assert stored(w) == "active"  # the overdue debt was settled, whichever ran first
    assert balances(client, adm, w.loan["id"])["projected_status"] == "active"
    assert Decimal(balances(client, adm, w.loan["id"])["overdue_outstanding"]) == 0


def test_assessment_racing_a_reversal_that_reopens_overdue_debt(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    first = schedule(client, adm, w.loan["id"])[0]
    clock_at(monkeypatch, at_local(date.fromisoformat(first["due_date"]) + timedelta(days=5)))
    p = pay(client, adm, w, first["total_due"], origin="field")
    assert stored(w) == "active"
    lock = tracked_connect()
    lock.execute(text("SELECT id FROM credit_loans WHERE id = :i FOR UPDATE"), {"i": w.loan["id"]})
    a_t = in_thread(lambda: client.post(f"{LOANS}/{w.loan['id']}/delinquency/assess", headers=adm))
    r_t = in_thread(lambda: post_rev(client, adm, p["id"], w, session=None))
    wait_blocked_n(LOAN_LOCK, 2)
    lock.rollback()
    lock.close()
    a_t[0].join(60), r_t[0].join(60)
    assert (a_t[1]["resp"].status_code, r_t[1]["resp"].status_code) == (200, 200)
    assert stored(w) == "past_due"  # whichever order: the reopened debt is overdue and the net ledger says so
    assert balances(client, adm, w.loan["id"])["projected_status"] == "past_due"
    assert count("credit_payment_reversals") == 1


# ================================ isolation ===========================================================
def test_nothing_legacy_is_read_or_written_and_no_cash_moves(client, tenant_a, monkeypatch):
    import app.services.loan_service as legacy

    def boom(*_a, **_k):
        raise AssertionError("the legacy refresh_loan_state must never run")

    monkeypatch.setattr(legacy, "refresh_loan_state", boom)
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    d1 = dates(client, adm, w)[0]
    clock_at(monkeypatch, at_local(d1 + timedelta(days=5)))
    legacy_before, money_before = {t: count(t) for t in LEGACY}, money_counts()
    assess(client, adm, w)
    p = pay(client, adm, w, "10.00", origin="field")
    rev(client, adm, p["id"], w, session=None)
    for url in (f"{LOANS}/{w.loan['id']}", f"{LOANS}/{w.loan['id']}/schedule", f"{LOANS}/{w.loan['id']}/balances"):
        assert client.get(url, headers=adm).status_code == 200
    assert {t: count(t) for t in LEGACY} == legacy_before  # legacy payments / loans / installments / capital untouched
    after = money_counts()
    assert (
        after["cash_movements"] == money_before["cash_movements"] and after["cash_audit"] == money_before["cash_audit"]
    )


def test_the_overdue_code_has_no_legacy_settings_live_product_scheduler_or_new_dependency():
    sources = {p.name: p.read_text(encoding="utf-8") for p in (ROOT / "app/modules/loans").glob("*.py")}
    for name in ("overdue.py", "allocation.py", "ledger.py", "payments.py", "reversals.py"):
        src = sources[name]
        assert "refresh_loan_state" not in src and "LoanSettings" not in src and "loan_settings" not in src, name
        assert "date.today" not in src and "datetime.now" not in src, name  # the business date is the contract's
    assert "CreditProductVersion" not in sources["overdue.py"] and "credit.models" not in sources["overdue.py"]
    for name in ("overdue.py", "allocation.py", "ledger.py"):  # the overdue rule never reads the mora start date
        assert not re.search(r"\.delinquency_starts_on|\[.delinquency_starts_on.\]", sources[name]), name
    py = (ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    assert not any(x in py for x in ("celery", "apscheduler", "dramatiq", "arq", "rq>", "schedule>"))
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "schedule:" not in ci and "cron" not in ci  # no scheduled workflow
    main_src = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "BackgroundTasks" not in main_src and "lifespan" not in sources["api.py"]


# ================================ migration 0012 ======================================================
def test_migration_0012_adds_only_the_permission_and_is_reversible(scratch_db):
    from sqlalchemy import create_engine

    assert _alembic(scratch_db, "upgrade", "0011").returncode == 0
    eng = create_engine(scratch_db)
    try:
        with eng.begin() as c:
            c.execute(
                text(
                    "INSERT INTO companies (name, slug, tax_id, address, phone, status, base_currency_code, default_timezone, "
                    "created_at, updated_at) VALUES ('X', 'x-1', '', '', '', 'active', 'DOP', 'America/Santo_Domingo', now(), now())"
                )
            )
            c.execute(
                text(
                    "INSERT INTO tenant_currencies (tenant_id, currency_code, enabled_at) SELECT id, 'DOP', now() FROM companies"
                )
            )
            c.execute(
                text(
                    "INSERT INTO roles (tenant_id, name, description, status, system_defined, created_at, updated_at) "
                    "SELECT id, 'Administrador de agencia', 'x', 'active', true, now(), now() FROM companies"
                )
            )
        with eng.connect() as c:
            before_tables = {
                r[0] for r in c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
            }
            before_cols = {
                (r[0], r[1])
                for r in c.execute(
                    text("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'")
                )
            }
        up = _alembic(scratch_db, "upgrade", "0012")
        assert up.returncode == 0, up.stderr
        with eng.connect() as c:
            assert (
                c.execute(text("SELECT count(*) FROM permissions WHERE code = 'loans.delinquency.assess'")).scalar()
                == 1
            )
            assert (
                c.execute(text("SELECT is_sensitive FROM permissions WHERE code = 'loans.delinquency.assess'")).scalar()
                is True
            )
            assert (
                c.execute(
                    text(
                        "SELECT count(*) FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id "
                        "WHERE p.code = 'loans.delinquency.assess'"
                    )
                ).scalar()
                == 1
            )
            assert before_tables == {
                r[0] for r in c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
            }
            assert before_cols == {
                (r[0], r[1])
                for r in c.execute(
                    text("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'")
                )
            }  # NO new table, NO new column
        # (alembic check compares against the models at head: it runs after the final re-upgrade below)
        down = _alembic(scratch_db, "downgrade", "0011")
        assert down.returncode == 0, down.stderr
        with eng.connect() as c:
            assert (
                c.execute(text("SELECT count(*) FROM permissions WHERE code = 'loans.delinquency.assess'")).scalar()
                == 0
            )
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0011"
        again = _alembic(scratch_db, "upgrade", "head")
        assert again.returncode == 0, again.stderr
        assert _alembic(scratch_db, "check").returncode == 0
    finally:
        eng.dispose()


def test_the_assessment_runs_under_concurrent_threads_only_through_the_loan_lock(client, tenant_a, monkeypatch):
    """Smoke: many simultaneous assessments of an unchanged loan never write and never deadlock."""
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    barrier = threading.Barrier(4)

    def go():
        barrier.wait(10)
        return client.post(f"{LOANS}/{w.loan['id']}/delinquency/assess", headers=adm)

    ts = [in_thread(go) for _ in range(3)]
    barrier.wait(10)
    for t, _ in ts:
        t.join(60)
    assert [o["resp"].status_code for _, o in ts] == [200, 200, 200]
    assert all(o["resp"].json()["changed"] is False for _, o in ts) and audit("loan.delinquency_assessed") == []
