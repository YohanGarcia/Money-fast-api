"""T-008 Payment Runtime v1 tests (T008-*). PostgreSQL only.

money received -> confirmed payment -> contractual allocation -> payment applications -> derived balances -> projection.
Cash only (counter / field). No reversal, prepayment, payoff, delinquency, bank or accounting here.
"""

import itertools
import re
import threading
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from app.core.db import SessionLocal, engine
from app.models.user import User, UserRole
from app.modules.cash import port as cash_port
from app.modules.identity.models import SecurityEvent
from app.modules.loans import allocation, ledger
from app.modules.loans import payments as pay_service
from app.modules.loans import service as loan_service
from app.schemas.cash import CashCommand
from app.services import cash_service
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
from tests.test_t005_engine import rules
from tests.test_t006_origination import (  # noqa: F401
    _release_tracked_connections,
    count,
    in_thread,
    tracked_connect,
    user_hdr,
    wait_blocked_n,
)
from tests.test_t007_disbursement import cash_for, disburse, session_balance, world7

LOANS = f"{V2}/loans"
PAYMENTS = f"{V2}/payments"
LOAN_LOCK = "%FROM credit_loans%FOR UPDATE%"
RECEIPT = "credit_payment_receipt"
_keys = itertools.count(1)
LEGACY = (
    "payments",
    "loans",
    "loan_installments",
    "cash_transfers",
    "cash_deliveries",
    "capital_movements",
    "bank_accounts",
)


# ================================ helpers ============================================================
def key():
    return f"pay-key-{next(_keys):08d}"


def loan_world(client, adm, tenant, **kw):
    w = world7(client, adm, tenant, **kw)
    out = disburse(client, adm, w)
    w.loan = out
    return w


def clock(monkeypatch, *, days=0, at=None):
    """Move the business clock used by the payment command AND the reads (the disbursement already happened)."""
    when = at or (datetime.now(UTC) + timedelta(days=days))
    monkeypatch.setattr(pay_service, "now_utc", lambda: when)
    monkeypatch.setattr(loan_service, "now_utc", lambda: when)
    return when


def schedule(client, adm, loan_id):
    rows = client.get(f"{LOANS}/{loan_id}/schedule", headers=adm).json()["obligations"]
    return [
        {
            **r,
            **{k: Decimal(r[k]) for k in ("principal_due", "interest_due", "fees_due", "delinquency_due", "total_due")},
        }
        for r in rows
    ]


def pbody(w, amount, origin="counter", branch=None, session=None, k=None, **extra):
    body = {
        "idempotency_key": k or key(),
        "amount": str(amount),
        "currency_code": "DOP",
        "origin": origin,
        "receiving_branch_id": branch or w.b["id"],
    }
    if origin == "counter":
        body["cash_session_id"] = session or w.cash.session_id
    return body | extra


def pay(client, hdr, w, amount, expect=200, **kw):
    r = client.post(f"{LOANS}/{w.loan['id']}/payments", headers=hdr, json=pbody(w, amount, **kw))
    assert r.status_code == expect, f"pay: {r.status_code} {r.text}"
    return r.json()


def balances(client, adm, loan_id):
    return client.get(f"{LOANS}/{loan_id}/balances", headers=adm).json()


def applications(payment_id=None):
    with SessionLocal() as db:
        sql = "SELECT payment_id, obligation_id, component, amount FROM credit_payment_applications"
        if payment_id:
            sql += " WHERE payment_id = :p"
        return db.execute(text(sql + " ORDER BY id"), {"p": payment_id}).all()


def money_state():
    with SessionLocal() as db:
        return {
            t: db.execute(text(f"SELECT count(*) FROM {t}")).scalar()
            for t in (
                "credit_payments",
                "credit_payment_applications",
                "cash_movements",
                "security_events",
                "cash_audit",
            )
        } | {
            "seq": db.execute(
                text("SELECT coalesce(max(last_value), 0) FROM tenant_sequences WHERE name = 'credit_payment'")
            ).scalar()
        }


def audit(prefix="payment."):
    with SessionLocal() as db:
        return [e for e in db.query(SecurityEvent).order_by(SecurityEvent.id) if e.event_type.startswith(prefix)]


def view(oid, seq, due, **due_parts):
    comps = {"fee": 0, "delinquency": 0, "interest": 0, "principal": 0} | due_parts
    return allocation.ObligationView(oid, seq, due, {k: Decimal(v) for k, v in comps.items()}, {})


# ================================ pure allocation (no DB) ============================================
def test_allocation_oldest_effective_due_first_then_sequence_and_exact_consumption():
    d = date
    obs = [
        view(1, 1, d(2026, 3, 10), interest=10, principal=90),  # contractually first ...
        view(2, 2, d(2026, 3, 1), interest=10, principal=90),  # ... but due EARLIER (effective date wins)
        view(3, 3, d(2026, 3, 1), interest=5, principal=45),  # same due date as #2: ascending sequence breaks the tie
        view(4, 4, d(2026, 4, 1), interest=10, principal=90),  # future: never touched
    ]
    order = ["fees", "delinquency", "interest", "principal"]
    rows = allocation.allocate(Decimal("160"), obs, d(2026, 3, 10), order)
    assert rows == [
        (2, "interest", 10),
        (2, "principal", 90),
        (3, "interest", 5),
        (3, "principal", 45),
        (1, "interest", 10),
    ]
    assert sum(a for _, _, a in rows) == Decimal("160")  # EXACT consumption
    assert all(oid != 4 for oid, _, _ in rows)  # no future installment
    with pytest.raises(ValueError):
        allocation.allocate(
            Decimal("250.01"), obs, d(2026, 3, 10), order
        )  # due to date = 250: one cent more is refused
    assert allocation.allocate(Decimal("250"), obs, d(2026, 3, 10), order)[-1] == (1, "principal", 90)


def test_allocation_follows_the_frozen_component_order_including_delinquency():
    d = date
    ob = [view(1, 1, d(2026, 3, 1), fee=5, delinquency=7, interest=10, principal=100)]
    default = allocation.allocate(Decimal("20"), ob, d(2026, 3, 1), ["fees", "delinquency", "interest", "principal"])
    assert default == [(1, "fee", 5), (1, "delinquency", 7), (1, "interest", 8)]
    principal_first = allocation.allocate(
        Decimal("20"), ob, d(2026, 3, 1), ["principal", "interest", "fees", "delinquency"]
    )
    assert principal_first == [
        (1, "principal", 20)
    ]  # the contract's order, not a hard-coded fee -> interest -> principal
    view_ = allocation.ObligationView(
        1,
        1,
        d(2026, 3, 1),
        {"fee": Decimal(5), "delinquency": Decimal(0), "interest": Decimal(10), "principal": Decimal(100)},
        {},
    )
    assert allocation.obligation_status(view_) == "pending"
    partial = allocation.ObligationView(1, 1, d(2026, 3, 1), view_.due, {"interest": Decimal(1)})
    assert allocation.obligation_status(partial) == "partially_paid"
    full = allocation.ObligationView(1, 1, d(2026, 3, 1), view_.due, view_.due)
    assert allocation.obligation_status(full) == "paid"


# ================================ counter / field flows ==============================================
def test_counter_payment_full_current_due_cash_in_applications_projection_and_derived_balances(
    client, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    legacy_before = {t: count(t) for t in LEGACY}
    clock(monkeypatch, days=35)  # the first obligation is due, the second is not
    rows = schedule(client, adm, w.loan["id"])
    first = rows[0]
    before = session_balance(w.cash.session_id)
    out = pay(client, adm, w, first["total_due"])
    assert out["replayed"] is False and out["payment_number"] == "PAG-000001" and out["status"] == "confirmed"
    assert (out["origin"], out["method"], out["receiving_branch_id"]) == ("counter", "cash", w.b["id"])
    assert out["collected_by"] == tenant_a["admin_id"] and out["currency_code"] == "DOP"
    # the allocation: only obligation #1, interest then principal (the frozen order), summing EXACTLY to the payment
    apps = [(a["component"], Decimal(a["amount"])) for a in out["applications"]]
    assert apps == [("interest", first["interest_due"]), ("principal", first["principal_due"])]
    assert sum(a for _, a in apps) == Decimal(out["amount"]) == first["total_due"]
    # exactly one cash receipt of a NON-legacy kind, into the open session of the receiving branch
    assert session_balance(w.cash.session_id) == before + first["total_due"]
    with SessionLocal() as db:
        mv = db.execute(
            text("SELECT id, kind, amount, reference FROM cash_movements WHERE kind = :k"), {"k": RECEIPT}
        ).all()
    assert (
        len(mv) == 1
        and mv[0].amount == first["total_due"]
        and mv[0].reference == "PAG-000001"
        and out["cash_movement_id"] == mv[0].id
    )
    # projection + derived balances
    after = schedule(client, adm, w.loan["id"])
    assert [r["status"] for r in after[:2]] == ["paid", "pending"]
    bal = balances(client, adm, w.loan["id"])
    assert Decimal(bal["total_paid"]) == first["total_due"] and Decimal(bal["due_to_date_outstanding"]) == 0
    assert Decimal(bal["outstanding_principal"]) == Decimal("7000") - first["principal_due"]
    assert bal["original_principal"] == "7000.0000" and bal["loan_status"] == "active"
    assert (
        len({bal["original_principal"], bal["outstanding_principal"], bal["total_debt"]}) == 3
    )  # three different things
    assert client.get(f"{LOANS}/{w.loan['id']}", headers=adm).json()["status"] == "active"  # debt remains
    assert {t: count(t) for t in LEGACY} == legacy_before  # the legacy payment/loan world was never touched


def test_field_payment_confirms_and_applies_immediately_without_any_cash_movement(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    clock(monkeypatch, days=35)
    first = schedule(client, adm, w.loan["id"])[0]
    cash_before, movements = session_balance(w.cash.session_id), count("cash_movements")
    out = pay(client, adm, w, first["total_due"], origin="field")
    assert out["origin"] == "field" and out["cash_session_id"] is None and out["cash_movement_id"] is None
    assert (
        count("cash_movements") == movements and session_balance(w.cash.session_id) == cash_before
    )  # custody stays with the collector
    assert out["receiving_branch_id"] == w.b["id"] and out["collected_by"] == tenant_a["admin_id"]
    assert (
        schedule(client, adm, w.loan["id"])[0]["status"] == "paid"
        and Decimal(balances(client, adm, w.loan["id"])["total_paid"]) == first["total_due"]
    )
    # a counter payment needs a session; a field payment must not carry one
    r = client.post(
        f"{LOANS}/{w.loan['id']}/payments",
        headers=adm,
        json=pbody(w, "1.00", origin="field") | {"cash_session_id": w.cash.session_id},
    )
    assert r.status_code == 422
    nosession = pbody(w, "1.00")
    del nosession["cash_session_id"]
    assert client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=nosession).status_code == 422


def test_partial_payments_apply_by_the_frozen_order_and_complete_an_obligation(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    clock(monkeypatch, days=35)
    first = schedule(client, adm, w.loan["id"])[0]
    half_interest = (first["interest_due"] / 2).quantize(Decimal("0.01"))
    p1 = pay(client, adm, w, half_interest)
    assert [(a["component"], Decimal(a["amount"])) for a in p1["applications"]] == [("interest", half_interest)]
    assert schedule(client, adm, w.loan["id"])[0]["status"] == "partially_paid"
    p2 = pay(client, adm, w, first["total_due"] - half_interest)  # the rest of the same obligation
    assert [a["component"] for a in p2["applications"]] == ["interest", "principal"]
    assert Decimal(p2["applications"][0]["amount"]) == first["interest_due"] - half_interest
    assert schedule(client, adm, w.loan["id"])[0]["status"] == "paid"
    assert [p["payment_number"] for p in client.get(f"{LOANS}/{w.loan['id']}/payments", headers=adm).json()] == [
        "PAG-000002",
        "PAG-000001",
    ]
    # no component is ever over-applied and every payment is exactly applied
    with SessionLocal() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM credit_payments p WHERE p.amount <> (SELECT sum(amount) FROM credit_payment_applications WHERE payment_id = p.id)"
                )
            ).scalar()
            == 0
        )
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM (SELECT a.obligation_id, a.component, sum(a.amount) s FROM credit_payment_applications a GROUP BY 1, 2) x "
                    "JOIN credit_loan_obligations o ON o.id = x.obligation_id "
                    "WHERE x.s > CASE x.component WHEN 'fee' THEN o.fees_due WHEN 'delinquency' THEN o.delinquency_due "
                    "WHEN 'interest' THEN o.interest_due ELSE o.principal_due END"
                )
            ).scalar()
            == 0
        )


def test_oldest_effective_due_first_across_obligations_and_never_a_future_one(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    clock(monkeypatch, days=65)  # obligations 1 and 2 are due; 3 is not
    rows = schedule(client, adm, w.loan["id"])
    due_now = rows[0]["total_due"] + rows[1]["total_due"]
    out = pay(client, adm, w, rows[0]["total_due"] + Decimal("10.00"))
    by_ob: dict[int, list] = {}
    with SessionLocal() as db:
        ids = {
            r[0]: r[1] for r in db.execute(text("SELECT id, sequence FROM credit_loan_obligations ORDER BY sequence"))
        }
    for a in out["applications"]:
        by_ob.setdefault(ids[a["obligation_id"]], []).append((a["component"], Decimal(a["amount"])))
    assert list(by_ob) == [1, 2]  # obligation 1 completely first, then 2: never 3
    assert by_ob[2] == [("interest", Decimal("10.00"))]
    st = [r["status"] for r in schedule(client, adm, w.loan["id"])]
    assert st[:3] == ["paid", "partially_paid", "pending"]
    # the rest of what is due, then ONE CENT more is refused: the third obligation (future) is never used as an advance
    pay(client, adm, w, due_now - rows[0]["total_due"] - Decimal("10.00"))
    r = client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "0.01"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "payment_exceeds_due_amount"
    assert [x["status"] for x in schedule(client, adm, w.loan["id"])][:3] == ["paid", "paid", "pending"]


def test_excess_over_the_due_to_date_amount_is_refused_without_side_effects(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    before = money_state()
    clock(monkeypatch, days=10)  # nothing is due yet
    r = client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "1.00"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "payment_exceeds_due_amount"
    clock(monkeypatch, days=35)
    first = schedule(client, adm, w.loan["id"])[0]
    over = client.post(
        f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, first["total_due"] + Decimal("0.01"))
    )
    assert over.status_code == 422 and over.json()["error"]["code"] == "payment_exceeds_due_amount"
    for bad in ("0", "-5", "10.001"):
        assert client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, bad)).status_code in (422,)
    assert (
        client.post(
            f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "10.5") | {"amount": 10.5}
        ).status_code
        == 422
    )  # float
    assert money_state() == before  # no payment, application, movement, number or audit


def test_loan_is_paid_only_when_every_obligation_is_settled(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    clock(monkeypatch, days=400)  # everything is due
    rows = schedule(client, adm, w.loan["id"])
    total = sum(r["total_due"] for r in rows)
    pay(client, adm, w, rows[0]["total_due"], origin="field")
    assert client.get(f"{LOANS}/{w.loan['id']}", headers=adm).json()["status"] == "active"
    last = pay(client, adm, w, total - rows[0]["total_due"], origin="field")
    assert Decimal(last["amount"]) == total - rows[0]["total_due"]
    loan = client.get(f"{LOANS}/{w.loan['id']}", headers=adm).json()
    assert loan["status"] == "paid" and all(r["status"] == "paid" for r in schedule(client, adm, w.loan["id"]))
    bal = balances(client, adm, w.loan["id"])
    assert (
        Decimal(bal["total_outstanding"]) == 0 and Decimal(bal["total_paid"]) == total and bal["loan_status"] == "paid"
    )
    again = client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "1.00", origin="field"))
    assert again.status_code == 409 and again.json()["error"]["code"] == "loan_not_payable"


def test_projection_can_be_rebuilt_from_scratch_from_obligations_and_applications(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    clock(monkeypatch, days=65)
    rows = schedule(client, adm, w.loan["id"])
    pay(client, adm, w, rows[0]["total_due"] + Decimal("5.00"), origin="field")
    pay(client, adm, w, "20.00")
    stored = [r["status"] for r in schedule(client, adm, w.loan["id"])]
    with SessionLocal() as db:
        _obs, views = ledger.views(db, w.loan["id"])
        rebuilt = [allocation.obligation_status(v) for v in views]
    assert rebuilt == stored == ["paid", "partially_paid"] + ["pending"] * 10
    with SessionLocal() as db:  # corrupt the projection by hand: the truth (applications) rebuilds it
        db.execute(text("UPDATE credit_loan_obligations SET status = 'pending'"))
        db.commit()
        _obs, views = ledger.views(db, w.loan["id"])
        assert [allocation.obligation_status(v) for v in views] == stored
        assert allocation.balances(views, date.today() + timedelta(days=65))["total_paid"] == rows[0][
            "total_due"
        ] + Decimal("25.00")
    with SessionLocal() as db:  # and there are no stored balance columns to decrement
        cols = {
            r[0]
            for r in db.execute(
                text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name IN "
                    "('credit_loans', 'credit_loan_obligations', 'credit_payments', 'credit_payment_applications')"
                )
            )
        }
    assert not [
        c for c in cols if "balance" in c or "outstanding" in c or c in ("paid_amount", "principal_paid", "total_paid")
    ]


def test_component_order_comes_from_the_frozen_contract_not_a_hard_coded_one(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    fee = {"code": "SEG", "name": "Seguro", "kind": "fixed", "amounts": {"DOP": "25"}, "timing": "per_installment"}
    raw = rules(
        fees=[fee],
        allocation={
            "order": ["principal", "interest", "fees", "delinquency"],
            "apply_by": "installment_then_component",
        },
    )
    w = loan_world(client, adm, tenant_a, raw=raw, code="PRD-ORD")
    clock(monkeypatch, days=35)
    first = schedule(client, adm, w.loan["id"])[0]
    assert first["fees_due"] == Decimal("25.00")
    small = (first["principal_due"] / 2).quantize(Decimal("0.01"))
    out = pay(client, adm, w, small)
    assert [(a["component"], Decimal(a["amount"])) for a in out["applications"]] == [
        ("principal", small)
    ]  # NOT fee first
    rest = pay(client, adm, w, first["total_due"] - small)
    assert [a["component"] for a in rest["applications"]] == ["principal", "interest", "fee"]
    assert schedule(client, adm, w.loan["id"])[0]["status"] == "paid"


def test_a_contract_with_an_undefined_apply_by_is_blocked_before_any_money(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    raw = rules(
        allocation={"order": ["fees", "delinquency", "interest", "principal"], "apply_by": "component_then_installment"}
    )
    w = loan_world(client, adm, tenant_a, raw=raw, code="PRD-CTI")
    clock(monkeypatch, days=35)
    before = money_state()
    r = client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "10.00"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "allocation_policy_blocked_by_spec"
    assert money_state() == before


# ================================ branch, currency, cash validation ==================================
def test_receiving_branch_is_explicit_and_a_loan_can_be_paid_at_another_branch(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    b2 = mk_branch(client, adm, "B2")
    cash_b2 = cash_for(tenant_a, b2["id"], balance="1000.00")
    clock(monkeypatch, days=35)
    first = schedule(client, adm, w.loan["id"])[0]
    missing = pbody(w, "5.00")
    del missing["receiving_branch_id"]
    assert (
        client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=missing).status_code == 422
    )  # never inferred
    assert (
        client.post(
            f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "5.00") | {"tenant_id": 1}
        ).status_code
        == 422
    )
    origin_cash = session_balance(w.cash.session_id)
    out = pay(client, adm, w, first["total_due"], branch=b2["id"], session=cash_b2.session_id)
    assert out["receiving_branch_id"] == b2["id"] and out["cash_session_id"] == cash_b2.session_id
    assert session_balance(cash_b2.session_id) == Decimal("1000.00") + first["total_due"]
    assert session_balance(w.cash.session_id) == origin_cash  # the origin branch gets NO fictitious movement
    loan = client.get(f"{LOANS}/{w.loan['id']}", headers=adm).json()
    assert (loan["origin_branch_id"], loan["disbursement_branch_id"]) == (
        w.b["id"],
        w.b["id"],
    )  # the loan keeps its branches
    ev = audit()[0].details
    assert ev["receiving_branch_id"] == b2["id"]


def test_cash_validation_session_branch_closed_session_currency_and_inactive_branch(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    b2 = mk_branch(client, adm, "B2")
    cash_b2 = cash_for(tenant_a, b2["id"])
    no_box = mk_branch(client, adm, "B3")  # created BEFORE the snapshot: admin set-up writes audit rows
    clock(monkeypatch, days=35)
    before = money_state()
    wrong = client.post(
        f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "5.00", session=cash_b2.session_id)
    )
    assert wrong.status_code == 409 and wrong.json()["error"]["code"] == "cash_unavailable"  # B2's session at branch B1
    assert (
        client.post(
            f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "5.00", branch=no_box["id"])
        ).status_code
        == 409
    )
    usd = client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "5.00") | {"currency_code": "USD"})
    assert usd.status_code == 422 and usd.json()["error"]["code"] == "currency_mismatch"
    with SessionLocal() as db:
        with pytest.raises(cash_port.CashCurrencyUnsupported):  # counter cash is RD$ only (BLOCKED_BY_EVIDENCE)
            cash_port.deposit(
                db,
                tenant_id=1,
                branch_id=1,
                session_id=1,
                amount=Decimal("1.00"),
                currency="USD",
                actor_user_id=1,
                kind=RECEIPT,
                reference="x",
                notes="x",
            )
        db.execute(text("UPDATE cash_sessions SET state = 'closing_review' WHERE id = :i"), {"i": w.cash.session_id})
        db.commit()
    assert (
        client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "5.00")).status_code == 409
    )  # closed session
    assert (
        client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "5.00", session=999999)).status_code
        == 409
    )
    with SessionLocal() as db:
        db.execute(text("UPDATE branches SET status = 'inactive' WHERE id = :i"), {"i": b2["id"]})
        db.commit()
    assert (
        client.post(
            f"{LOANS}/{w.loan['id']}/payments",
            headers=adm,
            json=pbody(w, "5.00", branch=b2["id"], session=cash_b2.session_id),
        ).status_code
        == 409
    )
    assert money_state() == before


def test_legacy_cash_reversal_cannot_touch_a_credit_payment_receipt(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    clock(monkeypatch, days=35)
    out = pay(client, adm, w, "10.00")
    with SessionLocal() as db:
        admin = db.get(User, tenant_a["admin_id"])
        admin.role = UserRole.admin  # the legacy cash code authorizes by role string
        db.commit()
        cmd = CashCommand(
            action="reverse",
            target_id=out["cash_movement_id"],
            branch_id=w.b["id"],
            session_id=w.cash.session_id,
            idempotency_key="legacy-reverse-key",
            notes="intento de reverso legacy",
            version=1,
        )
        with pytest.raises(HTTPException) as err:
            cash_service.command(db, admin, cmd)
        assert "no admite otro reverso" in str(err.value.detail)
        db.rollback()
    assert count("cash_movements", "reverses_id IS NOT NULL") == 0  # a reversal row never appeared


# ================================ authorization & tenant isolation ====================================
def test_payment_permissions_branch_scope_and_visibility(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    b2 = mk_branch(client, adm, "B2")
    cash_b2 = cash_for(tenant_a, b2["id"])
    clock(monkeypatch, days=35)
    first = schedule(client, adm, w.loan["id"])[0]
    # approve / formalize / disburse / read do NOT allow collecting
    other = user_hdr(
        client,
        sink,
        adm,
        tenant_a,
        "o@x.com",
        [
            "loans.disburse",
            "credit.applications.approve",
            "credit.applications.formalize",
            "loans.read",
            "payments.read",
        ],
    )
    assert client.post(f"{LOANS}/{w.loan['id']}/payments", headers=other, json=pbody(w, "5.00")).status_code == 403
    collector_b2 = user_hdr(
        client,
        sink,
        adm,
        tenant_a,
        "c2@x.com",
        ["payments.create", "payments.read"],
        scope="branch",
        branch_id=b2["id"],
    )
    # scoped to B2: may collect at B2 (a B1 loan), may not collect at B1
    assert (
        client.post(f"{LOANS}/{w.loan['id']}/payments", headers=collector_b2, json=pbody(w, "5.00")).status_code == 403
    )
    ok = client.post(
        f"{LOANS}/{w.loan['id']}/payments",
        headers=collector_b2,
        json=pbody(w, first["total_due"], branch=b2["id"], session=cash_b2.session_id),
    )
    assert ok.status_code == 200, ok.text
    pid = ok.json()["id"]
    assert client.get(f"{PAYMENTS}/{pid}", headers=collector_b2).status_code == 200  # its own receiving branch
    # visibility: receiving branch OR any branch of the loan; a stranger branch sees nothing
    b3 = mk_branch(client, adm, "B3")
    stranger = user_hdr(
        client, sink, adm, tenant_a, "s3@x.com", ["payments.read", "loans.read"], scope="branch", branch_id=b3["id"]
    )
    assert client.get(f"{PAYMENTS}/{pid}", headers=stranger).status_code == 403
    loan_branch = user_hdr(
        client, sink, adm, tenant_a, "b1@x.com", ["payments.read", "loans.read"], scope="branch", branch_id=w.b["id"]
    )
    assert (
        client.get(f"{PAYMENTS}/{pid}", headers=loan_branch).status_code == 200
    )  # the loan's own branch sees payments made elsewhere
    assert len(client.get(f"{LOANS}/{w.loan['id']}/payments", headers=loan_branch).json()) == 1
    assert client.get(f"{LOANS}/{w.loan['id']}/payments", headers=stranger).json() == []
    no_perm = user_hdr(client, sink, adm, tenant_a, "n@x.com", ["users.read"])
    assert client.get(f"{PAYMENTS}/{pid}", headers=no_perm).status_code == 403
    assert client.post(f"{LOANS}/{w.loan['id']}/payments", json=pbody(w, "1.00")).status_code == 401


def test_cross_tenant_payments_are_rejected_and_move_no_money(client, tenant_a, tenant_b, monkeypatch):
    adm_a, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    w = loan_world(client, adm_a, tenant_a)
    clock(monkeypatch, days=35)
    first = schedule(client, adm_a, w.loan["id"])[0]
    bb = mk_branch(client, adm_b, "BB")
    cash_bb = cash_for(tenant_b, bb["id"], balance="500.00")
    before = money_state()
    # tenant B, even with ITS OWN branch and session, cannot collect on tenant A's loan
    own = pbody(w, first["total_due"], branch=bb["id"], session=cash_bb.session_id)
    assert client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm_b, json=own).status_code == 404
    # tenant A cannot use tenant B's branch or session
    assert (
        client.post(
            f"{LOANS}/{w.loan['id']}/payments",
            headers=adm_a,
            json=pbody(w, "5.00", branch=bb["id"], session=cash_bb.session_id),
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{LOANS}/{w.loan['id']}/payments", headers=adm_a, json=pbody(w, "5.00", session=cash_bb.session_id)
        ).status_code
        == 409
    )
    assert session_balance(cash_bb.session_id) == Decimal("500.00") and money_state() == before
    pid = pay(client, adm_a, w, "10.00")["id"]
    for url in (f"{PAYMENTS}/{pid}", f"{LOANS}/{w.loan['id']}/payments", f"{LOANS}/{w.loan['id']}/balances"):
        assert client.get(url, headers=adm_b).status_code == 404, url
    with SessionLocal() as db:  # database level: a payment of tenant B on a loan of tenant A violates the composite FK
        with pytest.raises(DBAPIError, match="fk_credit_payments_loan_currency"):
            db.execute(
                text(
                    "INSERT INTO credit_payments (tenant_id, loan_id, payment_number, amount, currency_code, method, origin, status, "
                    "received_at, business_date, receiving_branch_id, collected_by, idempotency_key, request_digest, created_at) "
                    "VALUES (:t, :l, 'X-1', 1, 'DOP', 'cash', 'field', 'confirmed', now(), current_date, :b, 1, 'k-x-0000000001', 'd', now())"
                ),
                {"t": tenant_b["tenant_id"], "l": w.loan["id"], "b": bb["id"]},
            )
            db.commit()


# ================================ idempotency (replay / conflict) =====================================
def test_idempotent_replay_conflict_and_legitimate_second_payment(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    clock(monkeypatch, days=35)
    first = schedule(client, adm, w.loan["id"])[0]
    k = "idem-key-0000001"
    one = pay(client, adm, w, "100.00", k=k)
    after = money_state()
    again = pay(client, adm, w, "100.00", k=k)
    assert again["replayed"] is True and again["id"] == one["id"] and again["payment_number"] == one["payment_number"]
    assert again["applications"] == one["applications"]
    assert money_state() == after  # zero new payments, applications, movements, numbers or audit
    conflict = client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "101.00", k=k))
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
    other_origin = client.post(
        f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "100.00", origin="field", k=k)
    )
    assert other_origin.status_code == 409
    assert money_state() == after
    # the same payment under ANOTHER key is a legitimate additional payment (no arbitrary de-duplication)
    two = pay(client, adm, w, "100.00")
    assert two["id"] != one["id"] and two["payment_number"] == "PAG-000002"
    # external reference: unique per tenant when present
    pay(client, adm, w, "10.00", external_reference="LIBRO-77")
    dup = client.post(
        f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "10.00", external_reference="LIBRO-77")
    )
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "duplicate_external_reference"
    pay(client, adm, w, "10.00", external_reference="LIBRO-78")
    assert count("credit_payments") == 4 and first["total_due"] > Decimal("220")


# ================================ concurrency (real PostgreSQL connections) ===========================
def test_triple_payment_race_blocked_on_the_loan_lock_never_over_applies(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    clock(monkeypatch, days=35)
    first = schedule(client, adm, w.loan["id"])[0]
    lock = tracked_connect()
    lock.execute(text("SELECT id FROM credit_loans WHERE id = :i FOR UPDATE"), {"i": w.loan["id"]})
    ts = [
        in_thread(
            lambda: client.post(
                f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, first["total_due"], k=key())
            )
        )
        for _ in range(3)
    ]  # each one alone would settle the whole due amount
    wait_blocked_n(LOAN_LOCK, 3)  # all three queued behind the loan row, in the lock order's first step
    lock.rollback()
    lock.close()
    for t, _ in ts:
        t.join(60)
    codes = sorted(o["resp"].status_code for _, o in ts)
    assert codes == [200, 422, 422], codes
    assert {o["resp"].json()["error"]["code"] for _, o in ts if o["resp"].status_code == 422} == {
        "payment_exceeds_due_amount"
    }
    with SessionLocal() as db:
        assert db.execute(text("SELECT count(*) FROM credit_payments")).scalar() == 1
        applied = db.execute(text("SELECT sum(amount) FROM credit_payment_applications")).scalar()
    assert applied == first["total_due"] and money_state()["seq"] == 1  # nothing over-applied, one number consumed


def test_two_payments_competing_for_the_last_due_amount_and_a_replayed_key_race(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    clock(monkeypatch, days=35)
    first = schedule(client, adm, w.loan["id"])[0]
    big = (first["total_due"] * Decimal("0.65")).quantize(Decimal("0.01"))  # two of them cannot both fit
    barrier = threading.Barrier(3)

    def go(k):
        def run():
            barrier.wait(10)
            return client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, big, k=k))

        return run

    a, b = in_thread(go(key())), in_thread(go(key()))
    barrier.wait(10)
    a[0].join(60), b[0].join(60)
    assert sorted([a[1]["resp"].status_code, b[1]["resp"].status_code]) == [200, 422]
    with SessionLocal() as db:
        applied = db.execute(text("SELECT sum(amount) FROM credit_payment_applications")).scalar()
    assert applied == big <= first["total_due"]
    # the SAME key from two clients at once: one payment, one replay
    barrier2 = threading.Barrier(3)

    def same():
        barrier2.wait(10)
        return client.post(
            f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "10.00", k="same-key-00000001")
        )

    c, d = in_thread(same), in_thread(same)
    barrier2.wait(10)
    c[0].join(60), d[0].join(60)
    assert sorted(o["resp"].json()["replayed"] for _, o in (c, d)) == [False, True]
    assert count("credit_payments") == 2


def test_concurrent_counter_payments_on_the_same_cash_session_are_all_recorded_exactly_once(
    client, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    clock(monkeypatch, days=35)
    before = session_balance(w.cash.session_id)
    barrier = threading.Barrier(4)

    def go():
        barrier.wait(10)
        return client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "100.00", k=key()))

    ts = [in_thread(go) for _ in range(3)]
    barrier.wait(10)
    for t, _ in ts:
        t.join(60)
    assert [o["resp"].status_code for _, o in ts] == [200, 200, 200]
    assert sorted(o["resp"].json()["payment_number"] for _, o in ts) == ["PAG-000001", "PAG-000002", "PAG-000003"]
    assert session_balance(w.cash.session_id) == before + Decimal("300.00")  # no lost update on the session balance
    assert count("cash_movements", "kind = :k", k=RECEIPT) == 3


# ================================ atomicity & contract integrity ======================================
def test_counter_payment_is_atomic_a_failure_after_the_cash_inflow_rolls_everything_back(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    clock(monkeypatch, days=35)
    before, balance = money_state(), session_balance(w.cash.session_id)
    statuses = [r["status"] for r in schedule(client, adm, w.loan["id"])]
    original = pay_service._project

    def boom(*args, **kwargs):
        raise RuntimeError("falla despues del ingreso de caja")

    monkeypatch.setattr(pay_service, "_project", boom)
    body = pbody(w, "50.00", k="atomic-key-000001")
    with pytest.raises(RuntimeError):
        client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=body)
    assert (
        money_state() == before and session_balance(w.cash.session_id) == balance
    )  # no movement, payment, applications, number, audit
    assert [r["status"] for r in schedule(client, adm, w.loan["id"])] == statuses
    monkeypatch.setattr(pay_service, "_project", original)
    ok = client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=body)  # the very same command now succeeds
    assert ok.status_code == 200 and ok.json()["payment_number"] == "PAG-000001"


def test_contract_integrity_failure_blocks_before_any_money_moves(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    clock(monkeypatch, days=35)
    with engine.begin() as c:  # a storage-level corruption of the frozen contract (trigger off)
        c.execute(text("ALTER TABLE credit_formalizations DISABLE TRIGGER trg_credit_formalizations_guard"))
        c.execute(
            text(
                "UPDATE credit_formalizations SET contract_snapshot = jsonb_set(contract_snapshot, '{approved,amount}', '\"1.0000\"')"
            )
        )
        c.execute(text("ALTER TABLE credit_formalizations ENABLE TRIGGER trg_credit_formalizations_guard"))
    before, balance = money_state(), session_balance(w.cash.session_id)
    r = client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "10.00"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "contract_integrity_failed"
    assert money_state() == before and session_balance(w.cash.session_id) == balance


# ================================ time ================================================================
def test_business_date_uses_the_contract_timezone_not_the_utc_date(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world7(client, adm, tenant_a)
    # disburse on 1 Jan 2026 (Santo Domingo): obligations fall due on 1 Feb, 1 Mar, 1 Apr ...
    monkeypatch.setattr(loan_service, "now_utc", lambda: datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    w.loan = disburse(client, adm, w)
    rows = schedule(client, adm, w.loan["id"])
    assert [r["due_date"] for r in rows[:2]] == ["2026-02-01", "2026-03-01"]
    # 2026-03-01T02:00Z is still 28 Feb in Santo Domingo (UTC-4): only obligation #1 is due
    clock(monkeypatch, at=datetime(2026, 3, 1, 2, 0, tzinfo=UTC))
    assert balances(client, adm, w.loan["id"])["business_date"] == "2026-02-28"
    r = client.post(
        f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, rows[0]["total_due"] + Decimal("0.01"))
    )
    assert (
        r.status_code == 422 and r.json()["error"]["code"] == "payment_exceeds_due_amount"
    )  # the UTC date would have allowed it
    # three hours later it IS 1 March in Santo Domingo and obligation #2 is due
    clock(monkeypatch, at=datetime(2026, 3, 1, 5, 0, tzinfo=UTC))
    out = pay(client, adm, w, rows[0]["total_due"] + Decimal("0.01"))
    assert out["business_date"] == "2026-03-01" and out["received_at"].startswith("2026-03-01T05:00:00")


# ================================ purity, audit, immutability, invariants =============================
def test_every_get_performs_no_database_write(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    clock(monkeypatch, days=35)
    pid = pay(client, adm, w, "50.00")["id"]
    statements = []

    def before(conn, cursor, statement, parameters, context, executemany):
        if re.match(r"\s*(INSERT|UPDATE|DELETE)", statement, re.I):
            statements.append(statement[:90])

    event.listen(engine, "before_cursor_execute", before)
    try:
        for url in (
            LOANS,
            f"{LOANS}/{w.loan['id']}",
            f"{LOANS}/{w.loan['id']}/schedule",
            f"{LOANS}/{w.loan['id']}/balances",
            f"{LOANS}/{w.loan['id']}/payments",
            f"{PAYMENTS}/{pid}",
        ):
            assert client.get(url, headers=adm).status_code == 200, url
    finally:
        event.remove(engine, "before_cursor_execute", before)
    assert statements == []


def test_audit_trail_of_the_payment_and_silence_on_replays(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    clock(monkeypatch, days=35)
    first = schedule(client, adm, w.loan["id"])[0]
    k = "audit-key-0000001"
    out = pay(client, adm, w, first["total_due"], k=k)
    pay(client, adm, w, first["total_due"], k=k)  # replay
    events = audit()
    assert [e.event_type for e in events] == ["payment.confirmed"]  # the replay audited nothing
    e = events[0]
    assert e.actor_id == tenant_a["admin_id"] and e.tenant_id == tenant_a["tenant_id"] and e.correlation_id
    d = e.details
    assert (d["payment_id"], d["payment_number"], d["loan_id"]) == (out["id"], "PAG-000001", w.loan["id"])
    assert (d["amount"], d["currency_code"], d["origin"], d["method"]) == (out["amount"], "DOP", "counter", "cash")
    assert (
        d["receiving_branch_id"] == w.b["id"]
        and d["cash_session_id"] == w.cash.session_id
        and d["cash_movement_id"] == out["cash_movement_id"]
    )
    assert d["business_date"] == out["business_date"] and set(d["component_totals"]) == {"interest", "principal"}
    assert d["rules_digest"] == w.loan["rules_hash"] and d["contract_digest"] == w.loan["contract_hash"]
    assert "Perez" not in str(d) and "001-0000001-1" not in str(d)  # no customer identity
    with SessionLocal() as db:  # the cash side keeps its own audit
        assert db.execute(text("SELECT count(*) FROM cash_audit WHERE action = :a"), {"a": RECEIPT}).scalar() == 1


def test_payments_and_applications_are_immutable_and_the_database_enforces_the_invariants(
    client, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    clock(monkeypatch, days=35)
    out = pay(client, adm, w, "100.00")
    for sql in (
        "UPDATE credit_payments SET amount = amount + 1",
        "UPDATE credit_payments SET status = 'reversed'",
        "DELETE FROM credit_payments",
        "UPDATE credit_payment_applications SET amount = amount + 1",
        "DELETE FROM credit_payment_applications",
    ):
        with SessionLocal() as db:
            with pytest.raises(DBAPIError, match="immutable|append-only|violates"):
                db.execute(text(sql))
                db.commit()
    with SessionLocal() as db:
        first_ob = db.execute(
            text("SELECT id, principal_due FROM credit_loan_obligations ORDER BY sequence LIMIT 1")
        ).one()
    insert_payment = (
        "INSERT INTO credit_payments (tenant_id, loan_id, payment_number, amount, currency_code, method, origin, status, "
        "received_at, business_date, receiving_branch_id, collected_by, idempotency_key, request_digest, created_at) "
        "VALUES (:t, :l, :n, :a, 'DOP', 'cash', 'field', 'confirmed', now(), current_date, :b, 1, :k, 'd', now()) RETURNING id"
    )
    insert_app = (
        "INSERT INTO credit_payment_applications (tenant_id, payment_id, obligation_id, loan_id, component, amount, created_at) "
        "VALUES (:t, :p, :o, :l, 'principal', :a, now())"
    )
    base = {"t": tenant_a["tenant_id"], "l": w.loan["id"], "b": w.b["id"]}
    # (1) a payment whose applications do not add up to its amount cannot COMMIT
    with SessionLocal() as db:
        with pytest.raises(DBAPIError, match="must equal its amount"):
            pid = db.execute(text(insert_payment), base | {"n": "X-1", "a": 100, "k": "inv-key-000000001"}).scalar()
            db.execute(text(insert_app), base | {"p": pid, "o": first_ob.id, "a": 50})
            db.commit()
        db.rollback()
        with pytest.raises(DBAPIError, match="must equal its amount"):  # a payment with NO applications at all
            db.execute(text(insert_payment), base | {"n": "X-2", "a": 10, "k": "inv-key-000000002"})
            db.commit()
        db.rollback()
        # (2) the sum is right but a component is over-applied beyond its contractual amount: cannot commit either
        with pytest.raises(DBAPIError, match="over-applied"):
            over = first_ob.principal_due + 1
            pid = db.execute(text(insert_payment), base | {"n": "X-3", "a": over, "k": "inv-key-000000003"}).scalar()
            db.execute(text(insert_app), base | {"p": pid, "o": first_ob.id, "a": over})
            db.commit()
        db.rollback()
        # (3) an application must name an obligation of the SAME loan (composite FK)
        with pytest.raises(DBAPIError, match="fk_credit_payment_applications"):
            pid = db.execute(text(insert_payment), base | {"n": "X-4", "a": 5, "k": "inv-key-000000004"}).scalar()
            db.execute(text(insert_app), base | {"p": pid, "o": 999999, "a": 5})
            db.commit()
        db.rollback()
    assert count("credit_payments") == 1 and out["id"]


def test_balances_are_derived_never_stored_and_original_outstanding_and_debt_differ(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    clock(monkeypatch, days=35)
    first = schedule(client, adm, w.loan["id"])[0]
    pay(client, adm, w, first["total_due"])
    bal = balances(client, adm, w.loan["id"])
    rows = schedule(client, adm, w.loan["id"])
    assert Decimal(bal["outstanding_principal"]) == sum(r["principal_due"] for r in rows) - first["principal_due"]
    assert Decimal(bal["outstanding_interest"]) == sum(r["interest_due"] for r in rows) - first["interest_due"]
    assert Decimal(bal["total_outstanding"]) == sum(
        Decimal(bal[k])
        for k in ("outstanding_principal", "outstanding_interest", "outstanding_fees", "outstanding_delinquency")
    )
    assert len({bal["original_principal"], bal["outstanding_principal"], bal["total_debt"]}) == 3
    loan = client.get(f"{LOANS}/{w.loan['id']}", headers=adm).json()["balances"]
    assert loan["total_paid"] == bal["total_paid"] and loan["outstanding_principal"] == bal["outstanding_principal"]
    assert (
        rows[0]["paid_amount"] == bal["total_paid"] and rows[0]["outstanding_amount"] == "0.0000"
    )  # per obligation, derived


# ================================ migration ==========================================================
def test_migration_0010_upgrade_downgrade_reupgrade(scratch_db):
    from sqlalchemy import create_engine

    assert _alembic(scratch_db, "upgrade", "0009").returncode == 0
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
        up = _alembic(scratch_db, "upgrade", "0010")
        assert up.returncode == 0, up.stderr
        with eng.connect() as c:
            assert (
                c.execute(
                    text("SELECT count(*) FROM permissions WHERE code IN ('payments.read', 'payments.create')")
                ).scalar()
                == 2
            )
            assert (
                c.execute(
                    text(
                        "SELECT count(*) FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id "
                        "WHERE p.code IN ('payments.read', 'payments.create')"
                    )
                ).scalar()
                == 2
            )
            tables = {
                r[0] for r in c.execute(text("SELECT tablename FROM pg_tables WHERE tablename LIKE 'credit_payment%'"))
            }
            assert tables == {"credit_payments", "credit_payment_applications"}
            trg = {
                r[0] for r in c.execute(text("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'trg_credit_payment%'"))
            }
            assert trg == {
                "trg_credit_payments_guard",
                "trg_credit_payment_applications_guard",
                "trg_credit_payments_sum",
                "trg_credit_payment_applications_sum",
                "trg_credit_payment_applications_component",
            }
            deferred = {
                r[0]
                for r in c.execute(
                    text("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'trg_credit_payment%' AND tgdeferrable")
                )
            }
            assert deferred == {
                "trg_credit_payments_sum",
                "trg_credit_payment_applications_sum",
                "trg_credit_payment_applications_component",
            }
            idx = {
                r[0] for r in c.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'credit_payments'"))
            }
            assert "uq_credit_payments_external_reference" in idx
        down = _alembic(scratch_db, "downgrade", "0009")
        assert down.returncode == 0, down.stderr
        with eng.connect() as c:
            assert (
                c.execute(
                    text("SELECT count(*) FROM permissions WHERE code IN ('payments.read', 'payments.create')")
                ).scalar()
                == 0
            )
            assert c.execute(text("SELECT to_regclass('credit_payments')")).scalar() is None
            assert (
                c.execute(
                    text(
                        "SELECT count(*) FROM pg_proc WHERE proname IN ('credit_payment_sum_check', 'credit_payment_component_check')"
                    )
                ).scalar()
                == 0
            )
            assert c.execute(text("SELECT to_regclass('credit_loans')")).scalar() is not None  # T-007 untouched
        again = _alembic(scratch_db, "upgrade", "head")
        assert again.returncode == 0, again.stderr
        assert _alembic(scratch_db, "check").returncode == 0
    finally:
        eng.dispose()
