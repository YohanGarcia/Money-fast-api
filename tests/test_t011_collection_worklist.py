"""T-011 Credit Collection Worklist tests (T011-*). PostgreSQL only.

READ-ONLY list of loans with overdue NET debt: overdue = T-010's rule over the T-008/T-009 net ledger, each loan in ITS
frozen contract timezone, scope = ``collections.read`` on the MANAGING branch (tenant scope for loans without one), only
``customer_id`` (no PII), explicit sort + keyset cursor. No assignment, bucket, score, custody, rendition, accrual or write.
"""

import base64
import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import event, text

from app.core.db import SessionLocal, engine
from app.modules.loans import allocation
from app.modules.loans import overdue as overdue_service
from app.modules.loans import payments as pay_service
from app.modules.loans import reversals as rev_service
from app.modules.loans import service as loan_service
from app.modules.loans import worklist as worklist_service
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
from tests.test_t005_credit_products import flow
from tests.test_t005_engine import rules
from tests.test_t006_origination import (  # noqa: F401
    _release_tracked_connections,
    approve,
    count,
    post,
    review,
    user_hdr,
)
from tests.test_t007_disbursement import disburse
from tests.test_t008_payments import LEGACY, pay, schedule
from tests.test_t009_payment_reversal import audit, own, rev
from tests.test_t010_overdue_projection import (
    contractual_rows,
    dates,
    money_counts,
    stored,
    view,
    world10,
    write_listener,
)

W = f"{V2}/collections/overdue-loans"
ROOT = Path(__file__).resolve().parent.parent
EXPECTED_KEYS = {
    "loan_id",
    "loan_number",
    "customer_id",
    "managing_branch_id",
    "currency",
    "projected_status",
    "overdue_obligations",
    "days_overdue",
    "overdue_outstanding",
    "oldest_overdue_date",
    "next_due_date",
    "last_net_payment",
}


# ================================ helpers ============================================================
def local(day: date, tz="America/Santo_Domingo", hour=12, minute=0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(tz)).astimezone(UTC)


def clock(monkeypatch, when: datetime):
    for mod in (pay_service, loan_service, rev_service, overdue_service, worklist_service):
        monkeypatch.setattr(mod, "now_utc", lambda when=when: when)


def wl(client, hdr, expect=200, **params):
    r = client.get(W, headers=hdr, params={k: v for k, v in params.items() if v is not None})
    assert r.status_code == expect, f"worklist: {r.status_code} {r.text}"
    return r.json()


def ids(body):
    return [i["loan_id"] for i in body["items"]]


def mkloan(client, adm, tenant, tag, *, base, managing=None, amount="7000", product=None):
    """Another disbursed loan in the SAME branch / cash session of ``base`` (distinct customer, own idempotency key)."""
    customer = mk_customer(client, adm, identity=ident(given=f"Nombre{tag}", family=f"Apellido{tag}", doc=None))
    p, v = (product.p, product.v) if product else (base.p, base.v)
    w = SimpleNamespace(b=base.b, c=customer, p=p, v=v)
    extra = {"managing_branch_id": managing["id"]} if managing else {}
    d = review(client, adm, w, amount="10000", **extra)
    approve(client, adm, d, amount=amount)
    w.app_id, w.f = d["id"], post(client, adm, d["id"], "formalize")
    w.cash = base.cash
    w.loan = disburse(client, adm, w, key=f"disb-key-{tag}-0001")
    return w


def first_due(client, adm, w):
    return dates(client, adm, w)[0]


# ================================ pure helpers (no DB) ==============================================
def test_next_due_and_oldest_overdue_are_pure_derivations():
    d = date(2026, 3, 10)
    obs = [
        view(1, 1, d, principal=50),
        view(2, 2, d + timedelta(days=30), principal=50),
        view(3, 3, d + timedelta(days=60), applied={"principal": Decimal(50)}, principal=50),
    ]
    bd = d + timedelta(days=10)
    assert allocation.oldest_overdue_date(obs, bd) == d  # the EFFECTIVE date of the oldest overdue obligation
    assert allocation.next_due_date(obs, bd) == d + timedelta(days=30)  # the paid future one (#3) would never count
    assert allocation.next_due_date(obs, d) == d  # due today is NOT overdue: it is the next due
    assert allocation.oldest_overdue_date(obs, d) is None
    late = d + timedelta(days=31)
    assert allocation.next_due_date(obs, late) is None  # only overdue debt (+ a fully paid future obligation)
    paid_future = [
        view(1, 1, d, principal=50),
        view(2, 2, d + timedelta(days=30), applied={"principal": Decimal(50)}, principal=50),
    ]
    assert allocation.next_due_date(paid_future, d + timedelta(days=5)) is None


def test_the_cursor_is_parseable_validated_and_bound_to_its_ordering():
    import pytest

    from app.modules.loans.errors import InvalidCursor

    c = worklist_service.encode_cursor("overdue_outstanding", "desc", Decimal("12.5000"), 7)
    assert worklist_service.decode_cursor(c, "overdue_outstanding", "desc") == (Decimal("12.5000"), 7)
    for bad in (
        "",
        "%%%",
        "e30",
        base64.urlsafe_b64encode(b"[1,2]").decode(),
        base64.urlsafe_b64encode(b'{"s":"x"}').decode(),
    ):
        with pytest.raises(InvalidCursor):
            worklist_service.decode_cursor(bad, "overdue_outstanding", "desc")
    with pytest.raises(InvalidCursor):  # another sort / order never reuses it
        worklist_service.decode_cursor(c, "days_overdue", "desc")
    with pytest.raises(InvalidCursor):
        worklist_service.decode_cursor(c, "overdue_outstanding", "asc")
    tampered = base64.urlsafe_b64encode(
        json.dumps({"s": "days_overdue", "o": "asc", "v": "abc", "i": 1}).encode()
    ).decode()
    with pytest.raises(InvalidCursor):
        worklist_service.decode_cursor(tampered, "days_overdue", "asc")


# ================================ the overdue rule over real loans ====================================
def test_worklist_lists_only_loans_with_overdue_net_debt_and_the_t010_facts(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    rows = schedule(client, adm, w.loan["id"])
    d1, d2, d3 = (date.fromisoformat(r["due_date"]) for r in rows[:3])
    clock(monkeypatch, local(d1 - timedelta(days=3)))
    assert wl(client, adm)["items"] == []  # nothing due yet
    clock(monkeypatch, local(d1))
    assert wl(client, adm)["items"] == []  # due TODAY is not overdue
    clock(monkeypatch, local(d1 + timedelta(days=1)))
    body = wl(client, adm)
    assert len(body["items"]) == 1 and body["next_cursor"] is None
    item = body["items"][0]
    assert set(item) == EXPECTED_KEYS
    assert item["loan_id"] == w.loan["id"] and item["loan_number"] == w.loan["loan_number"]
    assert item["customer_id"] == w.c["id"] and item["managing_branch_id"] is None and item["currency"] == "DOP"
    assert (item["projected_status"], item["overdue_obligations"], item["days_overdue"]) == ("past_due", 1, 1)
    assert Decimal(item["overdue_outstanding"]) == rows[0]["total_due"]
    assert (item["oldest_overdue_date"], item["next_due_date"]) == (d1.isoformat(), d2.isoformat())
    assert item["last_net_payment"] is None
    clock(monkeypatch, local(d1 + timedelta(days=40)))  # obligations 1 and 2 are overdue; 3 is the next due
    item = wl(client, adm)["items"][0]
    assert (item["overdue_obligations"], item["days_overdue"]) == (2, 40)  # max days: from the OLDEST overdue due date
    assert Decimal(item["overdue_outstanding"]) == rows[0]["total_due"] + rows[1]["total_due"]
    assert (item["oldest_overdue_date"], item["next_due_date"]) == (d1.isoformat(), d3.isoformat())
    pay(client, adm, w, rows[0]["total_due"] + rows[1]["total_due"], origin="field")  # settle every overdue obligation
    assert wl(client, adm)["items"] == []  # fully settled overdue debt: gone from the worklist


def test_partial_payment_and_reversal_change_the_overdue_outstanding(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    first = schedule(client, adm, w.loan["id"])[0]
    clock(monkeypatch, local(date.fromisoformat(first["due_date"]) + timedelta(days=5)))
    p = pay(client, adm, w, "10.00", origin="field")
    assert Decimal(wl(client, adm)["items"][0]["overdue_outstanding"]) == first["total_due"] - Decimal("10.00")
    rev(client, adm, p["id"], w, session=None)
    assert Decimal(wl(client, adm)["items"][0]["overdue_outstanding"]) == first["total_due"]  # the reversal restores it


def test_the_stored_loan_status_is_never_a_filter(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    d1 = first_due(client, adm, w)
    clock(monkeypatch, local(d1 + timedelta(days=5)))
    assert stored(w) == "active"  # nobody assessed it: the stored status is stale ...
    body = wl(client, adm)
    assert ids(body) == [w.loan["id"]] and body["items"][0]["projected_status"] == "past_due"  # ... and it is listed
    with SessionLocal() as db:
        db.execute(text("UPDATE credit_loans SET status = 'past_due'"))
        db.commit()
    clock(monkeypatch, local(d1 - timedelta(days=2)))  # not overdue at this business date
    assert wl(client, adm)["items"] == []  # stored past_due but no derived overdue: excluded


def test_each_loan_uses_its_own_frozen_contract_timezone_around_midnight(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    base = world10(client, adm, tenant_a)  # America/Santo_Domingo (UTC-4)
    cal = {
        "source": "product",
        "timezone": "Pacific/Auckland",  # UTC+12/+13: a different calendar day for the same instant
        "non_working_weekdays": [5, 6],
        "holidays": [],
        "adjustment": "keep_original",
        "delinquency_start_basis": "effective_due_date",
        "accrual_basis": "contractual_dates",
    }
    nz_product = SimpleNamespace(p=None, v=None)
    nz_product.p, nz_product.v = flow(client, adm, "PRD-NZ", raw=rules(calendar=cal))
    nz = mkloan(client, adm, tenant_a, "Kiwi", base=base, product=nz_product)
    tzs = {base.loan["id"]: "America/Santo_Domingo", nz.loan["id"]: "Pacific/Auckland"}
    due = {base.loan["id"]: first_due(client, adm, base), nz.loan["id"]: first_due(client, adm, nz)}
    lo, hi = min(due.values()) - timedelta(days=1), max(due.values()) + timedelta(days=2)
    instants = []
    t = datetime(lo.year, lo.month, lo.day, tzinfo=UTC)
    while t.date() <= hi:
        instants.append(t)
        t += timedelta(hours=3)
    for lid, day in due.items():  # the exact local midnights (one minute before / at) of EACH timezone
        nxt = day + timedelta(days=1)
        instants += [local(nxt, tzs[lid], 0, 0) - timedelta(minutes=1), local(nxt, tzs[lid], 0, 0)]
    differing = 0
    for now in instants:
        clock(monkeypatch, now)
        expected = {lid for lid, d in due.items() if now.astimezone(ZoneInfo(tzs[lid])).date() > d}  # independent
        assert set(ids(wl(client, adm))) == expected, now
        differing += len(expected) == 1
    assert differing, "the two timezones must disagree on the overdue day for at least one instant"


def test_the_effective_due_date_counts_never_the_contractual_one(client, tenant_a, monkeypatch):
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
    w = world10(client, adm, tenant_a, raw=rules(calendar=cal), code="PRD-CAL-WL")
    first = schedule(client, adm, w.loan["id"])[0]
    contractual, effective = date.fromisoformat(first["contractual_date"]), date.fromisoformat(first["due_date"])
    assert effective > contractual
    clock(monkeypatch, local(effective))  # the effective date itself: not overdue (although past the contractual one)
    assert wl(client, adm)["items"] == []
    clock(monkeypatch, local(effective + timedelta(days=1)))
    item = wl(client, adm)["items"][0]
    assert (item["days_overdue"], item["oldest_overdue_date"]) == (1, effective.isoformat())  # from the EFFECTIVE date


def test_next_due_date_ignores_paid_future_obligations_and_returns_when_a_reversal_reopens_them(
    client, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    rows = schedule(client, adm, w.loan["id"])
    d1, d2, d3 = (date.fromisoformat(r["due_date"]) for r in rows[:3])
    clock(monkeypatch, local(d2 + timedelta(days=5)))  # obligations 1 and 2 are due; 3 is the next
    p1 = pay(client, adm, w, rows[0]["total_due"], origin="field")
    pay(client, adm, w, "10.00", origin="field")  # partial on obligation 2
    p3 = pay(client, adm, w, rows[1]["total_due"] - Decimal("10.00"), origin="field")  # obligation 2 completed
    assert wl(client, adm)["items"] == []
    rev(client, adm, p1["id"], w, session=None)  # obligation 1 is overdue again
    item = wl(client, adm)["items"][0]
    assert (item["overdue_obligations"], item["next_due_date"]) == (1, d3.isoformat())
    clock(monkeypatch, local(d2 - timedelta(days=5)))  # (test clock) obligation 2 is now a FUTURE obligation
    item = wl(client, adm)["items"][0]
    assert item["overdue_obligations"] == 1 and item["oldest_overdue_date"] == d1.isoformat()
    assert item["next_due_date"] == d3.isoformat()  # the fully paid future obligation 2 is ignored
    rev(client, adm, p3["id"], w, session=None)  # reopens obligation 2 (10.00 still paid: partial outstanding)
    assert wl(client, adm)["items"][0]["next_due_date"] == d2.isoformat()  # candidate again


# ================================ last net payment ====================================================
def test_last_net_payment_ignores_reversed_payments_and_includes_counter_and_field(
    client, tenant_a, tenant_b, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    d1 = first_due(client, adm, w)
    clock(monkeypatch, local(d1 + timedelta(days=5)))
    assert wl(client, adm)["items"][0]["last_net_payment"] is None
    own(w.cash.session_id, tenant_a["admin_id"])  # reversals of counter payments take cash out of the actor's session
    p1 = pay(client, adm, w, "10.00")  # counter
    lp = wl(client, adm)["items"][0]["last_net_payment"]
    assert (lp["payment_id"], lp["origin"], lp["amount"], lp["currency"]) == (p1["id"], "counter", "10.0000", "DOP")
    assert (lp["payment_number"], lp["business_date"]) == (p1["payment_number"], (d1 + timedelta(days=5)).isoformat())
    assert set(lp) == {"payment_id", "payment_number", "amount", "currency", "business_date", "origin"}
    clock(monkeypatch, local(d1 + timedelta(days=6)))
    p2 = pay(client, adm, w, "20.00", origin="field")
    lp = wl(client, adm)["items"][0]["last_net_payment"]
    assert (lp["payment_id"], lp["origin"], lp["amount"]) == (p2["id"], "field", "20.0000")
    clock(monkeypatch, local(d1 + timedelta(days=7)))
    p3 = pay(client, adm, w, "5.00")
    assert wl(client, adm)["items"][0]["last_net_payment"]["payment_id"] == p3["id"]
    rev(client, adm, p3["id"], w)  # the newest payment is reversed: it is EXCLUDED, the previous one becomes the latest
    assert wl(client, adm)["items"][0]["last_net_payment"]["payment_id"] == p2["id"]
    rev(client, adm, p2["id"], w, session=None)
    assert wl(client, adm)["items"][0]["last_net_payment"]["payment_id"] == p1["id"]
    rev(client, adm, p1["id"], w)
    assert wl(client, adm)["items"][0]["last_net_payment"] is None
    clock(monkeypatch, local(d1 + timedelta(days=8)))  # same business date: the newest (highest id) wins
    pay(client, adm, w, "1.00", origin="field")
    p5 = pay(client, adm, w, "2.00")
    assert wl(client, adm)["items"][0]["last_net_payment"]["payment_id"] == p5["id"]
    assert wl(client, admin_headers(client, tenant_b))["items"] == []  # another tenant sees nothing of it
    assert count("payments") == 0  # no legacy payment row involved


# ================================ authorization and scope =============================================
def test_collections_read_on_the_managing_branch_with_a_tenant_scope_fallback(
    client, sink, tenant_a, tenant_b, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    base = world10(client, adm, tenant_a)
    b_a, b_b = mk_branch(client, adm, "MGR-A"), mk_branch(client, adm, "MGR-B")
    la = mkloan(client, adm, tenant_a, "Alfa", base=base, managing=b_a)
    lb = mkloan(client, adm, tenant_a, "Beta", base=base, managing=b_b)
    ln = base  # no managing branch
    clock(monkeypatch, local(first_due(client, adm, base) + timedelta(days=5)))
    everything = {la.loan["id"], lb.loan["id"], ln.loan["id"]}
    assert set(ids(wl(client, adm))) == everything
    tenant_user = user_hdr(client, sink, adm, tenant_a, "t@x.com", ["collections.read"])
    assert set(ids(wl(client, tenant_user))) == everything  # tenant scope: sees every loan, NULL managing included
    at_a = user_hdr(client, sink, adm, tenant_a, "a@x.com", ["collections.read"], scope="branch", branch_id=b_a["id"])
    at_b = user_hdr(client, sink, adm, tenant_a, "b@x.com", ["collections.read"], scope="branch", branch_id=b_b["id"])
    assert ids(wl(client, at_a)) == [la.loan["id"]]  # only the loans MANAGED by its branch
    assert ids(wl(client, at_b)) == [lb.loan["id"]]  # not the other branch's loan, not the loan without managing branch
    at_origin = user_hdr(
        client, sink, adm, tenant_a, "o@x.com", ["collections.read"], scope="branch", branch_id=base.b["id"]
    )
    assert ids(wl(client, at_origin)) == []  # the ORIGIN / disbursement branch of every loan is not the managing one
    # the branch filter narrows, never widens
    assert ids(wl(client, at_a, branch_id=b_a["id"])) == [la.loan["id"]]
    wl(client, at_a, expect=403, branch_id=b_b["id"])
    assert ids(wl(client, tenant_user, branch_id=b_b["id"])) == [lb.loan["id"]]
    assert ids(wl(client, adm, branch_id=b_a["id"])) == [la.loan["id"]]
    other_tenant = admin_headers(client, tenant_b)
    wl(client, other_tenant, expect=404, branch_id=b_a["id"])  # a foreign branch id is a 404
    assert wl(client, other_tenant)["items"] == []  # and nothing of tenant A leaks
    # loans.read (or any other permission) is NOT enough; neither is the legacy collector role string
    loans_only = user_hdr(
        client, sink, adm, tenant_a, "l@x.com", ["loans.read", "payments.read", "loans.delinquency.assess"]
    )
    wl(client, loans_only, expect=403)
    legacy = user_hdr(client, sink, adm, tenant_a, "c@x.com", ["users.read"])
    with SessionLocal() as db:
        db.execute(text("UPDATE users SET role = 'collector' WHERE email = 'c@x.com'"))
        db.commit()
    wl(client, legacy, expect=403)
    assert client.get(W).status_code == 401


# ================================ filters, sorts, pagination ==========================================
def _key(sort, item):
    value = item[sort]
    return (
        Decimal(value)
        if sort == "overdue_outstanding"
        else (date.fromisoformat(value).toordinal() if sort == "oldest_overdue_date" else value)
    )


def test_filters_sorts_and_stable_keyset_pagination(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    a = world10(client, adm, tenant_a)
    b = mkloan(client, adm, tenant_a, "Beta", base=a)
    c = mkloan(client, adm, tenant_a, "Gama", base=a, amount="3000")
    d = mkloan(client, adm, tenant_a, "Delta", base=a, amount="3000")  # ties with c in days AND amount
    rows_b = schedule(client, adm, b.loan["id"])
    d1 = date.fromisoformat(rows_b[0]["due_date"])
    clock(monkeypatch, local(d1 + timedelta(days=40)))
    pay(client, adm, b, rows_b[0]["total_due"], origin="field")  # b: oldest overdue is now obligation 2
    full = wl(client, adm, limit=100)
    assert sorted(ids(full)) == sorted(x.loan["id"] for x in (a, b, c, d))
    # T-010 facts are the SAME numbers the single-loan balances give (no second formula)
    for item in full["items"]:
        bal = client.get(f"{V2}/loans/{item['loan_id']}/balances", headers=adm).json()
        assert (item["overdue_obligations"], item["days_overdue"]) == (
            bal["overdue_obligations"],
            bal["max_days_overdue"],
        )
        assert Decimal(item["overdue_outstanding"]) == Decimal(bal["overdue_outstanding"])
        assert item["projected_status"] == bal["projected_status"]
    default = wl(client, adm)  # default order: days_overdue DESC
    assert default["sort"] == "days_overdue" and default["order"] == "desc" and default["limit"] == 50
    assert [i["days_overdue"] for i in default["items"]] == sorted(
        (i["days_overdue"] for i in default["items"]), reverse=True
    )
    for sort in ("days_overdue", "overdue_outstanding", "oldest_overdue_date"):
        for order in ("asc", "desc"):
            whole = wl(client, adm, sort=sort, order=order, limit=100)["items"]
            expected = sorted(whole, key=lambda i: (_key(sort, i) * (1 if order == "asc" else -1), i["loan_id"]))
            assert [i["loan_id"] for i in whole] == [i["loan_id"] for i in expected], (sort, order)  # ties: loan_id ASC
            seen, cursor, pages = [], None, 0
            while True:
                page = wl(client, adm, sort=sort, order=order, limit=1, cursor=cursor)
                seen += ids(page)
                pages += 1
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            assert seen == [i["loan_id"] for i in whole] and len(set(seen)) == len(seen) == 4, (
                sort,
                order,
            )  # no dup/omission
    assert wl(client, adm, limit=2)["next_cursor"] is not None and wl(client, adm, limit=100)["next_cursor"] is None
    # filters
    days = {i["loan_id"]: i["days_overdue"] for i in full["items"]}
    threshold = days[b.loan["id"]] + 1
    assert b.loan["id"] not in ids(wl(client, adm, min_days_overdue=threshold))
    assert set(ids(wl(client, adm, min_days_overdue=threshold))) == {a.loan["id"], c.loan["id"], d.loan["id"]}
    assert ids(wl(client, adm, min_days_overdue=days[a.loan["id"]] + 1)) == []
    assert len(ids(wl(client, adm, currency="DOP"))) == 4 and ids(wl(client, adm, currency="USD")) == []
    # page size and validation
    for limit in (1, 50, 100):
        assert wl(client, adm, limit=limit)["limit"] == limit
    for bad in (
        {"limit": 0},
        {"limit": 101},
        {"sort": "score"},
        {"order": "sideways"},
        {"min_days_overdue": -1},
        {"cursor": "nope"},
    ):
        wl(client, adm, expect=422, **bad)
    page1 = wl(client, adm, sort="days_overdue", order="desc", limit=1)
    wl(
        client, adm, expect=422, sort="overdue_outstanding", order="desc", limit=1, cursor=page1["next_cursor"]
    )  # other sort
    wl(client, adm, expect=422, sort="days_overdue", order="asc", limit=1, cursor=page1["next_cursor"])  # other order
    # the dataset changes between pages: what was not touched is neither duplicated nor lost
    order_now = ids(wl(client, adm, limit=100))
    first_page = wl(client, adm, limit=1)
    removed = order_now[-1]
    gone = {a.loan["id"]: a, b.loan["id"]: b, c.loan["id"]: c, d.loan["id"]: d}[removed]
    rows_gone = schedule(client, adm, gone.loan["id"])
    pay(
        client,
        adm,
        gone,
        sum(r["total_due"] for r in rows_gone[:2]) - (rows_b[0]["total_due"] if gone is b else 0),
        origin="field",
    )
    rest, cursor = [], first_page["next_cursor"]
    while cursor:
        page = wl(client, adm, limit=1, cursor=cursor)
        rest += ids(page)
        cursor = page["next_cursor"]
    assert ids(first_page) + rest == [i for i in order_now if i != removed]


# ================================ read-only, no PII, no other package =================================
def test_the_worklist_is_read_only_pii_free_and_cheap(client, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    a = world10(client, adm, tenant_a)
    for tag in ("Beta", "Gama", "Delta"):  # created BEFORE the clock moves: the clock also dates the disbursement
        mkloan(client, adm, tenant_a, tag, base=a)
    d1 = first_due(client, adm, a)
    clock(monkeypatch, local(d1 + timedelta(days=10)))
    pay(client, adm, a, "10.00")  # a payment exists: the last-payment lookup runs too

    def selects(**params):
        stmts: list[str] = []

        def before(conn, cursor, statement, parameters, context, executemany):
            if re.match(r"\s*SELECT", statement, re.I):
                stmts.append(statement[:60])

        event.listen(engine, "before_cursor_execute", before)
        try:
            body = wl(client, adm, **params)
        finally:
            event.remove(engine, "before_cursor_execute", before)
        return len(stmts), body

    one_n, _ = selects(limit=1)
    four_n, body = selects(limit=100)
    assert len(body["items"]) == 4 and four_n == one_n  # no N+1: the queries do not grow with the rows
    assert four_n <= 12  # auth + loans + obligations/net applications (batched) + last payments (batched)
    # PII
    raw = json.dumps(body)
    for pii in ("Juan", "Perez", "001-0000001-1", "NombreBeta", "ApellidoGama", "@", "phone", "address", "document"):
        assert pii not in raw, pii
    assert set(body["items"][0]) == EXPECTED_KEYS
    assert not [k for k in EXPECTED_KEYS if re.search(r"bucket|score|priority|rank|risk|assign|collector|custody", k)]
    # zero writes, stored statuses untouched, no assessment, no cash, no legacy, no contractual change
    states = {t: count(t) for t in ("security_events",)}
    stored_before = loan_states()
    legacy_before, money_before, contract_before = {t: count(t) for t in LEGACY}, money_counts(), contractual_rows()

    import app.services.loan_service as legacy

    def boom(*_a, **_k):
        raise AssertionError("the legacy refresh_loan_state must never run")

    monkeypatch.setattr(legacy, "refresh_loan_state", boom)
    statements, stop = write_listener()
    try:
        for params in ({}, {"sort": "overdue_outstanding", "order": "asc"}, {"min_days_overdue": 1}, {"limit": 1}):
            wl(client, adm, **params)
    finally:
        stop()
    assert statements == []  # 0 INSERT / UPDATE / DELETE
    assert loan_states() == stored_before
    assert {t: count(t) for t in ("security_events",)} == states and audit("loan.delinquency_assessed") == []
    assert {t: count(t) for t in LEGACY} == legacy_before and money_counts() == money_before
    assert contractual_rows() == contract_before


def test_the_worklist_code_has_no_legacy_assignment_score_scheduler_or_new_dependency():
    import io
    import tokenize

    src = (ROOT / "app/modules/loans/worklist.py").read_text(encoding="utf-8")
    names = {t.string for t in tokenize.generate_tokens(io.StringIO(src).readline) if t.type == tokenize.NAME}
    for banned in (
        "refresh_loan_state",
        "LoanSettings",
        "loan_settings",
        "UserRole",
        "assigned_collector",
        "today",  # date.today(): the business date is each loan's frozen contract timezone
        "role",  # the legacy `users.role == 'collector'` string
        "CashSession",
        "contractual_date",
        "delinquency_starts_on",
    ):
        assert banned not in names, banned
    assert not [n for n in names if n.startswith(("cash_", "route", "assign"))]
    with SessionLocal() as db:
        names = {r[0] for r in db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"))}
    assert not {
        n
        for n in names
        if any(x in n for x in ("bucket", "score", "rendition", "promise", "collection"))
        or (n.startswith(("credit_", "collector")) and any(x in n for x in ("assign", "custody", "activity")))
    }
    py = (ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    assert not any(x in py for x in ("celery", "apscheduler", "dramatiq", "redis", "sqlalchemy-utils"))


# ================================ migration 0013 ======================================================
def test_migration_0013_adds_only_the_collections_read_permission_and_is_reversible(scratch_db):
    from sqlalchemy import create_engine

    assert _alembic(scratch_db, "upgrade", "0012").returncode == 0
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

        def shape(c):
            return (
                {r[0] for r in c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))},
                {
                    (r[0], r[1])
                    for r in c.execute(
                        text(
                            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'"
                        )
                    )
                },
            )

        with eng.connect() as c:
            before = shape(c)
        up = _alembic(scratch_db, "upgrade", "head")
        assert up.returncode == 0, up.stderr
        with eng.connect() as c:
            assert c.execute(text("SELECT count(*) FROM permissions WHERE code = 'collections.read'")).scalar() == 1
            assert (
                c.execute(text("SELECT is_sensitive FROM permissions WHERE code = 'collections.read'")).scalar()
                is False
            )
            assert (
                c.execute(
                    text(
                        "SELECT count(*) FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id "
                        "WHERE p.code = 'collections.read'"
                    )
                ).scalar()
                == 1
            )
            assert shape(c) == before  # NO new table, NO new column
        assert _alembic(scratch_db, "check").returncode == 0
        down = _alembic(scratch_db, "downgrade", "0012")
        assert down.returncode == 0, down.stderr
        with eng.connect() as c:
            assert c.execute(text("SELECT count(*) FROM permissions WHERE code = 'collections.read'")).scalar() == 0
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0012"
        again = _alembic(scratch_db, "upgrade", "head")
        assert again.returncode == 0, again.stderr
        assert _alembic(scratch_db, "check").returncode == 0
    finally:
        eng.dispose()


def loan_states():
    with SessionLocal() as db:
        return [tuple(r) for r in db.execute(text("SELECT id, status FROM credit_loans ORDER BY id"))]
