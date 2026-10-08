"""T-020 Field cash refund tests (T020-*). PostgreSQL only.

The PHYSICAL return to the customer of a REVERSED FIELD payment, 1:1 with its reversal, full amount, source derived from
custody: ``collector`` (the custodian hands it back; no Cash; terminal exit from collector custody, exclusive with an
accepted rendition) or ``branch_cash`` (one negative ``credit_field_refund`` movement from the refunder's own current
session). A declared rendition blocks it. Reversal != refund; discrepancy stays exact-only (reject = all cash returned).
"""

import json
import re
import threading
from decimal import Decimal
from pathlib import Path

from sqlalchemy import event, text

from app.core.db import SessionLocal, engine
from app.modules.field_custody import service as custody_service
from app.modules.loans import payments as pay_service
from tests import pg_env  # noqa: F401  (must precede app imports)
from tests.test_t001_foundation import _alembic, scratch_db  # noqa: F401
from tests.test_t002_identity import (  # noqa: F401  (fixtures + helpers shared with the earlier suites)
    admin_headers,
    client,
    fresh_db,
    sink,
    tenant_a,
    tenant_b,
)
from tests.test_t006_origination import (  # noqa: F401
    _release_tracked_connections,
    count,
    in_thread,
)
from tests.test_t007_disbursement import session_balance
from tests.test_t008_payments import PAYMENTS, clock, pay
from tests.test_t009_payment_reversal import audit
from tests.test_t010_overdue_projection import write_listener
from tests.test_t012_collection_assignment import _fresh_pool, mkuser  # noqa: F401
from tests.test_t019_field_cash_custody import (
    ACCEPT,
    KIND,
    READ,
    RENDER,
    cashier_session,
    code,
    decide,
    declare,
    fpay,
    key,
    outstanding,
    receipts,
    refused,
    sql,
    world,
)

ROOT = Path(__file__).resolve().parent.parent
V2 = "/api/v2"
REFUND = "cash.field_custody.refund"
REFUND_KIND = "credit_field_refund"
F = f"{V2}/cash/field-refunds"


# ================================ helpers ============================================================
def rworld(client, sink, tenant, monkeypatch):
    """T-019 world + a reverser and refund-capable collector / cashier."""
    x = world(client, sink, tenant, monkeypatch)
    x.rev_h, x.rev = mkuser(client, sink, x.adm, tenant, "rev@x.com", ["payments.reverse", "payments.read"], scope="branch", branch_id=x.b)
    x.rcol_h, x.rcol = mkuser(client, sink, x.adm, tenant, "rcol@x.com", ["payments.create", RENDER, REFUND, READ], scope="branch", branch_id=x.b)
    x.rcas_h, x.rcas = mkuser(client, sink, x.adm, tenant, "rcas@x.com", [ACCEPT, READ, REFUND], scope="branch", branch_id=x.b)
    x.rsess = cashier_session(tenant, x.w.cash.box_id, x.rcas, balance="1000.00")
    return x


def reverse(client, hdr, x, payment, expect=200):
    r = client.post(f"{PAYMENTS}/{payment['id']}/reversals", headers=hdr,
                    json={"idempotency_key": key("rv"), "reason": "error de cobro", "reversal_branch_id": x.b})
    assert r.status_code == expect, r.text
    return r.json()


def counter_reversal(client, x, tenant):
    """A counter payment and its T-009 reversal (cash withdrawn from the admin's own open session)."""
    counter = pay(client, x.adm, x.w, "10.00")
    own = cashier_session(tenant, x.w.cash.box_id, tenant["admin_id"], balance="100.00")
    r = client.post(f"{PAYMENTS}/{counter['id']}/reversals", headers=x.adm,
                    json={"idempotency_key": key("rv"), "reason": "error", "reversal_branch_id": x.b, "cash_session_id": own})
    assert r.status_code == 200, r.text
    return counter, r.json()


def refund(client, hdr, reversal_id, expect=200, k=None, reason="devuelto al cliente", **extra):
    r = client.post(f"{V2}/payment-reversals/{reversal_id}/field-refund", headers=hdr,
                    json={"idempotency_key": k or key("rf"), "reason": reason, **extra})
    assert r.status_code == expect, f"refund: {r.status_code} {r.text}"
    return r.json()


def movements(kind=REFUND_KIND):
    with SessionLocal() as db:
        return db.execute(text("SELECT id, session_id, amount, reverses_id FROM cash_movements WHERE kind = :k ORDER BY id"), {"k": kind}).all()


def custody(client, hdr, payment):
    return client.get(f"{PAYMENTS}/{payment['id']}/field-custody", headers=hdr).json()


def economic_state():
    with SessionLocal() as db:
        return {
            t: db.execute(text(f"SELECT md5(coalesce(string_agg(to_jsonb(x)::text, '' ORDER BY id), '')) FROM {t} x")).scalar()
            for t in ("credit_payments", "credit_payment_applications", "credit_payment_reversals",
                      "credit_payment_reversal_applications", "credit_loans", "credit_loan_obligations",
                      "credit_field_custody_receipts", "credit_field_renditions", "credit_field_rendition_items")
        }


# ================================ collector source ====================================================
def test_a_collector_refund_ends_custody_without_cash_and_the_receipt_can_never_be_rendered(client, sink, tenant_a, monkeypatch):
    x = rworld(client, sink, tenant_a, monkeypatch)
    never, rejected, cancelled = (fpay(client, x.rcol_h, x.w, a) for a in ("10.00", "11.00", "12.00"))
    r1 = declare(client, x.rcol_h, x.b, [rejected["id"]])
    decide(client, x.cas_h, r1["id"], "reject", reason="falta efectivo", counted_amount="10.00")  # reject = all cash returned
    r2 = declare(client, x.rcol_h, x.b, [cancelled["id"]])
    decide(client, x.rcol_h, r2["id"], "cancel")
    for p in (never, rejected, cancelled):  # S01 / S03 / S04 (F09 / F10)
        rv = reverse(client, x.rev_h, x, p)
        before, cash = economic_state(), session_balance(x.sess)
        out = refund(client, x.rcol_h, rv["id"])
        assert out["source_kind"] == "collector" and out["amount"] == p["amount"] and out["cash_session_id"] is None
        assert out["cash_movement_id"] is None and out["refunded_by"] == x.rcol and out["custodian_user_id"] == x.rcol
        assert out["refund_number"].startswith("RFD-") and out["replayed"] is False
        assert economic_state() == before and session_balance(x.sess) == cash  # nothing economic, no Cash
        c = custody(client, x.rcol_h, p)
        assert (c["physical_state"], c["tracking_status"], c["reversed"], c["refund_pending"]) == ("refunded_from_collector", "refunded", True, False)
        assert c["refund"]["id"] == out["id"] and p["id"] not in outstanding(client, x.rcol_h)
        assert code(declare(client, x.rcol_h, x.b, [p["id"]], expect=409)) == "receipt_refunded"  # never rendered after
    assert movements() == []
    # the custodian holds nothing any more: disabling now works (V2 disable equation)
    assert client.post(f"{V2}/users/{x.rcol}/disable", headers=x.adm).status_code == 200
    events = [e for e in audit("field_custody.refund") if e.event_type == "field_custody.refund_completed"]
    assert len(events) == 3 and {"reversed_by", "refunded_by", "source_kind"} <= set(events[0].details)
    assert events[0].details["reversed_by"] == x.rev and events[0].details["refunded_by"] == x.rcol


def test_a_declared_rendition_blocks_the_refund_until_it_is_cancelled_or_rejected(client, sink, tenant_a, monkeypatch):
    x = rworld(client, sink, tenant_a, monkeypatch)
    p = fpay(client, x.rcol_h, x.w, "20.00")
    r = declare(client, x.rcol_h, x.b, [p["id"]])
    rv = reverse(client, x.rev_h, x, p)  # S02: the rendition stays declared
    assert code(refund(client, x.rcol_h, rv["id"], expect=409)) == "receipt_in_declared_rendition"
    assert client.get(f"{V2}/cash/field-renditions/{r['id']}", headers=x.rcol_h).json()["state"] == "declared"  # no hidden cancel
    decide(client, x.rcol_h, r["id"], "cancel")
    assert refund(client, x.rcol_h, rv["id"])["source_kind"] == "collector"


# ================================ branch source =======================================================
def test_a_branch_refund_is_one_negative_movement_from_the_refunders_own_current_session(client, sink, tenant_a, monkeypatch):
    x = rworld(client, sink, tenant_a, monkeypatch)
    p1, p2 = fpay(client, x.rcol_h, x.w, "30.00"), fpay(client, x.rcol_h, x.w, "45.00")
    r = declare(client, x.rcol_h, x.b, [p1["id"], p2["id"]])
    acc = decide(client, x.cas_h, r["id"], "accept", cash_session_id=x.sess, counted_amount="75.00")  # one aggregate deposit
    rv1, rv2 = reverse(client, x.rev_h, x, p1), reverse(client, x.rev_h, x, p2)
    assert code(refund(client, x.rcas_h, rv1["id"], expect=422)) == "refund_session_required"
    assert code(refund(client, x.rcol_h, rv1["id"], expect=403, cash_session_id=x.rsess)) in ("cash_session_not_owned", "permission_denied")
    assert code(refund(client, x.rcas_h, rv1["id"], expect=403, cash_session_id=x.sess)) == "cash_session_not_owned"  # another's
    before, bal = economic_state(), session_balance(x.rsess)
    out = refund(client, x.rcas_h, rv1["id"], cash_session_id=x.rsess)
    assert out["source_kind"] == "branch_cash" and out["cash_session_id"] == x.rsess and out["custodian_user_id"] == x.rcol
    (m,) = movements()
    assert (m.id, m.session_id, m.amount, m.reverses_id) == (out["cash_movement_id"], x.rsess, Decimal("-30.00"), None)
    assert session_balance(x.rsess) == bal - Decimal("30.00") and economic_state() == before  # rendition / items untouched
    c = custody(client, x.cas_h, p1)
    assert (c["physical_state"], c["tracking_status"], c["refund_pending"]) == ("refunded_from_branch", "rendered", False)
    # the same aggregate rendition deposit backs a second, independent refund (no reverses_id coupling)
    out2 = refund(client, x.rcas_h, rv2["id"], cash_session_id=x.rsess)
    assert len(movements()) == 2 and out2["cash_movement_id"] != out["cash_movement_id"]
    assert client.get(f"{V2}/cash/field-renditions/{r['id']}", headers=x.cas_h).json()["state"] == "accepted"
    with SessionLocal() as db:
        assert db.execute(text("SELECT count(*) FROM credit_field_rendition_items WHERE released")).scalar() == 0
        assert db.execute(text("SELECT amount FROM cash_movements WHERE id = :m"), {"m": acc["cash_movement_id"]}).scalar() == Decimal("75.00")


def test_branch_refund_session_rules_and_insufficient_cash(client, sink, tenant_a, monkeypatch):
    x = rworld(client, sink, tenant_a, monkeypatch)
    p = fpay(client, x.rcol_h, x.w, "50.00")
    r = declare(client, x.rcol_h, x.b, [p["id"]])
    decide(client, x.cas_h, r["id"], "accept", cash_session_id=x.sess, counted_amount="50.00")
    rv = reverse(client, x.rev_h, x, p)
    poor = cashier_session(tenant_a, x.w.cash.box_id, x.rcas, balance="10.00")
    assert code(refund(client, x.rcas_h, rv["id"], expect=409, cash_session_id=poor)) == "insufficient_cash"
    closed = cashier_session(tenant_a, x.w.cash.box_id, x.rcas, state="closed", balance="1000.00")
    assert code(refund(client, x.rcas_h, rv["id"], expect=409, cash_session_id=closed)) == "cash_unavailable"
    assert movements() == [] and count("credit_field_refunds") == 0
    assert refund(client, x.rcas_h, rv["id"], cash_session_id=x.rsess)["cash_session_id"] == x.rsess  # explicit, own, open


# ================================ prerequisites and authorization ====================================
def test_refund_prerequisites_counter_pre_custody_permission_and_custodian(client, sink, tenant_a, tenant_b, monkeypatch):
    x = rworld(client, sink, tenant_a, monkeypatch)
    assert code(refund(client, x.rcol_h, 999999, expect=404)) == "reversal_not_found"  # F01
    _counter, crv = counter_reversal(client, x, tenant_a)
    withdrawals = count("cash_movements")
    assert code(refund(client, x.adm, crv["id"], expect=409)) == "field_refund_not_applicable"  # S06
    assert count("cash_movements") == withdrawals  # never a second withdrawal
    monkeypatch.setattr(pay_service, "create_receipt", lambda db, payment: None)
    old = fpay(client, x.rcol_h, x.w, "7.00")  # pre-T-019 shape: no receipt
    monkeypatch.undo()
    clock(monkeypatch, days=35)
    rv_old = reverse(client, x.rev_h, x, old)
    assert code(refund(client, x.rcol_h, rv_old["id"], expect=409)) == "pre_custody_not_refundable"  # S08 / F12
    p = fpay(client, x.rcol_h, x.w, "9.00")
    assert custody(client, x.rcol_h, p)["reversed"] is False  # S07: no reversal, no refund endpoint to call
    rv = reverse(client, x.rev_h, x, p)
    assert custody(client, x.rcol_h, p)["refund_pending"] is True
    other_h, _ = mkuser(client, sink, x.adm, tenant_a, "oc@x.com", ["payments.create", RENDER, REFUND], scope="branch", branch_id=x.b)
    assert code(refund(client, other_h, rv["id"], expect=403)) == "not_custodian"  # A02 / A07
    no_perm_h, _ = mkuser(client, sink, x.adm, tenant_a, "np@x.com", ["payments.reverse", RENDER, ACCEPT, READ], scope="branch", branch_id=x.b)
    assert code(refund(client, no_perm_h, rv["id"], expect=403)) == "permission_denied"  # reverse is not refund
    assert code(refund(client, x.col_h, rv["id"], expect=403)) == "permission_denied"
    assert code(refund(client, x.rcol_h, rv["id"], expect=422, cash_session_id=x.rsess)) == "refund_session_not_applicable"
    adm_b = admin_headers(client, tenant_b)
    assert code(refund(client, adm_b, rv["id"], expect=404)) == "reversal_not_found"  # A08
    for bad in ({"amount": "9.00"}, {"source_kind": "branch_cash"}, {"custodian_user_id": x.rcol}, {"payment_id": p["id"]}):
        assert client.post(f"{V2}/payment-reversals/{rv['id']}/field-refund", headers=x.rcol_h,
                           json={"idempotency_key": key("rf"), "reason": "devuelto", **bad}).status_code == 422
    assert client.post(f"{V2}/payment-reversals/{rv['id']}/field-refund", headers=x.rcol_h,
                       json={"idempotency_key": key("rf"), "reason": "  "}).status_code == 422
    # an inactive custodian cannot hand cash back (A09); custody stays visible
    sql("UPDATE users SET status = 'disabled' WHERE id = :u", u=x.rcol)
    with SessionLocal() as db:
        assert custody_service.has_open_custody(db, tenant_a["tenant_id"], x.rcol)
    sql("UPDATE users SET status = 'active' WHERE id = :u", u=x.rcol)
    assert refund(client, x.rcol_h, rv["id"])["source_kind"] == "collector"


def test_the_same_person_may_reverse_and_refund_when_holding_both_powers(client, sink, tenant_a, monkeypatch):
    x = rworld(client, sink, tenant_a, monkeypatch)
    both_col_h, _ = mkuser(client, sink, x.adm, tenant_a, "bc@x.com", ["payments.create", "payments.reverse", RENDER, REFUND], scope="branch", branch_id=x.b)
    p = fpay(client, both_col_h, x.w, "6.00")
    rv = reverse(client, both_col_h, x, p)  # H10: no hard reverser != refunder rule
    assert refund(client, both_col_h, rv["id"])["source_kind"] == "collector"
    both_cas_h, both_cas = mkuser(client, sink, x.adm, tenant_a, "bk@x.com", ["payments.reverse", ACCEPT, REFUND, READ], scope="branch", branch_id=x.b)
    sess = cashier_session(tenant_a, x.w.cash.box_id, both_cas, balance="100.00")
    p2 = fpay(client, x.rcol_h, x.w, "8.00")
    r = declare(client, x.rcol_h, x.b, [p2["id"]])
    decide(client, both_cas_h, r["id"], "accept", cash_session_id=sess, counted_amount="8.00")
    rv2 = reverse(client, both_cas_h, x, p2)
    assert refund(client, both_cas_h, rv2["id"], cash_session_id=sess)["source_kind"] == "branch_cash"


# ================================ idempotency and concurrency =========================================
def test_replays_never_duplicate_and_one_reversal_has_one_refund(client, sink, tenant_a, monkeypatch):
    x = rworld(client, sink, tenant_a, monkeypatch)
    p = fpay(client, x.rcol_h, x.w, "40.00")
    r = declare(client, x.rcol_h, x.b, [p["id"]])
    decide(client, x.cas_h, r["id"], "accept", cash_session_id=x.sess, counted_amount="40.00")
    rv = reverse(client, x.rev_h, x, p)
    k = key("rf")
    first = refund(client, x.rcas_h, rv["id"], k=k, cash_session_id=x.rsess)
    again = refund(client, x.rcas_h, rv["id"], k=k, cash_session_id=x.rsess)
    assert again["replayed"] is True and again["id"] == first["id"] and len(movements()) == 1  # F07
    assert code(refund(client, x.rcas_h, rv["id"], k=k, expect=409, reason="otro motivo", cash_session_id=x.rsess)) == "idempotency_conflict"
    assert code(refund(client, x.rcas_h, rv["id"], expect=409, cash_session_id=x.rsess)) == "already_refunded"  # F02
    assert len(movements()) == 1 and count("credit_field_refunds") == 1
    assert len([e for e in audit("field_custody.refund_completed")]) == 1  # replays add no audit
    # two concurrent refunds of one reversal (collector source): one wins
    p2 = fpay(client, x.rcol_h, x.w, "5.00")
    rv2 = reverse(client, x.rev_h, x, p2)
    barrier = threading.Barrier(2)

    def go():
        barrier.wait()
        return client.post(f"{V2}/payment-reversals/{rv2['id']}/field-refund", headers=x.rcol_h,
                           json={"idempotency_key": key("rf"), "reason": "devuelto"})

    ts = [in_thread(go) for _ in range(2)]
    for t, _ in ts:
        t.join(60)
    assert sorted(o["resp"].status_code for _, o in ts) == [200, 409]
    assert count("credit_field_refunds", "reversal_id = :r", r=rv2["id"]) == 1


def test_a_collector_refund_and_a_declaration_of_the_same_receipt_never_both_win(client, sink, tenant_a, monkeypatch):
    x = rworld(client, sink, tenant_a, monkeypatch)
    for _ in range(3):
        p = fpay(client, x.rcol_h, x.w, "3.00")
        rv = reverse(client, x.rev_h, x, p)
        barrier = threading.Barrier(2)

        def do_refund(rv=rv):
            barrier.wait()
            return client.post(f"{V2}/payment-reversals/{rv['id']}/field-refund", headers=x.rcol_h,
                               json={"idempotency_key": key("rf"), "reason": "devuelto"})

        def do_declare(p=p):
            barrier.wait()
            return client.post(f"{V2}/cash/field-renditions", headers=x.rcol_h,
                               json={"idempotency_key": key(), "receiving_branch_id": x.b, "payment_ids": [p["id"]]})

        (t1, o1), (t2, o2) = in_thread(do_refund), in_thread(do_declare)
        t1.join(60)
        t2.join(60)
        codes = (o1["resp"].status_code, o2["resp"].status_code)
        assert sorted(codes) == [200, 409], codes  # exactly one way out of collector custody
        refunded = count("credit_field_refunds", "payment_id = :p", p=p["id"])
        with SessionLocal() as db:
            live = db.execute(text("SELECT count(*) FROM credit_field_rendition_items WHERE payment_id = :p AND NOT released"), {"p": p["id"]}).scalar()
        assert refunded + live == 1


# ================================ database invariants =================================================
def test_the_database_enforces_every_refund_invariant(client, sink, tenant_a, tenant_b, monkeypatch):
    x = rworld(client, sink, tenant_a, monkeypatch)
    t = tenant_a["tenant_id"]
    p = fpay(client, x.rcol_h, x.w, "20.00")
    rv = reverse(client, x.rev_h, x, p)
    (rc,) = receipts(p["id"])
    ins = ("INSERT INTO credit_field_refunds (tenant_id, refund_number, payment_id, reversal_id, receipt_id, loan_id, receiving_branch_id, "
           "origin, currency_code, amount, source_kind, custodian_user_id, refunded_by, cash_session_id, cash_movement_id, reason, refunded_at, "
           "idempotency_key, request_digest) VALUES (:t, :n, :p, :rv, :rc, :l, :b, :o, :c, :a, :s, :u, :by, :cs, :cm, 'motivo', now(), :k, 'd')")
    base = dict(t=t, n="RFD-T1", p=p["id"], rv=rv["id"], rc=rc.id, l=x.w.loan["id"], b=x.b, o="field", c="DOP", a=Decimal("20.00"),
                s="collector", u=x.rcol, by=x.rcol, cs=None, cm=None, k="k-123456789012")
    for wrong in ({"rv": rv["id"] + 999}, {"p": p["id"] + 999}, {"rc": rc.id + 999}, {"l": x.w.loan["id"] + 999},
                  {"t": tenant_b["tenant_id"]}, {"b": x.b + 999}, {"c": "USD"}, {"a": Decimal("19.00")}, {"a": Decimal("10.00")},
                  {"o": "counter"}, {"by": x.rev}, {"cs": x.sess}, {"s": "branch_cash"}, {"u": x.cas, "by": x.cas}):
        refused(ins, **(base | wrong))
    sql(ins, **base)  # the exact snapshot is accepted once
    for dup in ({"n": "RFD-T2", "k": "k-123456789013"},):
        refused(ins, **(base | dup))  # one refund per reversal / payment / receipt
    refused("UPDATE credit_field_refunds SET reason = 'otro' WHERE payment_id = :p", p=p["id"])
    refused("DELETE FROM credit_field_refunds WHERE payment_id = :p", p=p["id"])
    # a rendition of exactly that receipt, otherwise valid: only the "refunded by its collector" guard can refuse it
    refused("WITH r AS (INSERT INTO credit_field_renditions (tenant_id, rendition_number, receiving_branch_id, custodian_user_id, currency_code, "
            "declared_amount, state, declared_by, declared_at, create_idempotency_key, create_request_digest) "
            "VALUES (:t, 'REN-X1', :b, :u, 'DOP', 20.00, 'declared', :u, now(), 'k-x-123456789', 'd') RETURNING id) "
            "INSERT INTO credit_field_rendition_items (tenant_id, rendition_id, receipt_id, payment_id, receiving_branch_id, custodian_user_id, "
            "currency_code, amount, released) SELECT :t, r.id, :rc, :p, :b, :u, 'DOP', 20.00, false FROM r",
            t=t, rc=rc.id, p=p["id"], b=x.b, u=x.rcol)
    # counter reversal through the field table
    counter, crv = counter_reversal(client, x, tenant_a)
    refused(ins, **(base | {"rv": crv["id"], "p": counter["id"], "a": Decimal("10.00"), "n": "RFD-C", "k": "k-123456789014"}))
    # branch source rules
    p2, p3 = fpay(client, x.rcol_h, x.w, "15.00"), fpay(client, x.rcol_h, x.w, "16.00")
    rv2, rv3 = reverse(client, x.rev_h, x, p2), reverse(client, x.rev_h, x, p3)
    (rc2,), (rc3,) = receipts(p2["id"]), receipts(p3["id"])
    b2 = base | dict(p=p2["id"], rv=rv2["id"], rc=rc2.id, a=Decimal("15.00"), n="RFD-B", k="k-123456789015")
    with SessionLocal() as db:
        mid = db.execute(text("INSERT INTO cash_movements (box_id, session_id, kind, amount, actor_id, notes, reference, created_at) "
                              "VALUES (:bx, :s, 'credit_field_refund', -15.00, :a, 'x', 'x', now()) RETURNING id"),
                         {"bx": x.w.cash.box_id, "s": x.rsess, "a": x.rcas}).scalar()
        db.commit()
    refused(ins, **(b2 | {"s": "branch_cash", "by": x.rcas, "cs": x.rsess, "cm": mid}))  # branch source before acceptance
    refused(ins, **(b2 | {"s": "branch_cash", "by": x.rcas}))  # branch source without session / movement
    r3 = declare(client, x.rcol_h, x.b, [p3["id"]])
    b3 = base | dict(p=p3["id"], rv=rv3["id"], rc=rc3.id, a=Decimal("16.00"), n="RFD-D", k="k-123456789016")
    refused(ins, **b3)  # collector source while declared
    refused(ins, **(b3 | {"s": "branch_cash", "by": x.rcas, "cs": x.rsess, "cm": mid}))  # branch source while declared
    decide(client, x.cas_h, r3["id"], "accept", cash_session_id=x.sess, counted_amount="16.00")
    refused(ins, **b3)  # collector source after the accepted rendition
    for kind, amount, sess in (("delivery", Decimal("-16.00"), x.rsess), (REFUND_KIND, Decimal("-15.00"), x.rsess),
                               (REFUND_KIND, Decimal("-16.00"), x.sess), (REFUND_KIND, Decimal("16.00"), x.rsess)):
        with SessionLocal() as db:
            m2 = db.execute(text("INSERT INTO cash_movements (box_id, session_id, kind, amount, actor_id, notes, reference, created_at) "
                                 "VALUES (:bx, :s, :k, :am, :a, 'x', 'x', now()) RETURNING id"),
                            {"bx": x.w.cash.box_id, "s": sess, "k": kind, "am": amount, "a": x.rcas}).scalar()
            db.commit()
        refused(ins, **(b3 | {"s": "branch_cash", "by": x.rcas, "cs": x.rsess, "cm": m2}))  # movement kind / amount / session / sign


# ================================ reads, purity, query count =========================================
def test_refund_reads_are_scoped_pure_constant_and_pii_free(client, sink, tenant_a, monkeypatch):
    x = rworld(client, sink, tenant_a, monkeypatch)
    pa, pb, pc = (fpay(client, x.rcol_h, x.w, a) for a in ("1.00", "2.00", "3.00"))
    r = declare(client, x.rcol_h, x.b, [pb["id"]])
    rva, rvb, rvc = (reverse(client, x.rev_h, x, p) for p in (pa, pb, pc))
    pending = client.get(F, headers=x.cas_h, params={"status": "pending"}).json()["items"]
    assert {i["reversal_id"]: i["physical_state"] for i in pending} == {
        rva["id"]: "collector_custody", rvb["id"]: "rendition_declared", rvc["id"]: "collector_custody"}
    refund(client, x.rcol_h, rva["id"])
    decide(client, x.rcol_h, r["id"], "cancel")
    refund(client, x.rcol_h, rvb["id"])
    assert [i["reversal_id"] for i in client.get(F, headers=x.cas_h, params={"status": "pending"}).json()["items"]] == [rvc["id"]]
    refunded = client.get(F, headers=x.cas_h, params={"status": "refunded"}).json()["items"]
    assert [i["reversal_id"] for i in refunded] == [rva["id"], rvb["id"]]
    stranger, _ = mkuser(client, sink, x.adm, tenant_a, "st@x.com", ["payments.read"])
    assert client.get(F, headers=stranger).status_code == 403
    assert client.get(F, headers=x.cas_h, params={"status": "other"}).status_code == 422

    def selects(**params):
        stmts = []

        def before(conn, cursor, statement, parameters, context, executemany):
            if re.match(r"\s*SELECT", statement, re.I):
                stmts.append(statement)

        event.listen(engine, "before_cursor_execute", before)
        try:
            assert client.get(F, headers=x.cas_h, params=params).status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", before)
        return len(stmts)

    for status in ("pending", "refunded"):
        assert selects(status=status, limit=1) == selects(status=status, limit=100)
    statements, stop = write_listener()
    events = count("security_events")
    try:
        for params in ({"status": "pending"}, {"status": "refunded"}):
            client.get(F, headers=x.cas_h, params=params)
        client.get(f"{PAYMENTS}/{pa['id']}/field-custody", headers=x.cas_h)
    finally:
        stop()
    assert statements == [] and count("security_events") == events
    raw = json.dumps([client.get(F, headers=x.cas_h, params={"status": s}).json() for s in ("pending", "refunded")])
    for leak in ("idempotency", "digest", "sha256", "Juan", "Perez", "Nombre", "@", "document", "phone"):
        assert leak not in raw, leak


def test_discrepancy_stays_exact_only_and_no_discrepancy_state_exists(client, sink, tenant_a, monkeypatch):
    x = rworld(client, sink, tenant_a, monkeypatch)
    p = fpay(client, x.rcol_h, x.w, "25.00")
    r = declare(client, x.rcol_h, x.b, [p["id"]])
    for counted in ("24.00", "26.00"):  # D02 / D03
        assert code(decide(client, x.cas_h, r["id"], "accept", expect=409, cash_session_id=x.sess, counted_amount=counted)) == "rendition_count_mismatch"
    rej = decide(client, x.cas_h, r["id"], "reject", reason="no cuadra", counted_amount="24.00")  # D04: all cash returned
    assert rej["items"][0]["released"] is True and p["id"] in outstanding(client, x.rcol_h)
    r2 = declare(client, x.rcol_h, x.b, [p["id"]])  # D12
    assert decide(client, x.cas_h, r2["id"], "accept", cash_session_id=x.sess, counted_amount="25.00")["state"] == "accepted"
    with SessionLocal() as db:
        tables = {t for (t,) in db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"))}
    assert not {t for t in tables if "discrep" in t or "write_off" in t or "suspense" in t}
    assert len([m for m in movements(KIND)]) == 1


def test_migration_0019_upgrade_downgrade_reupgrade_and_refusal_with_history(scratch_db):
    from sqlalchemy import create_engine

    assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    assert _alembic(scratch_db, "check").returncode == 0
    assert "0019" in _alembic(scratch_db, "heads").stdout
    assert _alembic(scratch_db, "downgrade", "0018").returncode == 0
    assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    eng = create_engine(scratch_db)
    with eng.begin() as c:
        c.execute(text("SET session_replication_role = replica"))  # bypass FKs/triggers only to plant a history row
        c.execute(text("INSERT INTO credit_field_refunds (tenant_id, refund_number, payment_id, reversal_id, receipt_id, loan_id, "
                       "receiving_branch_id, origin, currency_code, amount, source_kind, custodian_user_id, refunded_by, reason, refunded_at, "
                       "idempotency_key, request_digest) VALUES (1, 'RFD-1', 1, 1, 1, 1, 1, 'field', 'DOP', 1, 'collector', 1, 1, 'motivo', now(), "
                       "'k-123456789012', 'd')"))
    eng.dispose()
    out = _alembic(scratch_db, "downgrade", "0018")
    assert out.returncode != 0 and "Cannot downgrade 0019" in out.stderr
    eng = create_engine(scratch_db)
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM credit_field_refunds")).scalar() == 1
        assert c.execute(text("SELECT count(*) FROM permissions WHERE code = 'cash.field_custody.refund'")).scalar() == 1
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0019"
    eng.dispose()
