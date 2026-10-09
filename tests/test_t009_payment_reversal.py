"""T-009 Credit Payment Reversal tests (T009-*). PostgreSQL only.

A confirmed payment is compensated, never edited or deleted: payment (immutable) + reversal (append-only) + reversal
applications that mirror every original application -> NET ledger (applications - reversal applications).
Full reversal only. No partial reversal, adjustment, void, delinquency, payoff, bank or accounting here.
"""

import itertools
import re
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from app.core.db import SessionLocal, engine
from app.models.cash import CashSession
from app.models.user import User, UserRole
from app.modules.cash import port as cash_port
from app.modules.identity.models import SecurityEvent
from app.modules.loans import allocation, ledger
from app.modules.loans import payments as pay_service
from app.modules.loans import reversals as rev_service
from app.modules.loans import service as loan_service
from app.schemas.cash import CashCommand
from app.services import cash_service
from tests import pg_env  # noqa: F401  (must precede app imports)
from tests.cash_fixtures import close_session_now, open_v2_session
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
from tests.test_t006_origination import (  # noqa: F401
    _release_tracked_connections,
    count,
    in_thread,
    tracked_connect,
    user_hdr,
    wait_blocked_n,
)
from tests.test_t007_disbursement import cash_for, session_balance
from tests.test_t008_payments import LEGACY, LOAN_LOCK, balances, loan_world, pay, pbody, schedule

LOANS = f"{V2}/loans"
PAYMENTS = f"{V2}/payments"
RECEIPT, REVERSAL = "credit_payment_receipt", "credit_payment_reversal"
_keys = itertools.count(1)
KEEP = object()  # "use the default session of the world"


# ================================ helpers ============================================================
def key():
    return f"rev-key-{next(_keys):08d}"


def clock(monkeypatch, *, days=0):
    when = datetime.now(UTC) + timedelta(days=days)
    for mod in (pay_service, loan_service, rev_service):
        monkeypatch.setattr(mod, "now_utc", lambda when=when: when)
    return when


def own(session_id, user_id):
    """An open session of this user (the reversal takes the cash out of the executing cashier's drawer).

    T-021: the owner of a session is immutable (DB), so when ``session_id`` belongs to someone else the user gets their
    own open session (same balance, a free cash point of the same box). Returns the session id to use."""
    with SessionLocal() as db:
        s = db.get(CashSession, session_id)
        if s.cashier_id == user_id:
            return session_id
        new = open_v2_session(db, box_id=s.box_id, cashier_id=user_id, balance=s.balance)
        db.commit()
        return new.id


def add_session(box_id, cashier_id, balance="1000.00", state="open", opened_by=None):
    """An open v2 session (own cash point when the box's is busy); ``closed`` = opened and closed the T-021 way."""
    with SessionLocal() as db:
        s = open_v2_session(db, box_id=box_id, cashier_id=cashier_id, balance=balance, opened_by=opened_by)
        db.commit()
        sid = s.id
    if state == "closed":
        close_session_now(sid)
    else:
        assert state == "open", state
    return sid


def user_id(email):
    with SessionLocal() as db:
        return db.execute(text("SELECT id FROM users WHERE email = :e"), {"e": email}).scalar()


def rbody(w, session=KEEP, branch=None, k=None, reason="Cobro registrado por error", **extra):
    body = {"idempotency_key": k or key(), "reason": reason, "reversal_branch_id": branch or w.b["id"]}
    if session is KEEP:
        session = w.cash.session_id
    if session is not None:
        body["cash_session_id"] = session
    return body | extra


def post_rev(client, hdr, pid, w, **kw):
    return client.post(f"{PAYMENTS}/{pid}/reversals", headers=hdr, json=rbody(w, **kw))


def rev(client, hdr, pid, w, expect=200, **kw):
    r = post_rev(client, hdr, pid, w, **kw)
    assert r.status_code == expect, f"reverse: {r.status_code} {r.text}"
    return r.json()


def rstate():
    with SessionLocal() as db:
        out = {
            t: db.execute(text(f"SELECT count(*) FROM {t}")).scalar()
            for t in (
                "credit_payments",
                "credit_payment_applications",
                "credit_payment_reversals",
                "credit_payment_reversal_applications",
                "cash_movements",
                "cash_audit",
                "security_events",
            )
        }
        out["seq"] = db.execute(
            text("SELECT coalesce(max(last_value), 0) FROM tenant_sequences WHERE name = 'credit_payment_reversal'")
        ).scalar()
        out["sessions"] = [
            tuple(r) for r in db.execute(text("SELECT id, balance, state FROM cash_sessions ORDER BY id"))
        ]
        out["obligations"] = [
            tuple(r) for r in db.execute(text("SELECT id, status FROM credit_loan_obligations ORDER BY id"))
        ]
        out["loans"] = [tuple(r) for r in db.execute(text("SELECT id, status FROM credit_loans ORDER BY id"))]
    return out


def audit(prefix="payment.reversed"):
    with SessionLocal() as db:
        return [e for e in db.query(SecurityEvent).order_by(SecurityEvent.id) if e.event_type.startswith(prefix)]


def case(client, tenant_a, monkeypatch, days=35):
    """Admin + a disbursed loan + the admin owns the branch's open cash session + the clock moved to ``days``."""
    adm = admin_headers(client, tenant_a)
    w = loan_world(client, adm, tenant_a)
    clock(monkeypatch, days=days)
    own(w.cash.session_id, tenant_a["admin_id"])
    return adm, w, schedule(client, adm, w.loan["id"])


def loan_status(client, adm, w):
    return client.get(f"{LOANS}/{w.loan['id']}", headers=adm).json()["status"]


def statuses(client, adm, w):
    return [r["status"] for r in schedule(client, adm, w.loan["id"])]


def net_applied():
    """Independent SQL (not the ledger helper): net applied per obligation component."""
    with SessionLocal() as db:
        return {
            (r[0], r[1]): r[2]
            for r in db.execute(
                text(
                    "SELECT o.id, c.component, coalesce(a.s, 0) - coalesce(r.s, 0) FROM credit_loan_obligations o "
                    "CROSS JOIN (VALUES ('fee'), ('delinquency'), ('interest'), ('principal')) c(component) "
                    "LEFT JOIN (SELECT obligation_id, component, sum(amount) s FROM credit_payment_applications "
                    "GROUP BY 1, 2) a ON a.obligation_id = o.id AND a.component = c.component "
                    "LEFT JOIN (SELECT obligation_id, component, sum(amount) s FROM credit_payment_reversal_applications "
                    "GROUP BY 1, 2) r ON r.obligation_id = o.id AND r.component = c.component"
                )
            )
        }


# ================================ the economic model ==================================================
def test_counter_full_reversal_restores_cash_and_debt_and_keeps_the_history(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    first = rows[0]
    start = session_balance(w.cash.session_id)
    out = pay(client, adm, w, first["total_due"])
    assert session_balance(w.cash.session_id) == start + first["total_due"]
    legacy_before = {t: count(t) for t in LEGACY}
    original = client.get(f"{PAYMENTS}/{out['id']}", headers=adm).json()
    assert original["reversed"] is False and original["reversal_id"] is None

    r = rev(client, adm, out["id"], w, reason="El cliente pidio la devolucion")
    assert r["replayed"] is False and r["reversal_number"] == "REV-000001" and r["origin"] == "counter"
    assert (r["amount"], r["currency_code"], r["reversal_branch_id"]) == (out["amount"], "DOP", w.b["id"])
    assert r["reason"] == "El cliente pidio la devolucion" and r["reversed_by"] == tenant_a["admin_id"]
    assert r["cash_session_id"] == w.cash.session_id and r["cash_movement_id"] != out["cash_movement_id"]
    assert r["payment_id"] == out["id"] and r["payment_number"] == "PAG-000001"

    # CASH: exactly one compensating movement of the NEW kind, negative, linked to the original receipt
    assert session_balance(w.cash.session_id) == start
    with SessionLocal() as db:
        mv = db.execute(
            text("SELECT id, kind, amount, reverses_id, reference, session_id FROM cash_movements WHERE kind = :k"),
            {"k": REVERSAL},
        ).all()
        assert db.execute(text("SELECT count(*) FROM cash_movements WHERE kind = 'reversal'")).scalar() == 0
    assert len(mv) == 1 and mv[0].amount == -first["total_due"] and mv[0].id == r["cash_movement_id"]
    assert mv[0].reverses_id == out["cash_movement_id"] and mv[0].reference == "REV-000001"
    assert mv[0].session_id == w.cash.session_id

    # HISTORY: the original payment is untouched and still observable; "reversed" is DERIVED
    after = client.get(f"{PAYMENTS}/{out['id']}", headers=adm).json()
    assert after["reversed"] is True and after["reversal_id"] == r["id"] and after["reversal_number"] == "REV-000001"
    assert {k: v for k, v in after.items() if not k.startswith("reversal") and k != "reversed"} == {
        k: v for k, v in original.items() if not k.startswith("reversal") and k != "reversed"
    }  # same amount, status 'confirmed', applications, cash movement ...
    assert after["status"] == "confirmed" and len(after["applications"]) == 2
    got = client.get(f"{PAYMENTS}/{out['id']}/reversal", headers=adm).json()
    assert got["id"] == r["id"] and got["reversal_number"] == "REV-000001" and len(got["applications"]) == 2
    listed = client.get(f"{LOANS}/{w.loan['id']}/payments", headers=adm).json()
    assert [(p["id"], p["reversed"]) for p in listed] == [(out["id"], True)]
    assert count("credit_payments") == 1 and count("credit_payment_applications") == 2

    # NET LEDGER: the debt is back, exactly
    bal = balances(client, adm, w.loan["id"])
    assert Decimal(bal["total_paid"]) == 0 and Decimal(bal["due_to_date_outstanding"]) == first["total_due"]
    assert Decimal(bal["outstanding_principal"]) == Decimal("7000")
    assert statuses(client, adm, w)[0] == "pending" and loan_status(client, adm, w) == "past_due"
    assert {t: count(t) for t in LEGACY} == legacy_before  # no legacy payment / cash / capital row


def test_field_full_reversal_has_no_cash_movement_and_reopens_the_debt(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    first = rows[0]
    out = pay(client, adm, w, first["total_due"], origin="field")
    cash_before, movements = session_balance(w.cash.session_id), count("cash_movements")
    r = rev(client, adm, out["id"], w, session=None)
    assert r["origin"] == "field" and r["cash_session_id"] is None and r["cash_movement_id"] is None
    assert count("cash_movements") == movements and session_balance(w.cash.session_id) == cash_before
    assert Decimal(balances(client, adm, w.loan["id"])["total_paid"]) == 0 and statuses(client, adm, w)[0] == "pending"
    # a field reversal never carries a cash session; a counter reversal must
    out2 = pay(client, adm, w, "10.00", origin="field")
    bad = post_rev(client, adm, out2["id"], w, session=w.cash.session_id)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "reversal_cash_session_mismatch"
    out3 = pay(client, adm, w, "10.00")
    bad = post_rev(client, adm, out3["id"], w, session=None)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "reversal_cash_session_mismatch"
    assert count("credit_payment_reversals") == 1


def test_reversal_reprojects_paid_to_partially_paid_and_pending(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    first = rows[0]
    p1 = pay(client, adm, w, "10.00", origin="field")  # obligation 1: pending -> partially_paid
    p2 = pay(client, adm, w, first["total_due"] - Decimal("10.00"), origin="field")  # -> paid
    assert statuses(client, adm, w)[0] == "paid"
    rev(client, adm, p2["id"], w, session=None)
    assert statuses(client, adm, w)[0] == "partially_paid"  # paid -> partially_paid
    rev(client, adm, p1["id"], w, session=None)
    assert statuses(client, adm, w)[0] == "pending"  # partially_paid -> pending
    assert Decimal(balances(client, adm, w.loan["id"])["total_paid"]) == 0
    p3 = pay(client, adm, w, first["total_due"], origin="field")  # one payment settles it, its reversal -> pending
    assert statuses(client, adm, w)[0] == "paid"
    rev(client, adm, p3["id"], w, session=None)
    assert statuses(client, adm, w)[0] == "pending"


def test_a_paid_loan_reopens_to_active_and_the_debt_can_be_paid_again(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch, days=400)
    total = sum(r["total_due"] for r in rows)
    p1 = pay(client, adm, w, rows[0]["total_due"], origin="field")
    p2 = pay(client, adm, w, total - rows[0]["total_due"], origin="field")
    assert loan_status(client, adm, w) == "paid" and set(statuses(client, adm, w)) == {"paid"}
    rev(client, adm, p2["id"], w, session=None)
    assert (
        loan_status(client, adm, w) == "past_due"
    )  # T-010 D3: paid -> past_due directly (the reopened debt is already overdue at day 400)
    bal = balances(client, adm, w.loan["id"])
    assert Decimal(bal["total_outstanding"]) == total - rows[0]["total_due"] and bal["loan_status"] == "past_due"
    assert statuses(client, adm, w)[0] == "paid" and set(statuses(client, adm, w)[1:]) == {"pending"}
    # capacity reappears (net over-application): the very same amount can be paid again, and the loan is paid again
    p3 = pay(client, adm, w, total - rows[0]["total_due"], origin="field")
    assert loan_status(client, adm, w) == "paid" and p3["id"] not in (p1["id"], p2["id"])
    # a reversed payment cannot reopen anything twice; reversing the first one reopens obligation 1 only
    rev(client, adm, p1["id"], w, session=None)
    assert loan_status(client, adm, w) == "past_due" and statuses(client, adm, w)[0] == "pending"
    assert (
        client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "1.00", origin="field")).status_code
        == 200
    )


def test_the_reversal_mirrors_the_original_applications_exactly(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch, days=65)
    out = pay(client, adm, w, rows[0]["total_due"] + Decimal("10.00"))  # spans obligations 1 and 2
    assert len(out["applications"]) == 3
    r = rev(client, adm, out["id"], w)
    with SessionLocal() as db:
        orig = db.execute(
            text(
                "SELECT id, obligation_id, component, amount FROM credit_payment_applications "
                "WHERE payment_id = :p ORDER BY id"
            ),
            {"p": out["id"]},
        ).all()
        mirror = db.execute(
            text(
                "SELECT original_application_id, obligation_id, component, amount FROM credit_payment_reversal_applications "
                "WHERE reversal_id = :r ORDER BY original_application_id"
            ),
            {"r": r["id"]},
        ).all()
        total = db.execute(
            text("SELECT sum(amount) FROM credit_payment_reversal_applications WHERE reversal_id = :r"), {"r": r["id"]}
        ).scalar()
    assert [tuple(x) for x in orig] == [tuple(x) for x in mirror]  # same obligation, component and amount, one to one
    assert total == Decimal(out["amount"]) == Decimal(r["amount"])  # the sum is exactly the payment
    assert statuses(client, adm, w)[:3] == ["pending"] * 3


def test_later_payments_are_never_reallocated_and_the_oldest_debt_reappears(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch, days=65)
    p1 = pay(client, adm, w, rows[0]["total_due"], origin="field")  # covers installment 1
    p2 = pay(client, adm, w, rows[1]["total_due"], origin="field")  # covers installment 2
    assert statuses(client, adm, w)[:2] == ["paid", "paid"]
    with SessionLocal() as db:
        p2_rows = db.execute(
            text("SELECT * FROM credit_payment_applications WHERE payment_id = :p ORDER BY id"), {"p": p2["id"]}
        ).all()
    rev(client, adm, p1["id"], w, session=None)
    assert statuses(client, adm, w)[:2] == ["pending", "paid"]  # installment 1 owes again, installment 2 keeps P2
    with SessionLocal() as db:
        again = db.execute(
            text("SELECT * FROM credit_payment_applications WHERE payment_id = :p ORDER BY id"), {"p": p2["id"]}
        ).all()
        assert (
            db.execute(
                text("SELECT count(*) FROM credit_payment_reversal_applications WHERE payment_id = :p"), {"p": p2["id"]}
            ).scalar()
            == 0
        )
    assert [tuple(x) for x in again] == [tuple(x) for x in p2_rows]  # row-identical, ids included: P2 was not touched
    # the next payment applies OLDEST EFFECTIVE DUE FIRST, so it lands on the reopened installment 1
    p3 = pay(client, adm, w, "25.00", origin="field")
    with SessionLocal() as db:
        ob1 = db.execute(text("SELECT id FROM credit_loan_obligations WHERE sequence = 1")).scalar()
    assert {a["obligation_id"] for a in p3["applications"]} == {ob1}
    assert statuses(client, adm, w)[:2] == ["partially_paid", "paid"]


def test_the_external_reference_stays_occupied_after_a_reversal(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    ref = {"external_reference": "EXT-REF-0001"}
    p = client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "10.00", origin="field", **ref))
    assert p.status_code == 200, p.text
    rev(client, adm, p.json()["id"], w, session=None)
    dup = client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "10.00", origin="field", **ref))
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "duplicate_external_reference"
    fresh = {"external_reference": "EXT-REF-0002"}
    ok = client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "10.00", origin="field", **fresh))
    assert ok.status_code == 200
    assert pay(client, adm, w, "10.00", origin="field")["external_reference"] is None  # or no reference at all
    with SessionLocal() as db:  # the original row still holds it
        assert (
            db.execute(text("SELECT count(*) FROM credit_payments WHERE external_reference = 'EXT-REF-0001'")).scalar()
            == 1
        )


# ================================ cash sessions and branches ==========================================
def test_the_current_open_session_is_used_and_the_closed_original_is_never_reopened(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    out = pay(client, adm, w, rows[0]["total_due"])
    close_session_now(w.cash.session_id)  # the day ended: the original session is closed (count + handover)
    s2 = add_session(w.cash.box_id, tenant_a["admin_id"], balance="5000.00")
    with SessionLocal() as db:
        closed_before = tuple(
            db.execute(
                text("SELECT balance, state, version, closed_at FROM cash_sessions WHERE id = :i"),
                {"i": w.cash.session_id},
            ).one()
        )
    assert (
        post_rev(client, adm, out["id"], w, session=w.cash.session_id).status_code == 409
    )  # a closed session is refused
    r = rev(client, adm, out["id"], w, session=s2)
    assert r["cash_session_id"] == s2 and session_balance(s2) == Decimal("5000.00") - Decimal(out["amount"])
    with SessionLocal() as db:
        assert (
            tuple(
                db.execute(
                    text("SELECT balance, state, version, closed_at FROM cash_sessions WHERE id = :i"),
                    {"i": w.cash.session_id},
                ).one()
            )
            == closed_before
        )
        assert db.execute(text("SELECT session_id FROM cash_movements WHERE kind = :k"), {"k": REVERSAL}).scalar() == s2


def test_counter_reversal_session_validation_leaves_no_trace(client, sink, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    b2 = mk_branch(client, adm, "B2")
    cash_b2 = cash_for(tenant_a, b2["id"], balance="1000.00")
    own(cash_b2.session_id, tenant_a["admin_id"])
    out = pay(client, adm, w, rows[0]["total_due"])
    teller2 = user_hdr(client, sink, adm, tenant_a, "teller2@x.com", ["users.read"])  # a real, different cashier
    assert teller2
    other = add_session(w.cash.box_id, user_id("teller2@x.com"), balance="900.00", opened_by=tenant_a["admin_id"])
    unowned = add_session(w.cash.box_id, tenant_a["admin_id"], balance="900.00")
    with SessionLocal() as db, pytest.raises(DBAPIError):  # T-021: an ownerless session cannot exist (DB)
        db.execute(text("UPDATE cash_sessions SET cashier_id = NULL WHERE id = :i"), {"i": unowned})
        db.commit()
    closed = add_session(w.cash.box_id, tenant_a["admin_id"], balance="900.00", state="closed")
    poor = add_session(w.cash.box_id, tenant_a["admin_id"], balance="1.00")
    before = rstate()
    cases = [
        (cash_b2.session_id, 409, "cash_unavailable"),  # another branch's session
        (other, 403, "cash_session_not_owned"),
        (closed, 409, "cash_unavailable"),
        (poor, 409, "insufficient_cash"),
        (999999, 409, "cash_unavailable"),
    ]
    for sid, status, code in cases:
        r = post_rev(client, adm, out["id"], w, session=sid)
        assert (r.status_code, r.json()["error"]["code"]) == (status, code), (sid, r.text)
        assert rstate() == before  # no movement, reversal, application, REV number, audit or status change
    assert rev(client, adm, out["id"], w)["reversal_number"] == "REV-000001"  # the good session still works


def test_the_reversal_branch_must_be_the_receiving_branch_cross_branch_case(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    b2 = mk_branch(client, adm, "B2")
    cash_b2 = cash_for(tenant_a, b2["id"], balance="1000.00")
    own(cash_b2.session_id, tenant_a["admin_id"])
    out = pay(client, adm, w, rows[0]["total_due"], branch=b2["id"], session=cash_b2.session_id)  # B1 loan paid at B2
    before = rstate()
    for branch, session in ((w.b["id"], w.cash.session_id), (w.b["id"], cash_b2.session_id), (b2["id"] + 99, None)):
        r = post_rev(client, adm, out["id"], w, branch=branch, session=session)  # origin / managing / arbitrary branch
        assert r.status_code == 422 and r.json()["error"]["code"] == "reversal_branch_mismatch", r.text
    assert rstate() == before
    origin_cash = session_balance(w.cash.session_id)
    r = rev(client, adm, out["id"], w, branch=b2["id"], session=cash_b2.session_id)
    assert r["reversal_branch_id"] == b2["id"] and session_balance(cash_b2.session_id) == Decimal("1000.00")
    assert session_balance(w.cash.session_id) == origin_cash  # the loan's own branch gets no fictitious movement
    ev = audit()[0].details
    assert (ev["receiving_branch_id"], ev["reversal_branch_id"]) == (b2["id"], b2["id"])


# ================================ authorization and tenants ===========================================
def test_reversal_needs_its_own_permission_scoped_to_the_receiving_branch(client, sink, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    b2 = mk_branch(client, adm, "B2")
    cash_b2 = cash_for(tenant_a, b2["id"], balance="1000.00")
    out = pay(client, adm, w, rows[0]["total_due"], branch=b2["id"], session=cash_b2.session_id)
    creator = user_hdr(client, sink, adm, tenant_a, "cr@x.com", ["payments.create", "payments.read", "loans.disburse"])
    s_cr = own(
        cash_b2.session_id, user_id("cr@x.com")
    )  # ownership must NOT be what blocks them: only the permission may
    reader = user_hdr(client, sink, adm, tenant_a, "rd@x.com", ["payments.read"])
    s_rd = own(cash_b2.session_id, user_id("rd@x.com"))
    at_b1 = user_hdr(
        client,
        sink,
        adm,
        tenant_a,
        "b1@x.com",
        ["payments.reverse", "payments.read"],
        scope="branch",
        branch_id=w.b["id"],
    )
    s_b1 = own(cash_b2.session_id, user_id("b1@x.com"))  # T-021: each actor's own session exists before the snapshot
    before = rstate()
    assert post_rev(client, creator, out["id"], w, branch=b2["id"], session=s_cr).status_code == 403
    assert post_rev(client, reader, out["id"], w, branch=b2["id"], session=s_rd).status_code == 403
    assert post_rev(client, at_b1, out["id"], w, branch=b2["id"], session=s_b1).status_code == 403
    assert client.post(f"{PAYMENTS}/{out['id']}/reversals", json=rbody(w)).status_code == 401
    assert {k: v for k, v in rstate().items() if k != "security_events"} == {
        k: v for k, v in before.items() if k != "security_events"
    }
    at_b2 = user_hdr(
        client,
        sink,
        adm,
        tenant_a,
        "b2@x.com",
        ["payments.reverse", "payments.read"],
        scope="branch",
        branch_id=b2["id"],
    )
    s_b2 = own(cash_b2.session_id, user_id("b2@x.com"))
    r = rev(client, at_b2, out["id"], w, branch=b2["id"], session=s_b2)
    assert r["reversed_by"] == user_id("b2@x.com")
    assert client.get(f"{PAYMENTS}/{out['id']}/reversal", headers=at_b2).status_code == 200  # payments.read
    nope = user_hdr(client, sink, adm, tenant_a, "n@x.com", ["users.read"])
    assert client.get(f"{PAYMENTS}/{out['id']}/reversal", headers=nope).status_code == 403


def test_a_foreign_tenant_cannot_reverse_or_read_and_no_tenant_is_accepted_from_the_client(
    client, tenant_a, tenant_b, monkeypatch
):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    out = pay(client, adm, w, rows[0]["total_due"])
    other = admin_headers(client, tenant_b)
    before = rstate()
    r = client.post(f"{PAYMENTS}/{out['id']}/reversals", headers=other, json=rbody(w))
    assert r.status_code == 404
    assert client.get(f"{PAYMENTS}/{out['id']}/reversal", headers=other).status_code == 404
    assert post_rev(client, adm, out["id"], w, tenant_id=tenant_b["tenant_id"]).status_code == 422
    assert client.post(f"{PAYMENTS}/999999/reversals", headers=adm, json=rbody(w)).status_code == 404
    assert rstate() == before


def test_the_request_is_full_reversal_only_and_requires_a_reason(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    out = pay(client, adm, w, rows[0]["total_due"])
    before = rstate()
    for extra in ({"amount": "5.00"}, {"components": ["interest"]}, {"obligation_id": 1}, {"partial": True}):
        assert post_rev(client, adm, out["id"], w, **extra).status_code == 422  # no partial reversal, no amount
    for reason in ("", "ab", "  x ", "x" * 501):
        assert post_rev(client, adm, out["id"], w, reason=reason).status_code == 422
    no_reason = rbody(w)
    del no_reason["reason"]
    assert client.post(f"{PAYMENTS}/{out['id']}/reversals", headers=adm, json=no_reason).status_code == 422
    no_branch = rbody(w)
    del no_branch["reversal_branch_id"]
    assert client.post(f"{PAYMENTS}/{out['id']}/reversals", headers=adm, json=no_branch).status_code == 422
    assert rstate() == before
    assert rev(client, adm, out["id"], w, reason="x" * 500)["reason"] == "x" * 500


# ================================ idempotency and races ===============================================
def test_idempotent_replay_conflict_and_already_reversed(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    out = pay(client, adm, w, rows[0]["total_due"])
    k = "idem-rev-0000001"
    first = rev(client, adm, out["id"], w, k=k)
    after_first = rstate()
    again = rev(client, adm, out["id"], w, k=k)  # same key + same digest
    assert again["replayed"] is True and again["id"] == first["id"] and again["reversal_number"] == "REV-000001"
    assert rstate() == after_first  # no new REV number, movement, applications or audit
    conflict = post_rev(client, adm, out["id"], w, k=k, reason="Otro motivo distinto")
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
    twice = post_rev(client, adm, out["id"], w, k=key())  # another key on an already reversed payment
    assert twice.status_code == 409 and twice.json()["error"]["code"] == "payment_already_reversed"
    assert rstate() == after_first


def test_double_and_triple_reversal_races_produce_exactly_one_reversal(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    out = pay(client, adm, w, rows[0]["total_due"])
    start = session_balance(w.cash.session_id) - Decimal(out["amount"])
    lock = tracked_connect()
    lock.execute(text("SELECT id FROM credit_loans WHERE id = :i FOR UPDATE"), {"i": w.loan["id"]})
    ts = [in_thread(lambda: post_rev(client, adm, out["id"], w, k=key())) for _ in range(3)]
    wait_blocked_n(LOAN_LOCK, 3)  # all three queue behind the loan row: the first step of the lock order
    lock.rollback()
    lock.close()
    for t, _ in ts:
        t.join(60)
    codes = sorted(o["resp"].status_code for _, o in ts)
    assert codes == [200, 409, 409], codes
    assert {o["resp"].json()["error"]["code"] for _, o in ts if o["resp"].status_code == 409} == {
        "payment_already_reversed"
    }
    s = rstate()
    assert (s["credit_payment_reversals"], s["credit_payment_reversal_applications"], s["seq"]) == (1, 2, 1)
    assert count("cash_movements", "kind = :k", k=REVERSAL) == 1 and session_balance(w.cash.session_id) == start
    assert loan_status(client, adm, w) == "past_due" and statuses(client, adm, w)[0] == "pending"
    # two simultaneous clients, two different keys, another payment
    p2 = pay(client, adm, w, "20.00")
    barrier = threading.Barrier(3)

    def go():
        barrier.wait(10)
        return post_rev(client, adm, p2["id"], w, k=key())

    a, b = in_thread(go), in_thread(go)
    barrier.wait(10)
    a[0].join(60), b[0].join(60)
    assert sorted([a[1]["resp"].status_code, b[1]["resp"].status_code]) == [200, 409]
    assert rstate()["credit_payment_reversals"] == 2 and rstate()["seq"] == 2
    # the SAME key from two clients at once: one reversal, one replay
    p3 = pay(client, adm, w, "30.00")
    barrier2 = threading.Barrier(3)

    def same():
        barrier2.wait(10)
        return post_rev(client, adm, p3["id"], w, k="same-rev-key-0001")

    c, d = in_thread(same), in_thread(same)
    barrier2.wait(10)
    c[0].join(60), d[0].join(60)
    assert sorted(o["resp"].json()["replayed"] for _, o in (c, d)) == [False, True]
    assert rstate()["credit_payment_reversals"] == 3


def test_reversal_racing_a_payment_leaves_a_consistent_net_ledger(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    first = rows[0]
    p1 = pay(client, adm, w, first["total_due"], origin="field")
    lock = tracked_connect()
    lock.execute(text("SELECT id FROM credit_loans WHERE id = :i FOR UPDATE"), {"i": w.loan["id"]})
    r_t = in_thread(lambda: post_rev(client, adm, p1["id"], w, session=None))
    p_t = in_thread(
        lambda: client.post(
            f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, first["total_due"], origin="field")
        )
    )
    wait_blocked_n(LOAN_LOCK, 2)
    lock.rollback()
    lock.close()
    r_t[0].join(60), p_t[0].join(60)
    assert r_t[1]["resp"].status_code == 200
    paid_again = p_t[1]["resp"].status_code
    assert paid_again in (200, 422)  # 422 when the payment ran first: the debt was settled until the reversal
    expected = first["total_due"] if paid_again == 200 else Decimal(0)
    assert Decimal(balances(client, adm, w.loan["id"])["total_paid"]) == expected
    assert statuses(client, adm, w)[0] == ("paid" if paid_again == 200 else "pending")
    with (
        SessionLocal() as db
    ):  # no component is over-applied and none is negative (independent SQL, not the ledger helper)
        due = {
            (r[0], r[1]): r[2]
            for r in db.execute(
                text(
                    "SELECT o.id, c.component, CASE c.component WHEN 'fee' THEN o.fees_due WHEN 'delinquency' THEN o.delinquency_due "
                    "WHEN 'interest' THEN o.interest_due ELSE o.principal_due END FROM credit_loan_obligations o "
                    "CROSS JOIN (VALUES ('fee'), ('delinquency'), ('interest'), ('principal')) c(component)"
                )
            )
        }
    for k, net in net_applied().items():
        assert 0 <= net <= due[k], k


def test_the_last_payment_racing_a_reversal_ends_in_the_same_state_either_way(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch, days=400)
    total = sum(r["total_due"] for r in rows)
    p1 = pay(client, adm, w, total - Decimal("10.00"), origin="field")  # everything but the last 10.00
    lock = tracked_connect()
    lock.execute(text("SELECT id FROM credit_loans WHERE id = :i FOR UPDATE"), {"i": w.loan["id"]})
    r_t = in_thread(lambda: post_rev(client, adm, p1["id"], w, session=None))
    p_t = in_thread(
        lambda: client.post(f"{LOANS}/{w.loan['id']}/payments", headers=adm, json=pbody(w, "10.00", origin="field"))
    )
    wait_blocked_n(LOAN_LOCK, 2)
    lock.rollback()
    lock.close()
    r_t[0].join(60), p_t[0].join(60)
    assert (r_t[1]["resp"].status_code, p_t[1]["resp"].status_code) == (200, 200)  # both orders succeed
    bal = balances(client, adm, w.loan["id"])
    assert Decimal(bal["total_paid"]) == Decimal("10.00") and Decimal(bal["total_outstanding"]) == total - Decimal(
        "10.00"
    )
    assert (
        loan_status(client, adm, w) == "past_due"
    )  # the debt is overdue (day 400): the common projection ends in past_due either way
    assert Decimal(client.get(f"{LOANS}/{w.loan['id']}", headers=adm).json()["original_principal"]) == Decimal("7000")


def test_concurrent_counter_reversals_on_the_same_session_are_each_recorded_once(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    ps = [pay(client, adm, w, "100.00") for _ in range(3)]
    mid = session_balance(w.cash.session_id)
    barrier = threading.Barrier(4)

    def go(pid):
        def run():
            barrier.wait(10)
            return post_rev(client, adm, pid, w, k=key())

        return run

    ts = [in_thread(go(p["id"])) for p in ps]
    barrier.wait(10)
    for t, _ in ts:
        t.join(60)
    assert [o["resp"].status_code for _, o in ts] == [200, 200, 200]
    assert sorted(o["resp"].json()["reversal_number"] for _, o in ts) == ["REV-000001", "REV-000002", "REV-000003"]
    assert session_balance(w.cash.session_id) == mid - Decimal("300.00")  # no lost update on the session balance
    assert count("cash_movements", "kind = :k", k=REVERSAL) == 3


# ================================ atomicity ===========================================================
def _boom(*_a, **_k):
    raise RuntimeError("falla inyectada")


def _inject(monkeypatch, point):
    if point == "after_cash_withdrawal":
        real = cash_port.withdraw

        def after(*a, **k):
            real(*a, **k)
            raise RuntimeError("falla inyectada")

        monkeypatch.setattr(cash_port, "withdraw", after)
    elif point == "after_reversal_insert":
        monkeypatch.setattr(rev_service, "_mirror_applications", _boom)
    elif point == "after_applications":
        monkeypatch.setattr(rev_service.ledger, "project", _boom)
    elif point == "after_projection":
        monkeypatch.setattr(rev_service, "record_event", _boom)
    elif point == "after_audit":
        real_event = rev_service.record_event

        def after_event(*a, **k):
            real_event(*a, **k)
            raise RuntimeError("falla inyectada")

        monkeypatch.setattr(rev_service, "record_event", after_event)


@pytest.mark.parametrize(
    ("origin", "point"),
    [
        ("counter", "after_cash_withdrawal"),
        ("counter", "after_reversal_insert"),
        ("counter", "after_applications"),
        ("counter", "after_projection"),
        ("counter", "after_audit"),
        ("field", "after_reversal_insert"),
        ("field", "after_applications"),
        ("field", "after_projection"),
        ("field", "after_audit"),
    ],
)
def test_reversal_is_atomic_a_failure_after_any_step_rolls_everything_back(
    client, tenant_a, monkeypatch, origin, point
):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    out = pay(client, adm, w, rows[0]["total_due"], origin=origin)
    session = w.cash.session_id if origin == "counter" else None
    before = rstate()
    k = "atomic-rev-000001"
    with monkeypatch.context() as m:
        _inject(m, point)
        with pytest.raises(RuntimeError):
            client.post(f"{PAYMENTS}/{out['id']}/reversals", headers=adm, json=rbody(w, session=session, k=k))
    assert rstate() == before  # cash balance, movement, reversal, applications, REV number, statuses, audit
    again = client.post(f"{PAYMENTS}/{out['id']}/reversals", headers=adm, json=rbody(w, session=session, k=k))
    assert (
        again.status_code == 200
        and again.json()["reversal_number"] == "REV-000001"
        and again.json()["replayed"] is False
    )


def test_contract_integrity_failure_blocks_the_reversal_before_any_money_moves(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    out = pay(client, adm, w, rows[0]["total_due"])
    with engine.begin() as c:  # a storage-level corruption of the frozen contract (trigger off)
        c.execute(text("ALTER TABLE credit_formalizations DISABLE TRIGGER trg_credit_formalizations_guard"))
        c.execute(
            text(
                "UPDATE credit_formalizations SET contract_snapshot = jsonb_set(contract_snapshot, '{approved,amount}', '\"1.0000\"')"
            )
        )
        c.execute(text("ALTER TABLE credit_formalizations ENABLE TRIGGER trg_credit_formalizations_guard"))
    before = rstate()
    r = post_rev(client, adm, out["id"], w)
    assert r.status_code == 409 and r.json()["error"]["code"] == "contract_integrity_failed"
    assert rstate() == before


# ================================ legacy isolation ====================================================
def test_the_legacy_cash_reversal_cannot_reverse_any_of_the_new_movement_kinds(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    out = pay(client, adm, w, rows[0]["total_due"])
    r = rev(client, adm, out["id"], w)
    with SessionLocal() as db:
        disb = db.execute(text("SELECT id FROM cash_movements WHERE kind = 'credit_disbursement'")).scalar()
        admin = db.get(User, tenant_a["admin_id"])
        admin.role = UserRole.admin  # the legacy cash code authorizes by role string
        db.commit()
        for target in (out["cash_movement_id"], r["cash_movement_id"], disb):  # receipt, reversal, disbursement
            cmd = CashCommand(
                action="reverse",
                target_id=target,
                branch_id=w.b["id"],
                session_id=w.cash.session_id,
                idempotency_key=f"legacy-key-{target:08d}",
                notes="intento de reverso legacy",
                version=1,
            )
            with pytest.raises(HTTPException) as err:
                cash_service.command(db, admin, cmd)
            assert "no admite otro reverso" in str(err.value.detail)
            db.rollback()
    assert count("cash_movements", "kind = 'reversal'") == 0


def test_no_legacy_row_and_no_bank_or_accounting_row_is_written(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    out = pay(client, adm, w, rows[0]["total_due"])
    legacy = {t: count(t) for t in LEGACY}
    rev(client, adm, out["id"], w)
    assert {t: count(t) for t in LEGACY} == legacy
    with SessionLocal() as db:
        names = {r[0] for r in db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"))}
    assert not {
        n for n in names if any(x in n for x in ("ledger", "journal", "outbox", "gl_"))
    }  # no accounting / outbox


# ================================ database invariants =================================================
def raw_reversal(db, base, payment_id, **over):
    row = {
        "t": base["t"],
        "p": payment_id,
        "l": base["l"],
        "n": f"REV-RAW-{next(_keys)}",
        "a": base["a"],
        "o": "field",
        "b": base["b"],
        "k": key(),
        "by": base["by"],
        "cm": None,
        "cs": None,
    } | over
    return db.execute(
        text(
            "INSERT INTO credit_payment_reversals (tenant_id, payment_id, loan_id, reversal_number, amount, currency_code, "
            "origin, reason, reversed_by, reversed_at, business_date, reversal_branch_id, cash_session_id, cash_movement_id, "
            "idempotency_key, request_digest, created_at) VALUES (:t, :p, :l, :n, :a, 'DOP', :o, 'prueba', :by, now(), "
            "current_date, :b, :cs, :cm, :k, 'd', now()) RETURNING id"
        ),
        row,
    ).scalar()


def raw_mirror(db, base, reversal_id, app, **over):
    row = {
        "t": base["t"],
        "r": reversal_id,
        "p": app.payment_id,
        "a": app.id,
        "o": app.obligation_id,
        "l": base["l"],
        "c": app.component,
        "m": app.amount,
    } | over
    db.execute(
        text(
            "INSERT INTO credit_payment_reversal_applications (tenant_id, reversal_id, payment_id, original_application_id, "
            "obligation_id, loan_id, component, amount, created_at) VALUES (:t, :r, :p, :a, :o, :l, :c, :m, now())"
        ),
        row,
    )


def test_reversal_tables_are_immutable_and_the_database_enforces_full_mirror_and_cash_backing(
    client, tenant_a, monkeypatch
):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    b2 = mk_branch(client, adm, "B2")
    done = pay(client, adm, w, "40.00", origin="field")
    r = rev(client, adm, done["id"], w, session=None)
    target = pay(client, adm, w, "50.00", origin="field")  # NOT reversed: the raw attempts below aim at it
    counter = pay(client, adm, w, "60.00")
    base = {
        "t": tenant_a["tenant_id"],
        "l": w.loan["id"],
        "a": Decimal("50.0000"),
        "b": w.b["id"],
        "by": tenant_a["admin_id"],
    }
    for sql in (
        "UPDATE credit_payment_reversals SET reason = 'otra cosa'",
        "DELETE FROM credit_payment_reversals",
        "UPDATE credit_payment_reversal_applications SET amount = amount + 1",
        "DELETE FROM credit_payment_reversal_applications",
    ):
        with SessionLocal() as db:
            with pytest.raises(DBAPIError, match="immutable|append-only|violates"):
                db.execute(text(sql))
                db.commit()
    with SessionLocal() as db:
        t_apps = db.execute(
            text(
                "SELECT id, payment_id, obligation_id, component, amount FROM credit_payment_applications WHERE payment_id = :p ORDER BY id"
            ),
            {"p": target["id"]},
        ).all()
        c_apps = db.execute(
            text(
                "SELECT id, payment_id, obligation_id, component, amount FROM credit_payment_applications WHERE payment_id = :p ORDER BY id"
            ),
            {"p": counter["id"]},
        ).all()

        def attempt(match, fn):
            with pytest.raises(DBAPIError, match=match):
                fn()
                db.commit()
            db.rollback()

        # a PARTIAL reversal cannot exist: amount is bound to the payment's own (composite FK)
        attempt(
            "fk_credit_payment_reversals_payment", lambda: raw_reversal(db, base, target["id"], a=Decimal("10.0000"))
        )
        # a reversal at another branch cannot exist either
        attempt("fk_credit_payment_reversals_payment", lambda: raw_reversal(db, base, target["id"], b=b2["id"]))
        # a reversal without its applications cannot COMMIT
        attempt("must equal its amount", lambda: raw_reversal(db, base, target["id"]))
        # applications that do not mirror the original (amount / component / obligation) are impossible
        attempt(
            "fk_credit_payment_reversal_applications_original",
            lambda: raw_mirror(db, base, raw_reversal(db, base, target["id"]), t_apps[0], m=t_apps[0].amount - 1),
        )
        attempt(
            "fk_credit_payment_reversal_applications_original",
            lambda: raw_mirror(db, base, raw_reversal(db, base, target["id"]), t_apps[0], c="fee"),
        )

        # one reversal per payment (UNIQUE) and one reversal application per original application (UNIQUE)
        def dup():
            rid = raw_reversal(db, base, done["id"], a=Decimal("40.0000"))
            return rid

        attempt("uq_credit_payment_reversals_payment", dup)

        def mirror_twice():
            rid = raw_reversal(db, base, target["id"])
            for a in t_apps:
                raw_mirror(db, base, rid, a)
            raw_mirror(db, base, rid, t_apps[0])

        attempt("uq_credit_payment_reversal_applications_original|must equal", mirror_twice)

        # a COUNTER reversal must be backed by the compensating cash movement of the right kind / amount / link
        def unbacked():
            rid = raw_reversal(
                db,
                base,
                counter["id"],
                a=Decimal("60.0000"),
                o="counter",
                cs=counter["cash_session_id"],
                cm=counter["cash_movement_id"],  # the RECEIPT itself, not a compensation
            )
            for a in c_apps:
                raw_mirror(db, base, rid, a)

        attempt("not backed by its compensating cash movement", unbacked)
        # the good chain is accepted (field, full, exact mirror)
        rid = raw_reversal(db, base, target["id"])
        for a in t_apps:
            raw_mirror(db, base, rid, a)
        db.commit()
    assert count("credit_payment_reversals") == 2 and r["id"]


def test_the_net_over_application_trigger_allows_reappeared_capacity_but_never_more(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    first = rows[0]
    p = pay(client, adm, w, first["total_due"], origin="field")  # obligation 1 is fully paid
    rev(client, adm, p["id"], w, session=None)  # ... and reversed: net 0
    with SessionLocal() as db:
        ob = db.execute(text("SELECT id, principal_due FROM credit_loan_obligations WHERE sequence = 1")).one()
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
    with SessionLocal() as db:
        # the gross sum would be 2x principal_due, the NET sum is exactly principal_due: accepted
        pid = db.execute(text(insert_payment), base | {"n": "N-1", "a": ob.principal_due, "k": key()}).scalar()
        db.execute(text(insert_app), base | {"p": pid, "o": ob.id, "a": ob.principal_due})
        db.commit()
        with pytest.raises(DBAPIError, match="over-applied"):  # one cent more than the contractual amount: refused
            pid = db.execute(text(insert_payment), base | {"n": "N-2", "a": "0.01", "k": key()}).scalar()
            db.execute(text(insert_app), base | {"p": pid, "o": ob.id, "a": "0.01"})
            db.commit()
        db.rollback()


def test_net_statuses_can_be_rebuilt_from_scratch_from_obligations_applications_and_reversals(
    client, tenant_a, monkeypatch
):
    adm, w, rows = case(client, tenant_a, monkeypatch, days=65)
    p1 = pay(client, adm, w, rows[0]["total_due"] + Decimal("5.00"), origin="field")
    p2 = pay(client, adm, w, "20.00")
    rev(client, adm, p1["id"], w, session=None)
    stored = statuses(client, adm, w)
    assert stored[:3] == ["pending", "partially_paid", "pending"]  # p1 gone, p2 (20.00) now sits on obligation 1
    with SessionLocal() as db:
        _o, views = ledger.views(db, w.loan["id"])
        assert [allocation.obligation_status(v) for v in views] == stored
        db.execute(text("UPDATE credit_loan_obligations SET status = 'paid'"))  # corrupt the projection by hand
        db.commit()
        _o, views = ledger.views(db, w.loan["id"])
        assert [allocation.obligation_status(v) for v in views] == stored  # the net truth rebuilds it
    assert (
        Decimal(balances(client, adm, w.loan["id"])["total_paid"])
        == Decimal(p2["amount"])
        == sum(v for v in net_applied().values())
    )


# ================================ reads, audit ========================================================
def test_every_get_performs_no_database_write_after_a_reversal(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    pid = pay(client, adm, w, "50.00")["id"]
    rev(client, adm, pid, w)
    statements = []

    def before(conn, cursor, statement, parameters, context, executemany):
        if re.match(r"\s*(INSERT|UPDATE|DELETE)", statement, re.I):
            statements.append(statement[:90])

    event.listen(engine, "before_cursor_execute", before)
    try:
        for url in (
            f"{LOANS}/{w.loan['id']}",
            f"{LOANS}/{w.loan['id']}/schedule",
            f"{LOANS}/{w.loan['id']}/balances",
            f"{LOANS}/{w.loan['id']}/payments",
            f"{PAYMENTS}/{pid}",
            f"{PAYMENTS}/{pid}/reversal",
        ):
            assert client.get(url, headers=adm).status_code == 200, url
    finally:
        event.remove(engine, "before_cursor_execute", before)
    assert statements == []
    assert (
        client.get(f"{PAYMENTS}/{pay(client, adm, w, '5.00')['id']}/reversal", headers=adm).status_code == 404
    )  # not reversed


def test_audit_trail_of_the_reversal_and_silence_on_replays(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    out = pay(client, adm, w, rows[0]["total_due"])
    k = "audit-rev-000001"
    r = rev(client, adm, out["id"], w, k=k, reason="Pago duplicado por error")
    rev(client, adm, out["id"], w, k=k, reason="Pago duplicado por error")  # replay
    events = audit()
    assert [e.event_type for e in events] == ["payment.reversed"]  # the replay audited nothing
    e = events[0]
    assert e.actor_id == tenant_a["admin_id"] and e.tenant_id == tenant_a["tenant_id"] and e.correlation_id
    d = e.details
    assert (d["payment_id"], d["payment_number"], d["reversal_id"], d["reversal_number"]) == (
        out["id"],
        "PAG-000001",
        r["id"],
        "REV-000001",
    )
    assert (d["loan_id"], d["amount"], d["currency_code"], d["origin"]) == (
        w.loan["id"],
        out["amount"],
        "DOP",
        "counter",
    )
    assert (d["receiving_branch_id"], d["reversal_branch_id"], d["reason"]) == (
        w.b["id"],
        w.b["id"],
        "Pago duplicado por error",
    )
    assert d["cash_session_id"] == w.cash.session_id and d["original_cash_movement_id"] == out["cash_movement_id"]
    assert d["reversal_cash_movement_id"] == r["cash_movement_id"] and d["business_date"] == str(r["business_date"])
    assert set(d["component_totals"]) == {"interest", "principal"}
    assert d["rules_digest"] == w.loan["rules_hash"] and d["contract_digest"] == w.loan["contract_hash"]
    assert "Perez" not in str(d) and "001-0000001-1" not in str(d)  # no customer identity
    with SessionLocal() as db:  # the cash side keeps its own audit
        assert db.execute(text("SELECT count(*) FROM cash_audit WHERE action = :a"), {"a": REVERSAL}).scalar() == 1
    assert [x.event_type for x in audit("payment.")] == ["payment.confirmed", "payment.reversed"]


def test_the_payment_ledger_has_no_stored_balance_and_reads_only_net_amounts(client, tenant_a, monkeypatch):
    adm, w, rows = case(client, tenant_a, monkeypatch)
    out = pay(client, adm, w, "100.00")
    rev(client, adm, out["id"], w)
    with SessionLocal() as db:
        cols = {
            r[0]
            for r in db.execute(
                text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name IN "
                    "('credit_payment_reversals', 'credit_payment_reversal_applications')"
                )
            )
        }
        _o, views = ledger.views(db, w.loan["id"])
    assert not [c for c in cols if "balance" in c or "outstanding" in c]
    assert all(sum(v.applied.values()) == 0 for v in views)  # gross 100.00, reversed 100.00 -> net 0.00
    assert Decimal(balances(client, adm, w.loan["id"])["total_paid"]) == 0


# ================================ migration 0011 ======================================================
def _seed_company(eng):
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


def test_migration_0011_upgrade_clean_downgrade_reupgrade_and_alembic_check(scratch_db):
    from sqlalchemy import create_engine

    assert _alembic(scratch_db, "upgrade", "0010").returncode == 0
    eng = create_engine(scratch_db)
    try:
        _seed_company(eng)
        up = _alembic(scratch_db, "upgrade", "head")
        assert up.returncode == 0, up.stderr
        with eng.connect() as c:
            assert c.execute(text("SELECT count(*) FROM permissions WHERE code = 'payments.reverse'")).scalar() == 1
            assert (
                c.execute(text("SELECT is_sensitive FROM permissions WHERE code = 'payments.reverse'")).scalar() is True
            )
            assert (
                c.execute(
                    text(
                        "SELECT count(*) FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id WHERE p.code = 'payments.reverse'"
                    )
                ).scalar()
                == 1
            )
            tables = {
                r[0]
                for r in c.execute(
                    text("SELECT tablename FROM pg_tables WHERE tablename LIKE 'credit_payment_reversal%'")
                )
            }
            assert tables == {"credit_payment_reversals", "credit_payment_reversal_applications"}
            trg = {
                r[0]
                for r in c.execute(
                    text("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'trg_credit_payment_reversal%'")
                )
            }
            assert trg == {
                "trg_credit_payment_reversals_guard",
                "trg_credit_payment_reversal_applications_guard",
                "trg_credit_payment_reversals_sum",
                "trg_credit_payment_reversal_applications_sum",
                "trg_credit_payment_reversals_cash",
            }
            deferred = {
                r[0]
                for r in c.execute(
                    text(
                        "SELECT tgname FROM pg_trigger WHERE tgname LIKE 'trg_credit_payment_reversal%' AND tgdeferrable"
                    )
                )
            }
            assert deferred == {
                "trg_credit_payment_reversals_sum",
                "trg_credit_payment_reversal_applications_sum",
                "trg_credit_payment_reversals_cash",
            }
            fn = c.execute(text("SELECT prosrc FROM pg_proc WHERE proname = 'credit_payment_component_check'")).scalar()
            assert "credit_payment_reversal_applications" in fn  # the net version
        assert _alembic(scratch_db, "check").returncode == 0
        down = _alembic(scratch_db, "downgrade", "0010")  # no reversal data: a clean downgrade
        assert down.returncode == 0, down.stderr
        with eng.connect() as c:
            assert c.execute(text("SELECT count(*) FROM permissions WHERE code = 'payments.reverse'")).scalar() == 0
            assert c.execute(text("SELECT to_regclass('credit_payment_reversals')")).scalar() is None
            assert c.execute(text("SELECT to_regclass('credit_payments')")).scalar() is not None  # T-008 untouched
            assert (
                "credit_payment_reversal_applications"
                not in c.execute(
                    text("SELECT prosrc FROM pg_proc WHERE proname = 'credit_payment_component_check'")
                ).scalar()
            )
            assert (
                c.execute(
                    text(
                        "SELECT count(*) FROM pg_proc WHERE proname IN ('credit_reversal_sum_check', 'credit_reversal_cash_check')"
                    )
                ).scalar()
                == 0
            )
        again = _alembic(scratch_db, "upgrade", "head")
        assert again.returncode == 0, again.stderr
        assert _alembic(scratch_db, "check").returncode == 0
    finally:
        eng.dispose()


def test_downgrade_0011_is_refused_while_reversal_history_exists(scratch_db):
    from sqlalchemy import create_engine

    assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    eng = create_engine(scratch_db)
    try:
        _seed_company(eng)
        with eng.begin() as c:
            # replica role: the point is the downgrade guard, not rebuilding a whole loan chain row by row
            c.execute(text("SET session_replication_role = replica"))
            c.execute(
                text(
                    "INSERT INTO credit_payment_reversals (tenant_id, payment_id, loan_id, reversal_number, amount, currency_code, "
                    "origin, reason, reversed_by, reversed_at, business_date, reversal_branch_id, idempotency_key, request_digest, "
                    "created_at) SELECT id, 1, 1, 'REV-000001', 10, 'DOP', 'field', 'prueba', 1, now(), current_date, 1, "
                    "'downgrade-key-0001', 'd', now() FROM companies"
                )
            )
        assert _alembic(scratch_db, "downgrade", "0011").returncode == 0  # T-010 (0012) holds no economic rows: clean
        refused = _alembic(scratch_db, "downgrade", "0010")
        assert refused.returncode != 0 and "Cannot downgrade 0011" in refused.stderr
        with eng.connect() as c:  # nothing was dropped, nothing was falsified
            assert c.execute(text("SELECT count(*) FROM credit_payment_reversals")).scalar() == 1
            assert c.execute(text("SELECT count(*) FROM permissions WHERE code = 'payments.reverse'")).scalar() == 1
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0011"
    finally:
        eng.dispose()
