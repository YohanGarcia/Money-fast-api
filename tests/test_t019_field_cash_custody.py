"""T-019 Field cash custody and rendition tests (T019-*). PostgreSQL only.

Three truths kept apart: the payment (debt, T-008), the custody receipt (who physically holds field cash) and the cash
session (when it entered a drawer). One receipt per field payment, born in the payment's transaction; custodian = the
authenticated collector (no proxy); renditions declared -> accepted (exact, one Cash movement via the port) | rejected |
cancelled; reversals never change custody; no backfill; the database enforces every invariant.
"""

import inspect
import itertools
import json
import re
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from app.core.db import SessionLocal, engine
from app.models.cash import CashSession
from app.models.user import User, UserRole
from app.modules.field_custody import receipts as receipt_service
from app.modules.field_custody import service as custody_service
from app.modules.loans import payments as pay_service
from app.schemas.cash import CashCommand
from app.services import cash_service
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
from tests.test_t003_organization import mk_branch
from tests.test_t006_origination import (  # noqa: F401
    _release_tracked_connections,
    count,
    in_thread,
)
from tests.test_t007_disbursement import cash_for, session_balance
from tests.test_t008_payments import LOANS, PAYMENTS, clock, loan_world, pay, pbody
from tests.test_t009_payment_reversal import audit
from tests.test_t010_overdue_projection import write_listener
from tests.test_t012_collection_assignment import _fresh_pool, mkuser  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
V2 = "/api/v2"
R = f"{V2}/cash/field-renditions"
C = f"{V2}/cash/field-custody"
READ, RENDER, ACCEPT = "cash.field_custody.read", "cash.field_custody.render", "cash.field_custody.accept"
KIND = "credit_field_rendition"
_keys = itertools.count(1)


def key(prefix="fc"):
    return f"{prefix}-key-{next(_keys):08d}"


# ================================ helpers ============================================================
def cashier_session(tenant, box_id, cashier_id, state="open", balance="0.00"):
    with SessionLocal() as db:
        s = CashSession(
            box_id=box_id,
            business_date=date.today(),
            state=state,
            opening_expected=Decimal(balance),
            opening_counted=Decimal(balance),
            balance=Decimal(balance),
            opened_by=tenant["admin_id"],
            cashier_id=cashier_id,
        )
        db.add(s)
        db.commit()
        return s.id


def world(client, sink, tenant, monkeypatch, tag="a"):
    adm = admin_headers(client, tenant)
    w = loan_world(client, adm, tenant)
    clock(monkeypatch, days=35)
    b = w.b["id"]
    col_h, col = mkuser(client, sink, adm, tenant, f"col-{tag}@x.com", ["payments.create", "payments.read", RENDER], scope="branch", branch_id=b)
    cas_h, cas = mkuser(client, sink, adm, tenant, f"cas-{tag}@x.com", [ACCEPT, READ], scope="branch", branch_id=b)
    return SimpleNamespace(adm=adm, w=w, b=b, col_h=col_h, col=col, cas_h=cas_h, cas=cas, sess=cashier_session(tenant, w.cash.box_id, cas))


def fpay(client, hdr, w, amount="50.00", expect=200):
    return pay(client, hdr, w, amount, origin="field", expect=expect)


def declare(client, hdr, branch, payment_ids, expect=200, k=None):
    r = client.post(R, headers=hdr, json={"idempotency_key": k or key(), "receiving_branch_id": branch, "payment_ids": payment_ids})
    assert r.status_code == expect, f"declare: {r.status_code} {r.text}"
    return r.json()


def decide(client, hdr, rid, action, expect=200, k=None, **body):
    r = client.post(f"{R}/{rid}/{action}", headers=hdr, json={"idempotency_key": k or key(), **body})
    assert r.status_code == expect, f"{action}: {r.status_code} {r.text}"
    return r.json()


def code(resp_json):
    return resp_json["error"]["code"]


def receipts(payment_id=None):
    with SessionLocal() as db:
        sql = "SELECT id, payment_id, loan_id, receiving_branch_id, custodian_user_id, currency_code, amount FROM credit_field_custody_receipts"
        return db.execute(text(sql + (" WHERE payment_id = :p" if payment_id else "") + " ORDER BY id"), {"p": payment_id}).all()


def movements(kind=KIND):
    with SessionLocal() as db:
        return db.execute(text("SELECT id, session_id, amount, kind FROM cash_movements WHERE kind = :k ORDER BY id"), {"k": kind}).all()


def outstanding(client, hdr, **params):
    return {i["payment_id"]: i for i in client.get(C, headers=hdr, params={"limit": 100, **params}).json()["items"]}


def sql(statement, **params):
    with SessionLocal() as db:
        db.execute(text(statement), params)
        db.commit()


def refused(statement, **params):
    with pytest.raises(DBAPIError):
        sql(statement, **params)


# ================================ birth ================================================================
def test_a_field_payment_creates_exactly_one_receipt_atomically_and_a_counter_payment_none(client, sink, tenant_a, monkeypatch):
    x = world(client, sink, tenant_a, monkeypatch)
    p = fpay(client, x.col_h, x.w, "75.00")
    (rc,) = receipts(p["id"])
    assert rc[1:] == (p["id"], x.w.loan["id"], x.b, x.col, "DOP", Decimal("75.0000"))
    assert p["collected_by"] == x.col and p["cash_session_id"] is None  # the payment itself is unchanged
    counter = pay(client, x.adm, x.w, "10.00")  # counter: the cash entered a session; no custody receipt
    assert receipts(counter["id"]) == []
    # the custodian is the authenticated actor: no request field can name another custodian
    for extra in ({"collector_user_id": x.cas}, {"custodian_user_id": x.cas}, {"collected_by": x.cas}):
        assert client.post(f"{LOANS}/{x.w.loan['id']}/payments", headers=x.col_h, json=pbody(x.w, "1.00", origin="field") | extra).status_code == 422
    # an admin who records a field payment becomes its custodian (v1 operating rule, documented)
    adm_p = fpay(client, x.adm, x.w, "5.00")
    assert receipts(adm_p["id"])[0][4] == tenant_a["admin_id"]
    # field collection needs payments.create AND cash.field_custody.render on the receiving branch; counter does not
    only_pay, _ = mkuser(client, sink, x.adm, tenant_a, "op@x.com", ["payments.create"], scope="branch", branch_id=x.b)
    before = count("credit_payments")
    assert code(fpay(client, only_pay, x.w, "1.00", expect=403)) == "permission_denied"
    assert count("credit_payments") == before
    assert pay(client, only_pay, x.w, "1.00", session=x.w.cash.session_id)["origin"] == "counter"
    # the receipt insert failing rolls the whole payment back (debt, applications, number)
    def boom(db, payment):
        raise RuntimeError("receipt failure")

    monkeypatch.setattr(pay_service, "create_receipt", boom)
    before = (count("credit_payments"), count("credit_payment_applications"), count("credit_field_custody_receipts"))
    with pytest.raises(RuntimeError):
        client.post(f"{LOANS}/{x.w.loan['id']}/payments", headers=x.col_h, json=pbody(x.w, "3.00", origin="field"))
    assert (count("credit_payments"), count("credit_payment_applications"), count("credit_field_custody_receipts")) == before


# ================================ rendition: exact acceptance, replay, release ==========================
def test_declare_and_exact_acceptance_create_one_movement_and_replays_never_duplicate(client, sink, tenant_a, monkeypatch):
    x = world(client, sink, tenant_a, monkeypatch)
    p1, p2, p3 = (fpay(client, x.col_h, x.w, a) for a in ("40.00", "60.00", "25.00"))
    k = key()
    r = declare(client, x.col_h, x.b, [p2["id"], p1["id"]], k=k)
    assert r["state"] == "declared" and r["declared_amount"] == "100.0000" and r["custodian_user_id"] == x.col
    assert {i["payment_id"]: i["amount"] for i in r["items"]} == {p1["id"]: "40.0000", p2["id"]: "60.0000"}
    assert declare(client, x.col_h, x.b, [p1["id"], p2["id"]], k=k)["replayed"] is True  # sorted ids: same digest
    assert code(declare(client, x.col_h, x.b, [p1["id"]], k=k, expect=409)) == "idempotency_conflict"
    # declared is still the collector's cash (pending), not branch cash
    out = outstanding(client, x.col_h)
    assert set(out) == {p1["id"], p2["id"], p3["id"]} and out[p1["id"]]["pending_rendition_id"] == r["id"]
    assert code(declare(client, x.col_h, x.b, [p1["id"], p3["id"]], expect=409)) == "receipt_already_claimed"  # C04
    balance = session_balance(x.sess)
    ak = key()
    acc = decide(client, x.cas_h, r["id"], "accept", k=ak, cash_session_id=x.sess, counted_amount="100.00")
    assert acc["state"] == "accepted" and acc["cash_session_id"] == x.sess and acc["counted_amount"] == "100.0000"
    (m,) = movements()
    assert (m.id, m.session_id, m.amount) == (acc["cash_movement_id"], x.sess, Decimal("100.00"))
    assert session_balance(x.sess) == balance + Decimal("100.00")
    # replay: same answer, no second movement; another key or command after the terminal transition: 409
    assert decide(client, x.cas_h, r["id"], "accept", k=ak, cash_session_id=x.sess, counted_amount="100.00")["replayed"] is True
    assert code(decide(client, x.cas_h, r["id"], "accept", k=ak, expect=409, cash_session_id=x.sess, counted_amount="100.0")) in ("idempotency_conflict",)
    assert code(decide(client, x.cas_h, r["id"], "accept", expect=409, cash_session_id=x.sess, counted_amount="100.00")) == "rendition_not_declared"
    assert code(decide(client, x.cas_h, r["id"], "reject", expect=409, reason="tarde")) == "rendition_not_declared"
    assert code(decide(client, x.col_h, r["id"], "cancel", expect=409)) == "rendition_not_declared"
    assert len(movements()) == 1 and session_balance(x.sess) == balance + Decimal("100.00")
    # accepted receipts are no longer outstanding and can never be declared again; the third one still is
    assert set(outstanding(client, x.col_h)) == {p3["id"]}
    assert code(declare(client, x.col_h, x.b, [p1["id"]], expect=409)) == "receipt_already_claimed"
    detail = client.get(f"{PAYMENTS}/{p1['id']}/field-custody", headers=x.col_h).json()
    assert detail["tracking_status"] == "rendered" and detail["renditions"][0]["state"] == "accepted"
    assert client.get(f"{PAYMENTS}/{p3['id']}/field-custody", headers=x.col_h).json()["tracking_status"] == "outstanding"
    events = [e.event_type for e in audit("field_custody.")]
    assert events == ["field_custody.rendition_declared", "field_custody.rendition_accepted"]  # replays add none


def test_mismatched_counts_are_never_accepted_and_reject_or_cancel_release_the_receipts(client, sink, tenant_a, monkeypatch):
    x = world(client, sink, tenant_a, monkeypatch)
    p1, p2 = fpay(client, x.col_h, x.w, "30.00"), fpay(client, x.col_h, x.w, "20.00")
    r = declare(client, x.col_h, x.b, [p1["id"], p2["id"]])
    for counted in ("49.99", "50.01", "0.00", "60.00"):  # short / over: nothing moves (C09, C10)
        assert code(decide(client, x.cas_h, r["id"], "accept", expect=409, cash_session_id=x.sess, counted_amount=counted)) == "rendition_count_mismatch"
    assert code(decide(client, x.cas_h, r["id"], "accept", expect=422, cash_session_id=x.sess, counted_amount="50.001")) == "invalid_custody_amount"
    assert movements() == [] and client.get(f"{R}/{r['id']}", headers=x.cas_h).json()["state"] == "declared"
    # maker-checker: the custodian never decides; a non-custodian never cancels
    custodian_too, cu = mkuser(client, sink, x.adm, tenant_a, "both@x.com", ["payments.create", RENDER, ACCEPT, READ], scope="branch", branch_id=x.b)
    own = declare(client, custodian_too, x.b, [fpay(client, custodian_too, x.w, "5.00")["id"]])
    s2 = cashier_session(tenant_a, x.w.cash.box_id, cu)
    assert code(decide(client, custodian_too, own["id"], "accept", expect=403, cash_session_id=s2, counted_amount="5.00")) == "maker_checker_violation"
    assert code(decide(client, custodian_too, own["id"], "reject", expect=403, reason="propio")) == "maker_checker_violation"
    assert code(decide(client, x.cas_h, r["id"], "cancel", expect=403)) in ("permission_denied", "not_custodian")
    reader, _ = mkuser(client, sink, x.adm, tenant_a, "rd@x.com", [READ], scope="branch", branch_id=x.b)
    for action, body in (("accept", {"cash_session_id": x.sess, "counted_amount": "50.00"}), ("reject", {"reason": "sin permiso"})):
        assert code(decide(client, reader, r["id"], action, expect=403, **body)) == "permission_denied"  # read is not accept
        assert code(decide(client, x.col_h, r["id"], action, expect=403, **body)) == "permission_denied"  # render is not accept
    # reject: reason mandatory, counted is an observation only; items released; the cash stays with the custodian
    decide(client, x.cas_h, r["id"], "reject", expect=422, reason="  ")
    rej = decide(client, x.cas_h, r["id"], "reject", reason="Faltan 0.01", counted_amount="49.99")
    assert rej["state"] == "rejected" and all(i["released"] for i in rej["items"]) and rej["counted_amount"] == "49.9900"
    assert movements() == [] and set(outstanding(client, x.col_h)) >= {p1["id"], p2["id"]}
    assert outstanding(client, x.col_h)[p1["id"]]["pending_rendition_id"] is None
    # re-declare after rejection; then cancel by the custodian releases again; then accept works
    r2 = declare(client, x.col_h, x.b, [p1["id"]])
    can = decide(client, x.col_h, r2["id"], "cancel")
    assert can["state"] == "cancelled" and can["items"][0]["released"] is True and movements() == []
    r3 = declare(client, x.col_h, x.b, [p1["id"], p2["id"]])
    decide(client, x.cas_h, r3["id"], "accept", cash_session_id=x.sess, counted_amount="50.00")
    history = client.get(f"{PAYMENTS}/{p1['id']}/field-custody", headers=x.cas_h).json()
    assert [h["state"] for h in history["renditions"]] == ["rejected", "cancelled", "accepted"]
    assert [h["released"] for h in history["renditions"]] == [True, True, False]
    assert len(movements()) == 1


def test_the_target_session_is_explicit_open_of_the_branch_and_owned_by_the_acceptor(client, sink, tenant_a, monkeypatch):
    x = world(client, sink, tenant_a, monkeypatch)
    r = declare(client, x.col_h, x.b, [fpay(client, x.col_h, x.w, "10.00")["id"]])
    body = {"idempotency_key": key(), "counted_amount": "10.00"}
    assert client.post(f"{R}/{r['id']}/accept", headers=x.cas_h, json=body).status_code == 422  # no "latest" session
    other_h, other = mkuser(client, sink, x.adm, tenant_a, "cas2@x.com", [ACCEPT, READ], scope="branch", branch_id=x.b)
    foreign_session = cashier_session(tenant_a, x.w.cash.box_id, other)  # C12: two open sessions in the box
    assert code(decide(client, x.cas_h, r["id"], "accept", expect=403, cash_session_id=foreign_session, counted_amount="10.00")) == "cash_session_not_owned"
    # a session nobody owns as cashier (opened by an admin) is not the acceptor's drawer either
    assert code(decide(client, x.cas_h, r["id"], "accept", expect=403, cash_session_id=x.w.cash.session_id, counted_amount="10.00")) == "cash_session_not_owned"
    closed = cashier_session(tenant_a, x.w.cash.box_id, x.cas, state="closed")
    assert code(decide(client, x.cas_h, r["id"], "accept", expect=409, cash_session_id=closed, counted_amount="10.00")) == "cash_unavailable"  # C11
    b2 = mk_branch(client, x.adm, "FC-B2")
    box2 = cash_for(tenant_a, b2["id"])
    other_branch = cashier_session(tenant_a, box2.box_id, x.cas)
    assert code(decide(client, x.cas_h, r["id"], "accept", expect=409, cash_session_id=other_branch, counted_amount="10.00")) == "cash_unavailable"
    assert movements() == []
    ok = decide(client, x.cas_h, r["id"], "accept", cash_session_id=x.sess, counted_amount="10.00")
    assert ok["cash_session_id"] == x.sess and other_h  # the explicit, owned session of this branch


def test_declaration_rules_custodian_branch_counter_pre_custody_and_tenant(client, sink, tenant_a, tenant_b, monkeypatch):
    x = world(client, sink, tenant_a, monkeypatch)
    col2_h, col2 = mkuser(client, sink, x.adm, tenant_a, "col2@x.com", ["payments.create", RENDER], scope="branch", branch_id=x.b)
    mine, theirs = fpay(client, x.col_h, x.w), fpay(client, col2_h, x.w)
    assert code(declare(client, x.col_h, x.b, [mine["id"], theirs["id"]], expect=403)) == "not_custodian"  # C05
    counter = pay(client, x.adm, x.w, "10.00")
    assert code(declare(client, x.col_h, x.b, [counter["id"]], expect=404)) == "custody_receipt_not_found"
    # a field payment recorded before T-019 (simulated: no receipt) stays pre_custody: never declared nor backfilled
    monkeypatch.setattr(pay_service, "create_receipt", lambda db, payment: None)
    old = fpay(client, x.col_h, x.w, "7.00")
    monkeypatch.undo()
    clock(monkeypatch, days=35)
    assert receipts(old["id"]) == [] and code(declare(client, x.col_h, x.b, [old["id"]], expect=404)) == "custody_receipt_not_found"
    pre = client.get(f"{PAYMENTS}/{old['id']}/field-custody", headers=x.cas_h).json()
    assert pre == {"payment_id": old["id"], "origin": "field", "receiving_branch_id": x.b, "tracking_status": "pre_custody"}
    assert client.get(f"{PAYMENTS}/{counter['id']}/field-custody", headers=x.cas_h).json()["tracking_status"] == "not_applicable"
    # another branch (C07): the receipt belongs to its receiving branch; no cross-branch rendition
    b2 = mk_branch(client, x.adm, "FC-X2")
    both_h, _ = mkuser(client, sink, x.adm, tenant_a, "both2@x.com", ["payments.create", RENDER])
    p_b1 = fpay(client, both_h, x.w)
    assert code(declare(client, both_h, b2["id"], [p_b1["id"]], expect=422)) == "custody_branch_mismatch"
    # foreign tenant (C06): the other tenant's admin cannot see or declare these receipts
    adm_b = admin_headers(client, tenant_b)
    assert code(declare(client, adm_b, x.b, [mine["id"]], expect=404)) in ("tenant_mismatch", "resource_not_found", "not_found")
    assert client.get(f"{PAYMENTS}/{mine['id']}/field-custody", headers=adm_b).status_code == 404
    assert client.get(C, headers=adm_b, params={"limit": 100}).json()["items"] == []
    # schema: no client amounts, no duplicates, no empty list
    for bad in ({"payment_ids": []}, {"payment_ids": [mine["id"], mine["id"]]}, {"payment_ids": [mine["id"]], "amount": "1.00"},
                {"payment_ids": [mine["id"]], "custodian_user_id": col2}):
        assert client.post(R, headers=x.col_h, json={"idempotency_key": key(), "receiving_branch_id": x.b, **bad}).status_code == 422


# ================================ reversal never changes custody ========================================
def test_a_reversal_never_changes_physical_custody_before_during_or_after_a_rendition(client, sink, tenant_a, monkeypatch):
    x = world(client, sink, tenant_a, monkeypatch)
    rev_h, _ = mkuser(client, sink, x.adm, tenant_a, "rev@x.com", ["payments.reverse", "payments.read"], scope="branch", branch_id=x.b)
    calls = []
    real = custody_service.cash_port.withdraw
    monkeypatch.setattr("app.modules.loans.reversals.cash_port.withdraw", lambda *a, **k: calls.append(k) or real(*a, **k))

    def reverse(p):
        r = client.post(f"{PAYMENTS}/{p['id']}/reversals", headers=rev_h,
                        json={"idempotency_key": key("rv"), "reason": "error de cobro", "reversal_branch_id": x.b})
        assert r.status_code == 200, r.text

    before, pending, accepted = fpay(client, x.col_h, x.w, "11.00"), fpay(client, x.col_h, x.w, "12.00"), fpay(client, x.col_h, x.w, "13.00")
    reverse(before)  # R04: debt restored, cash still with the collector
    assert before["id"] in outstanding(client, x.col_h)
    r_pending = declare(client, x.col_h, x.b, [pending["id"]])
    reverse(pending)  # R05: the declared rendition stays valid
    assert client.get(f"{R}/{r_pending['id']}", headers=x.col_h).json()["state"] == "declared"
    r_acc = declare(client, x.col_h, x.b, [accepted["id"]])
    decide(client, x.cas_h, r_acc["id"], "accept", cash_session_id=x.sess, counted_amount="13.00")
    bal, moved = session_balance(x.sess), len(movements())
    reverse(accepted)  # R06: the cash stays in the session; no withdrawal, no release, no refund
    assert session_balance(x.sess) == bal and len(movements()) == moved and calls == []
    assert client.get(f"{PAYMENTS}/{accepted['id']}/field-custody", headers=x.col_h).json()["tracking_status"] == "rendered"
    # R08: rendition after the reversal is allowed (the physical cash still exists) and R05 accepts normally
    r_after = declare(client, x.col_h, x.b, [before["id"]])
    decide(client, x.cas_h, r_after["id"], "accept", cash_session_id=x.sess, counted_amount="11.00")
    decide(client, x.cas_h, r_pending["id"], "accept", cash_session_id=x.sess, counted_amount="12.00")
    assert len(movements()) == 3 and calls == []
    with SessionLocal() as db:  # no reversal ever released an item
        assert db.execute(text("SELECT count(*) FROM credit_field_rendition_items WHERE released")).scalar() == 0
    src = (ROOT / "app/modules/loans/reversals.py").read_text(encoding="utf-8")
    assert "field_custody" not in src  # T-009 runtime unchanged


# ================================ concurrency ==========================================================
def test_two_declarations_of_one_receipt_and_two_decisions_of_one_rendition_never_both_win(client, sink, tenant_a, monkeypatch):
    x = world(client, sink, tenant_a, monkeypatch)
    p = fpay(client, x.col_h, x.w, "9.00")
    barrier = threading.Barrier(2)

    def go_declare():
        barrier.wait()
        return client.post(R, headers=x.col_h, json={"idempotency_key": key(), "receiving_branch_id": x.b, "payment_ids": [p["id"]]})

    ts = [in_thread(go_declare) for _ in range(2)]
    for t, _ in ts:
        t.join(60)
    assert sorted(o["resp"].status_code for _, o in ts) == [200, 409]
    rid = next(o["resp"].json()["id"] for _, o in ts if o["resp"].status_code == 200)
    barrier2 = threading.Barrier(3)

    def go(action, **body):
        def run():
            barrier2.wait()
            return client.post(f"{R}/{rid}/{action}", headers=x.cas_h if action != "cancel" else x.col_h, json={"idempotency_key": key(), **body})
        return run

    ts = [in_thread(go("accept", cash_session_id=x.sess, counted_amount="9.00")), in_thread(go("accept", cash_session_id=x.sess, counted_amount="9.00")),
          in_thread(go("cancel"))]
    for t, _ in ts:
        t.join(60)
    codes = sorted(o["resp"].status_code for _, o in ts)
    assert codes.count(200) == 1 and codes.count(409) == 2, codes
    assert len(movements()) <= 1
    state = client.get(f"{R}/{rid}", headers=x.cas_h).json()["state"]
    assert (state == "accepted") == (len(movements()) == 1)


# ================================ database invariants ==================================================
def test_the_database_enforces_every_custody_invariant(client, sink, tenant_a, tenant_b, monkeypatch):
    x = world(client, sink, tenant_a, monkeypatch)
    p = fpay(client, x.col_h, x.w, "20.00")
    p2 = fpay(client, x.col_h, x.w, "5.00")
    counter = pay(client, x.adm, x.w, "10.00")
    (rc,) = receipts(p["id"])
    t, loan = tenant_a["tenant_id"], x.w.loan["id"]
    ins = ("INSERT INTO credit_field_custody_receipts (tenant_id, payment_id, loan_id, receiving_branch_id, custodian_user_id, "
           "currency_code, amount, received_at, created_at) SELECT :t, id, :l, :b, :u, :c, :a, received_at, now() FROM credit_payments WHERE id = :p")
    refused(ins, t=t, l=loan, b=x.b, u=x.col, c="DOP", a=Decimal("20.00"), p=p["id"])  # duplicate payment receipt
    refused(ins, t=t, l=loan, b=x.b, u=tenant_a["admin_id"], c="DOP", a=Decimal("10.00"), p=counter["id"])  # counter payment
    # a field payment without a receipt (pre-T-019 shape) isolates each snapshot rule: only the exact snapshot is accepted
    monkeypatch.setattr(pay_service, "create_receipt", lambda db, payment: None)
    bare = fpay(client, x.col_h, x.w, "7.00")
    monkeypatch.setattr(pay_service, "create_receipt", receipt_service.create_receipt)
    other_branch = mk_branch(client, x.adm, "FC-DB2")["id"]
    base = dict(t=t, l=loan, b=x.b, u=x.col, c="DOP", a=Decimal("7.00"), p=bare["id"])
    for wrong in ({"t": tenant_b["tenant_id"]}, {"l": loan + 999}, {"a": Decimal("6.99")}, {"a": Decimal("7.001")}, {"c": "USD"},
                  {"u": x.cas}, {"b": other_branch}):
        refused(ins, **(base | wrong))
    sql(ins, **base)  # the exact snapshot is accepted (positive control)
    refused(ins, **base)  # and only once
    refused("UPDATE credit_field_custody_receipts SET amount = 1 WHERE id = :i", i=rc.id)
    refused("DELETE FROM credit_field_custody_receipts WHERE id = :i", i=rc.id)
    # renditions / items
    r = declare(client, x.col_h, x.b, [p["id"]])
    item_ins = ("INSERT INTO credit_field_rendition_items (tenant_id, rendition_id, receipt_id, payment_id, receiving_branch_id, "
                "custodian_user_id, currency_code, amount, released) VALUES (:t, :r, :rc, :p, :b, :u, 'DOP', :a, false)")
    (rc2,) = receipts(p2["id"])
    ib = dict(t=t, r=r["id"], rc=rc2.id, p=p2["id"], b=x.b, u=x.col, a=Decimal("5.00"))
    refused(item_ins, **(ib | {"a": Decimal("2.50")}))  # partial amount
    refused(item_ins, **(ib | {"u": x.cas}))  # other custodian
    refused(item_ins, **(ib | {"b": x.b + 1}))  # other branch
    refused(item_ins, **(ib | {"t": tenant_b["tenant_id"]}))  # other tenant
    refused(item_ins, **(ib | {"rc": rc.id, "p": p["id"], "a": Decimal("20.00")}))  # duplicate live claim of the same receipt
    refused(item_ins, **ib)  # sum check at commit: declared amount would no longer equal the items
    refused("UPDATE credit_field_rendition_items SET released = true WHERE rendition_id = :r", r=r["id"])  # declared: not releasable
    refused("UPDATE credit_field_renditions SET state = 'accepted', decided_by = :c, decided_at = now(), counted_amount = declared_amount, "
            "decision_idempotency_key = 'k-123456789012', decision_request_digest = 'd' WHERE id = :r", c=x.cas, r=r["id"])  # no movement
    with SessionLocal() as db:
        mid = db.execute(text("INSERT INTO cash_movements (box_id, session_id, kind, amount, actor_id, notes, reference, created_at) "
                              "VALUES (:bx, :s, 'credit_field_rendition', 19.00, :a, 'x', 'x', now()) RETURNING id"),
                         {"bx": x.w.cash.box_id, "s": x.sess, "a": x.cas}).scalar()
        db.commit()
    accept_sql = ("UPDATE credit_field_renditions SET state = 'accepted', decided_by = :c, decided_at = now(), counted_amount = :n, "
                  "cash_session_id = :s, cash_movement_id = :m, decision_idempotency_key = 'k-123456789012', decision_request_digest = 'd' WHERE id = :r")
    refused(accept_sql, c=x.cas, n=Decimal("19.00"), s=x.sess, m=mid, r=r["id"])  # counted != declared
    refused(accept_sql, c=x.cas, n=Decimal("20.00"), s=x.sess, m=mid, r=r["id"])  # movement amount != declared (deferred)
    refused(accept_sql, c=x.col, n=Decimal("20.00"), s=x.sess, m=mid, r=r["id"])  # maker-checker
    refused("UPDATE credit_field_renditions SET state = 'rejected', decided_by = :c, decided_at = now(), decision_reason = 'motivo', "
            "decision_idempotency_key = 'k-123456789013', decision_request_digest = 'd' WHERE id = :r", c=x.cas, r=r["id"])  # live items left
    refused("DELETE FROM credit_field_renditions WHERE id = :r", r=r["id"])
    acc = decide(client, x.cas_h, r["id"], "accept", cash_session_id=x.sess, counted_amount="20.00")
    refused("UPDATE credit_field_renditions SET state = 'rejected', decision_reason = 'tarde' WHERE id = :r", r=r["id"])  # second transition
    refused("UPDATE credit_field_rendition_items SET released = true WHERE rendition_id = :r", r=r["id"])  # accepted never releases
    refused("UPDATE credit_field_rendition_items SET released = false WHERE rendition_id = :r", r=r["id"])
    refused("DELETE FROM credit_field_rendition_items WHERE rendition_id = :r", r=r["id"])
    r2 = declare(client, x.col_h, x.b, [p2["id"]])
    refused("UPDATE credit_field_renditions SET state = 'accepted', decided_by = :c, decided_at = now(), counted_amount = declared_amount, "
            "cash_session_id = :s, cash_movement_id = :m, decision_idempotency_key = 'k-123456789014', decision_request_digest = 'd' WHERE id = :r",
            c=x.cas, s=x.sess, m=acc["cash_movement_id"], r=r2["id"])  # a second rendition on the same movement
    refused("INSERT INTO credit_field_renditions (tenant_id, rendition_number, receiving_branch_id, custodian_user_id, currency_code, "
            "declared_amount, state, declared_by, declared_at, create_idempotency_key, create_request_digest) "
            "VALUES (:t, 'REN-X', :b, :u, 'DOP', 5, 'accepted', :u, now(), 'k-123456789015', 'd')", t=t, b=x.b, u=x.col)  # born terminal


# ================================ user lifecycle =======================================================
def test_a_custodian_cannot_be_disabled_while_custody_or_a_declaration_is_open(client, sink, tenant_a, monkeypatch):
    x = world(client, sink, tenant_a, monkeypatch)

    def disable(uid, expect):
        r = client.post(f"{V2}/users/{uid}/disable", headers=x.adm)
        assert r.status_code == expect, r.text
        return r.json()

    idle_h, idle = mkuser(client, sink, x.adm, tenant_a, "idle@x.com", ["payments.create", RENDER], scope="branch", branch_id=x.b)
    p = fpay(client, x.col_h, x.w, "8.00")
    assert code(disable(x.col, 409)) == "outstanding_field_custody"  # outstanding receipt
    r = declare(client, x.col_h, x.b, [p["id"]])
    assert code(disable(x.col, 409)) == "outstanding_field_custody"  # declared rendition
    decide(client, x.cas_h, r["id"], "accept", cash_session_id=x.sess, counted_amount="8.00")
    disable(x.col, 200)  # everything handed in: disables normally, custody never transferred
    disable(idle, 200)  # a user without custody disables normally
    assert fpay(client, idle_h, x.w, "1.00", expect=401)  # disabled: its sessions were revoked
    # an inactive custodian (disabled out of band) never disappears from the reads
    p2 = fpay(client, x.adm, x.w, "4.00")
    sql("UPDATE users SET status = 'disabled' WHERE id = :u", u=tenant_a["admin_id"])
    with SessionLocal() as db:
        assert custody_service.has_open_custody(db, tenant_a["tenant_id"], tenant_a["admin_id"])
        rows = db.execute(text("SELECT payment_id FROM credit_field_custody_receipts WHERE custodian_user_id = :u"), {"u": tenant_a["admin_id"]}).all()
    assert p2["id"] in {r.payment_id for r in rows}
    assert p2["id"] in outstanding(client, x.cas_h, custodian_user_id=tenant_a["admin_id"])
    with SessionLocal() as db:  # an inactive custodian cannot be born or declare
        with pytest.raises(Exception) as err:
            receipt_service.require_active_user(db, tenant_a["admin_id"])
        assert "custodian_inactive" == err.value.code
    sql("UPDATE users SET status = 'active' WHERE id = :u", u=tenant_a["admin_id"])
    src = inspect.getsource(cash_service.user_has_pending)
    assert "credit_field" not in src  # the legacy check is untouched; the modern guard is additive


# ================================ reads, scope, purity, query count =====================================
def test_reads_are_scoped_pii_free_pure_and_constant_in_queries(client, sink, tenant_a, monkeypatch):
    x = world(client, sink, tenant_a, monkeypatch)
    col2_h, col2 = mkuser(client, sink, x.adm, tenant_a, "c2@x.com", ["payments.create", RENDER], scope="branch", branch_id=x.b)
    b2 = mk_branch(client, x.adm, "FC-R2")
    stranger, _ = mkuser(client, sink, x.adm, tenant_a, "st@x.com", [READ], scope="branch", branch_id=b2["id"])
    mine = [fpay(client, x.col_h, x.w, a)["id"] for a in ("1.00", "2.00", "3.00")]
    theirs = fpay(client, col2_h, x.w, "4.00")["id"]
    declare(client, x.col_h, x.b, [mine[0]])
    assert set(outstanding(client, x.col_h)) == set(mine)  # S01 own only
    assert set(outstanding(client, col2_h)) == {theirs}  # S02
    assert set(outstanding(client, x.cas_h)) == {*mine, theirs}  # S03 branch reader
    assert outstanding(client, stranger) == {}  # another branch
    assert client.get(f"{PAYMENTS}/{theirs}/field-custody", headers=x.col_h).status_code == 403
    summary = client.get(f"{C}/summary", headers=x.cas_h, params={"receiving_branch_id": x.b}).json()
    by = {c["custodian_user_id"]: c for c in summary["custodians"]}
    assert by[x.col] == {"custodian_user_id": x.col, "outstanding_receipts": 3, "outstanding_amount": "6.0000", "pending_receipts": 1, "pending_amount": "1.0000"}
    assert client.get(f"{C}/summary", headers=x.col_h, params={"receiving_branch_id": x.b}).status_code == 403
    pend = client.get(R, headers=x.cas_h, params={"receiving_branch_id": x.b, "state": "declared"}).json()["items"]
    assert [p["state"] for p in pend] == ["declared"]
    # keyset pagination: full pages, no holes
    seen, cursor = [], None
    while True:
        page = client.get(C, headers=x.cas_h, params={"limit": 2, **({"cursor": cursor} if cursor else {})}).json()
        seen += [i["payment_id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == sorted({*mine, theirs}) and client.get(C, headers=x.cas_h, params={"cursor": "zz"}).status_code == 422

    def selects(url, **params):
        stmts = []

        def before(conn, cursor, statement, parameters, context, executemany):
            if re.match(r"\s*SELECT", statement, re.I):
                stmts.append(statement)

        event.listen(engine, "before_cursor_execute", before)
        try:
            assert client.get(url, headers=x.cas_h, params=params).status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", before)
        return len(stmts)

    for url in (C, R):
        assert selects(url, limit=1) == selects(url, limit=100), url  # no N+1
    statements, stop = write_listener()
    events = count("security_events")
    try:
        for url, params in ((C, {}), (f"{C}/summary", {"receiving_branch_id": x.b}), (R, {}), (f"{PAYMENTS}/{mine[0]}/field-custody", {})):
            assert client.get(url, headers=x.cas_h, params=params).status_code == 200
    finally:
        stop()
    assert statements == [] and count("security_events") == events  # GETs never write nor audit
    raw = json.dumps([client.get(u, headers=x.cas_h, params=p).json() for u, p in ((C, {}), (R, {}), (f"{C}/summary", {"receiving_branch_id": x.b}))])
    for leak in ("idempotency", "digest", "sha256", "-key-", "Juan", "Perez", "Nombre", "@", "document", "phone"):
        assert leak not in raw, leak


# ================================ boundaries ===========================================================
def test_the_legacy_cash_reversal_refuses_a_field_rendition_movement(client, sink, tenant_a, monkeypatch):
    x = world(client, sink, tenant_a, monkeypatch)
    r = declare(client, x.col_h, x.b, [fpay(client, x.col_h, x.w, "6.00")["id"]])
    acc = decide(client, x.cas_h, r["id"], "accept", cash_session_id=x.sess, counted_amount="6.00")
    with SessionLocal() as db:
        admin = db.get(User, tenant_a["admin_id"])
        admin.role = UserRole.admin
        db.commit()
        cmd = CashCommand(action="reverse", target_id=acc["cash_movement_id"], branch_id=x.b, session_id=x.sess,
                          idempotency_key="legacy-reverse-fc1", notes="intento legacy", version=1)
        with pytest.raises(HTTPException) as err:
            cash_service.command(db, admin, cmd)
        assert "no admite otro reverso" in str(err.value.detail)
        db.rollback()
    assert count("cash_movements", "reverses_id IS NOT NULL") == 0


def test_no_legacy_dual_write_no_direct_session_write_no_new_sort_or_side_effect():
    pkg = ROOT / "app/modules/field_custody"
    src = "\n".join(p.read_text(encoding="utf-8") for p in pkg.glob("*.py"))
    for banned in ("CashAllocation", "CashDelivery", "cash_service", "app.models.payment", "app.models.loan ", "session.balance",
                   "CreditPaymentApplication", "CreditLoanObligation", "record_cash", "outbox", "accounting"):
        assert banned not in src, banned
    # T-020 READS the reversal a refund is linked to; custody never creates or changes one
    assert "CreditPaymentReversal(" not in src and "update(CreditPaymentReversal" not in src
    assert "deposit_field_rendition(" in src and "UPDATE cash_sessions" not in src
    assert custody_service.cash_port.FIELD_RENDITION_KIND == KIND
    legacy = (ROOT / "app/services/cash_service.py").read_text(encoding="utf-8")
    assert "credit_field_rendition" not in legacy  # the legacy reversal list never learns the modern kind
    t009 = (ROOT / "docs/T-009-CREDIT-PAYMENT-REVERSAL.md").read_text(encoding="utf-8")
    assert "superseded by T-019" in t009


def test_migration_0018_upgrade_downgrade_reupgrade_and_refusal_with_history(scratch_db):
    assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    assert _alembic(scratch_db, "check").returncode == 0
    assert "0019" in _alembic(scratch_db, "heads").stdout  # T-020 added 0019 on top of 0018
    assert _alembic(scratch_db, "downgrade", "0017").returncode == 0  # empty: clean
    assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    from sqlalchemy import create_engine

    eng = create_engine(scratch_db)
    with eng.begin() as c:
        c.execute(text("SET session_replication_role = replica"))  # bypass FKs/triggers only to plant a history row
        c.execute(text("INSERT INTO credit_field_renditions (id, tenant_id, rendition_number, receiving_branch_id, custodian_user_id, "
                       "currency_code, declared_amount, state, declared_by, declared_at, create_idempotency_key, create_request_digest) "
                       "VALUES (1, 1, 'REN-1', 1, 1, 'DOP', 1, 'declared', 1, now(), 'k-123456789012', 'd')"))
    eng.dispose()
    out = _alembic(scratch_db, "downgrade", "0017")
    assert out.returncode != 0 and "Cannot downgrade 0018" in out.stderr
    eng = create_engine(scratch_db)
    with eng.connect() as c:  # nothing dropped, nothing erased
        assert c.execute(text("SELECT count(*) FROM permissions WHERE code IN ('cash.field_custody.read', "
                              "'cash.field_custody.render', 'cash.field_custody.accept')")).scalar() == 3
        assert c.execute(text("SELECT count(*) FROM credit_field_renditions")).scalar() == 1
        assert c.execute(text("SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg_credit_field%' AND tgname NOT LIKE 'trg_credit_field_refunds%'")).scalar() == 8
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0019"  # the refused chain rolls back
    eng.dispose()


def test_an_inactive_custodian_keeps_custody_and_a_declared_rendition_can_still_be_accepted(client, sink, tenant_a, monkeypatch):
    x = world(client, sink, tenant_a, monkeypatch)
    r = declare(client, x.col_h, x.b, [fpay(client, x.col_h, x.w, "14.00")["id"]])
    sql("UPDATE users SET status = 'disabled' WHERE id = :u", u=x.col)  # C17 / C18: the collector leaves after declaring
    assert client.get(f"{R}/{r['id']}", headers=x.cas_h).json()["custodian_user_id"] == x.col  # custody never transferred
    acc = decide(client, x.cas_h, r["id"], "accept", cash_session_id=x.sess, counted_amount="14.00")
    assert acc["state"] == "accepted" and len(movements()) == 1
