"""T-023A same-CashPoint session handover / opening from handover (T023A-*). PostgreSQL only.

A closing session may leave its exact counted cash with a NAMED next cashier of the SAME CashPoint. The receiver recounts it
(a fresh full denomination map whose total must equal the handover) and, in ONE transaction, the source closes and the
receiver's own session opens (``opening_source = handover``). Cash moves between two sessions: a negative
``session_handover_out`` and a positive ``opening_handover_fund`` (aggregate 0), no capital, no opening difference; the source
difference stays exactly as counted. Decline is an immutable annotation; redirect cancels + replaces + points forward in one
step (direct -> direct, or direct -> capital with no way back). Every command owns a tenant-global key (replay, savepoint claim,
named-constraint classification). Every invariant has a database backstop; the downgrade is lossless-only.
"""

import hashlib
import importlib.util
import json
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.db import SessionLocal
from app.core.errors import IdempotencyConflict
from app.models.cash import CashSession, CashSessionHandover
from app.modules.cash import ddl, handover_ddl, session_handovers
from app.modules.cash import sessions as cash_core
from app.modules.identity import admin as identity_admin
from app.modules.identity.authorization import build_principal
from app.modules.identity.catalog import CATALOG, CATALOG_CODES
from app.modules.identity.errors import PermissionDenied
from app.modules.identity.models import UserAccount
from tests import pg_env  # noqa: F401  (must precede app imports)
from tests.test_t001_foundation import _alembic, scratch_db  # noqa: F401
from tests.test_t002_identity import (  # noqa: F401  (fixtures + helpers shared with the earlier suites)
    admin_headers,
    client,
    events,
    fresh_db,
    sink,
    tenant_a,
    tenant_b,
)
from tests.test_t003_organization import mk_cp
from tests.test_t006_origination import count
from tests.test_t012_collection_assignment import mkuser
from tests.test_t021_cashpoint_session_lifecycle import (
    ACCEPT,
    CASH,
    CLOSE,
    OPEN,
    READ,
    V2,
    _plant,
    accept_,
    capital_rows,
    code,
    cworld,
    key,
    movements,
    open_,
    refused,
    sql,
    suspend,
)
from tests.test_t021h_cash_idempotency_hardening import blind, failing_flush, race, seam, verdict

ROOT = Path(__file__).resolve().parent.parent
RECEIVE, REDIRECT = "cash.handovers.receive", "cash.handovers.redirect"
SH = f"{CASH}/session-handovers"
REASON = "El conteo del receptor no coincide con lo entregado"
SUP_REASON = "Reasignacion por supervision de la agencia"
N30 = {"1000": 30}  # 30,000


# ================================ harness =================================================================
def hworld(client, sink, tenant, tag="a"):
    """cworld (cashier ``cas``, capital receiver ``rec``) + a direct receiver, a second one and a supervisor."""
    x = cworld(client, sink, tenant, tag)
    for name, perms in (
        ("nxt", [RECEIVE, OPEN, CLOSE, READ]),
        ("alt", [RECEIVE, OPEN, READ]),
        ("sup", [REDIRECT, RECEIVE, OPEN, READ, ACCEPT]),
    ):
        h, uid = mkuser(client, sink, x.adm, tenant, f"{name}-{tag}@x.com", perms, scope="branch", branch_id=x.b)
        setattr(x, f"{name}_h", h)
        setattr(x, name, uid)
    return x


def dclose(client, hdr, sid, counts, receiver, note=None, expect=200, k=None, destination="next_session"):
    body = {"idempotency_key": k or key("t23"), "denominations": counts, "destination": destination}
    if receiver is not None:
        body["receiver_user_id"] = receiver
    if note is not None:
        body["observation_note"] = note
    r = client.post(f"{CASH}/sessions/{sid}/close", headers=hdr, json=body)
    assert r.status_code == expect, f"close: {r.status_code} {r.text}"
    return r


def daccept(client, hdr, hid, counts, expect=200, k=None):
    r = client.post(
        f"{SH}/{hid}/accept", headers=hdr, json={"idempotency_key": k or key("t23"), "denominations": counts}
    )
    assert r.status_code == expect, f"accept: {r.status_code} {r.text}"
    return r


def ddecline(client, hdr, hid, reason=REASON, expect=200, k=None):
    r = client.post(f"{SH}/{hid}/decline", headers=hdr, json={"idempotency_key": k or key("t23"), "reason": reason})
    assert r.status_code == expect, f"decline: {r.status_code} {r.text}"
    return r


def dredirect(client, hdr, hid, destination, receiver, reason=SUP_REASON, expect=200, k=None):
    body = {
        "idempotency_key": k or key("t23"),
        "destination": destination,
        "receiver_user_id": receiver,
        "reason": reason,
    }
    r = client.post(f"{SH}/{hid}/redirect", headers=hdr, json=body)
    assert r.status_code == expect, f"redirect: {r.status_code} {r.text}"
    return r


def declared(client, x, notes=30, receiver=None, amount_extra=0):
    """An open capital session closed with the exact count: a pending direct handover (default receiver ``nxt``)."""
    s = open_(client, x.cas_h, x.cp, amount=f"{notes * 1000 + amount_extra}.00").json()
    closed = dclose(client, x.cas_h, s["id"], {"1000": notes}, receiver or x.nxt, note="Conteo del cierre").json()
    return s["id"], closed["session_handover"]["id"], closed


def q(statement, **params):
    with SessionLocal() as db:
        return db.execute(text(statement), params).all()


def handover_row(hid):
    return q("SELECT * FROM cash_session_handovers WHERE id = :i", i=hid)[0]._mapping


def session_row(sid):
    return q("SELECT * FROM cash_sessions WHERE id = :i", i=sid)[0]._mapping


def money():
    return {
        t: count(t)
        for t in (
            "cash_movements",
            "capital_movements",
            "cash_sessions",
            "cash_custody_transfers",
            "cash_session_handovers",
            "cash_session_differences",
        )
    }


def tx(*statements):
    """Run raw statements in ONE transaction and commit (the deferred backstops run at COMMIT)."""
    with SessionLocal() as db:
        for statement, params in statements:
            db.execute(text(statement), params)
        db.commit()


def actor_for(db, user_id):
    return build_principal(db, db.get(UserAccount, user_id), 0)


def j_accept(hid, counts, k):
    return lambda db, actor: session_handovers.accept_session_handover(
        db, actor, hid, idempotency_key=k, denominations=counts
    )


def j_decline(hid, k):
    return lambda db, actor: session_handovers.decline_session_handover(
        db, actor, hid, idempotency_key=k, reason=REASON
    )


def j_redirect(hid, k, destination, receiver):
    return lambda db, actor: session_handovers.redirect_session_handover(
        db, actor, hid, idempotency_key=k, destination=destination, receiver_user_id=receiver, reason=SUP_REASON
    )


def insert_handover(x, sid, **override):
    v = {
        "tenant_id": x.t,
        "box_id": x.box,
        "cash_point_id": x.cp,
        "source_session_id": sid,
        "from_user_id": x.cas,
        "to_user_id": x.alt,
        "amount": Decimal("30000.00"),
        "currency_code": "DOP",
        "state": "pending",
    } | override
    return (
        "INSERT INTO cash_session_handovers (tenant_id, box_id, cash_point_id, source_session_id, from_user_id, "
        "to_user_id, amount, currency_code, state, created_at, version) VALUES (:tenant_id, :box_id, :cash_point_id, "
        ":source_session_id, :from_user_id, :to_user_id, :amount, :currency_code, :state, now(), 1)"
    ), v


def insert_session(x, **override):
    v = {
        "box_id": x.box,
        "tenant_id": x.t,
        "cash_point_id": x.cp,
        "src": "handover",
        "hid": None,
        "who": x.nxt,
        "exp": Decimal("30000.00"),
        "cnt": Decimal("30000.00"),
        "dens": json.dumps(N30),
        "okey": None,
    } | override
    return (
        "INSERT INTO cash_sessions (box_id, tenant_id, cash_point_id, currency_code, business_date, state, opening_source, "
        "opening_contract, opening_expected, opening_counted, opening_denominations, opening_handover_id, balance, "
        "balance_base, snapshot, denominations, notes, opened_by, cashier_id, opened_at, open_idempotency_key, version) "
        "VALUES (:box_id, :tenant_id, :cash_point_id, 'DOP', current_date, 'open', :src, 'v2', :exp, :cnt, "
        "CAST(:dens AS jsonb), :hid, 0, 0, '{}', '{}', '', :who, :who, now(), :okey, 1)"
    ), v


def keys_in(value, found=None):
    """Every dict key anywhere in a JSON-like value."""
    found = set() if found is None else found
    if isinstance(value, dict):
        for k, v in value.items():
            found.add(k)
            keys_in(v, found)
    elif isinstance(value, list):
        for v in value:
            keys_in(v, found)
    return found


# ================================ 1. declaration (D2, D1) =================================================
def test_a_direct_declaration_freezes_the_counted_cash_on_the_same_cash_point(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    s = open_(client, x.cas_h, x.cp, amount="31000.00").json()
    closed = dclose(client, x.cas_h, s["id"], N30, x.nxt, note="Faltan 1000 en el conteo").json()
    sh = closed["session_handover"]
    assert closed["state"] == "closing" and closed["handover"] is None  # the capital handover field stays capital-only
    assert (closed["closing_expected"], closed["counted"], closed["difference"]) == ("31000.00", "30000.00", "-1000.00")
    assert (sh["state"], sh["amount"], sh["from_user_id"], sh["to_user_id"], sh["cash_point_id"]) == (
        "pending",
        "30000.00",
        x.cas,
        x.nxt,
        x.cp,
    )  # the physical count is frozen, never the expected 31,000
    assert closed["next_action"] == {
        "action": "accept_session_handover",
        "session_handover_id": sh["id"],
        "receiver_user_id": x.nxt,
        "endpoint": f"{CASH}/session-handovers/{sh['id']}/accept",
    }
    assert count("cash_custody_transfers", "session_id = :s", s=s["id"]) == 0  # the capital table is untouched
    assert [(d["phase"], d["difference"], d["status"]) for d in closed["differences"]] == [
        ("closing", "-1000.00", "pending_review")
    ]  # the source difference is a record, never netted into the handover
    row = handover_row(sh["id"])
    assert (row["cash_point_id"], row["box_id"], row["tenant_id"], row["state"]) == (x.cp, x.box, x.t, "pending")
    assert movements(s["id"], "session_handover_out") == []  # nothing moves until the receiver confirms
    (declared_ev,) = events("cash.session_handover.declared")
    assert declared_ev.details["session_handover_id"] == sh["id"] and declared_ev.subject_id == x.nxt
    (closing_ev,) = events("cash.session.closing_counted")
    assert closing_ev.details["session_handover_id"] == sh["id"] and closing_ev.details["handover_id"] is None
    # an ordinary open on the CashPoint keeps failing while the source is closing: the source itself reserves it
    assert code(open_(client, x.cas_h, x.cp, source="zero", amount="0", expect=409)) == "cash_point_busy"


def test_the_declaration_names_a_valid_receiver_of_the_same_cash_point_or_nothing_is_written(client, sink, tenant_a):
    x, other = hworld(client, sink, tenant_a), hworld(client, sink, tenant_a, "o")
    s = open_(client, x.cas_h, x.cp, amount="30000.00").json()
    _h, only_receive = mkuser(
        client, sink, x.adm, tenant_a, "rcv-only@x.com", [RECEIVE, READ], scope="branch", branch_id=x.b
    )
    _h2, open_only = mkuser(
        client, sink, x.adm, tenant_a, "open-only@x.com", [OPEN, READ], scope="branch", branch_id=x.b
    )
    before = money()
    for receiver, expected in (
        (open_only, "invalid_receiver"),  # open without receive
        (None, "receiver_required"),
        (x.cas, "invalid_receiver"),  # never the maker
        (x.rec, "invalid_receiver"),  # a capital receiver (accept) is not a direct receiver
        (only_receive, "invalid_receiver"),  # receive without open
        (other.nxt, "invalid_receiver"),  # holds both, but for another branch: the handover stays on THIS CashPoint
        (999_999, "invalid_receiver"),
    ):
        assert code(dclose(client, x.cas_h, s["id"], N30, receiver, expect=422)) == expected
    assert code(dclose(client, x.cas_h, s["id"], {"1000": 0}, x.nxt, expect=422)) == "next_session_requires_cash"
    assert dclose(client, x.cas_h, s["id"], N30, x.nxt, destination="elsewhere", expect=422)
    assert money() == before and session_row(s["id"])["state"] == "open"  # not a single row written by the refusals
    assert dclose(client, x.cas_h, s["id"], N30, x.nxt).json()["state"] == "closing"
    # a cashier who holds receive + open is STILL never the receiver of their own handover
    cp2 = mk_cp(client, x.adm, x.b, "SELF", currencies=["DOP"])["id"]
    me_h, me = mkuser(
        client, sink, x.adm, tenant_a, "cas2-self@x.com", [OPEN, CLOSE, READ, RECEIVE], scope="branch", branch_id=x.b
    )
    own = open_(client, me_h, cp2, amount="30000.00").json()["id"]
    assert code(dclose(client, me_h, own, N30, me, expect=422)) == "invalid_receiver"
    assert session_row(own)["state"] == "open"


def test_the_database_refuses_a_direct_handover_that_is_not_for_its_closing_session(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    sid, _hid, _ = declared(client, x)
    cp2 = mk_cp(client, x.adm, x.b, "OTRA", currencies=["DOP"])["id"]
    free = open_(client, x.cas_h, cp2, amount="1000.00").json()["id"]  # an OPEN session on another CashPoint
    cases = (
        (insert_handover(x, sid, cash_point_id=cp2), "same cash point"),  # a different CashPoint: never
        (insert_handover(x, sid, to_user_id=x.cas), "other than its maker"),
        (insert_handover(x, sid, amount=Decimal("29999.00")), "freezes exactly its counted cash"),
        (insert_handover(x, sid, from_user_id=x.rec), "from its cashier"),
        (insert_handover(x, sid, state="confirmed"), "born pending"),
        (insert_handover(x, free, cash_point_id=cp2), "closing session"),
    )
    for (statement, params), match in cases:
        refused(statement, match, **params)
    assert count("cash_session_handovers") == 1
    # one non-cancelled handover per source session, whatever its receiver
    refused(*insert_handover(x, sid)[:1], match="uq_cash_session_handovers_live", **insert_handover(x, sid)[1])


# ================================ 2. accept = close source + open receiver, atomically ====================
def test_accepting_recounts_the_cash_and_swaps_the_two_sessions_with_no_capital_effect(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    s = open_(client, x.cas_h, x.cp, amount="31000.00").json()
    hid = dclose(client, x.cas_h, s["id"], N30, x.nxt, note="Faltan 1000 en el conteo").json()["session_handover"]["id"]
    capital_before = money()["capital_movements"]
    receiver_count = {"1000": 29, "500": 2}  # a DIFFERENT composition with the same 30,000 total
    out = daccept(client, x.nxt_h, hid, receiver_count).json()
    sh, src, new = out["session_handover"], out["source_session"], out["receiving_session"]
    assert (sh["state"], sh["accepted_by"], sh["receiving_session_id"], sh["receiver_denominations"]) == (
        "confirmed",
        x.nxt,
        new["id"],
        receiver_count,
    )
    assert out["replayed"] is False and out["replacement"] is None
    # the source: closed, difference preserved EXACTLY, balance = expected - handed over
    assert (src["state"], src["counted"], src["closing_expected"], src["difference"], src["balance"]) == (
        "closed",
        "30000.00",
        "31000.00",
        "-1000.00",
        "1000.00",
    )
    assert [(d["phase"], d["difference"], d["status"]) for d in src["differences"]] == [
        ("closing", "-1000.00", "pending_review")
    ]
    # the receiving session: its own, open, same CashPoint, born from the handover, no opening difference
    assert (new["state"], new["cashier_user_id"], new["opened_by"], new["cash_point_id"]) == (
        "open",
        x.nxt,
        x.nxt,
        x.cp,
    )
    assert (new["opening_source"], new["opening_handover_id"], new["opening_contract"]) == ("handover", hid, "v2")
    assert (new["opening_expected"], new["opening_counted"], new["balance"]) == ("30000.00", "30000.00", "30000.00")
    assert new["opening_denominations"] == receiver_count and new["differences"] == []
    assert count("cash_session_differences", "session_id = :s", s=new["id"]) == 0
    row = session_row(new["id"])
    assert (row["open_idempotency_key"], row["open_request_digest"]) == (
        None,
        None,
    )  # the accept command owns idempotency
    # exactly one movement on each side, equal and opposite; capital untouched; aggregate cash effect zero
    (out_mv,) = movements(s["id"], "session_handover_out")
    (in_mv,) = movements(new["id"], "opening_handover_fund")
    assert (out_mv.amount, in_mv.amount) == (Decimal("-30000.00"), Decimal("30000.00"))
    assert q("SELECT coalesce(sum(amount), 0) FROM cash_movements WHERE session_handover_id = :h", h=hid)[0][0] == 0
    assert q("SELECT count(*) FROM cash_movements WHERE session_handover_id = :h", h=hid)[0][0] == 2
    assert money()["capital_movements"] == capital_before and capital_rows("from_cash") == []
    # one confirmed handover -> one receiving session
    assert count("cash_sessions", "opening_handover_id = :h", h=hid) == 1
    (accepted,) = events("cash.session_handover.accepted")
    assert accepted.details["receiving_session_id"] == new["id"] and accepted.actor_id == x.nxt
    opened = [e for e in events("cash.session.opened") if e.details.get("opening_source") == "handover"]
    assert len(opened) == 1 and opened[0].details["session_id"] == new["id"] and opened[0].actor_id == x.nxt
    # an unresolved source difference never blocked it; it is now resolvable (the source is closed)
    assert client.get(f"{CASH}/sessions/{s['id']}", headers=x.cas_h).json()["state"] == "closed"


def test_a_receiver_count_that_differs_by_one_note_is_refused_with_zero_side_effects(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    sid, hid, _ = declared(client, x)
    before, k = money(), key("t23")
    for counts in ({"1000": 29, "500": 1}, {"1000": 30, "500": 1}, {"1000": 29}):  # 29,500 / 30,500 / 29,000
        assert code(daccept(client, x.nxt_h, hid, counts, expect=422, k=k)) == "receiver_count_mismatch"
    assert code(daccept(client, x.nxt_h, hid, {"1000": 0}, expect=422, k=k)) == "receiver_count_mismatch"
    assert money() == before  # no session, no CashMovement, no CapitalMovement, no opening difference
    assert handover_row(hid)["state"] == "pending" and handover_row(hid)["accept_idempotency_key"] is None
    assert session_row(sid)["state"] == "closing"
    ok = daccept(client, x.nxt_h, hid, N30, k=k).json()  # the refused attempts did not consume the key
    assert ok["replayed"] is False and ok["session_handover"]["state"] == "confirmed"


def test_only_the_named_receiver_accepts_and_nobody_accepts_on_their_behalf(client, sink, tenant_a, tenant_b):
    x = hworld(client, sink, tenant_a)
    sid, hid, _ = declared(client, x)
    assert code(daccept(client, x.alt_h, hid, N30, expect=403)) == "not_named_receiver"  # same permissions, not named
    assert code(daccept(client, x.sup_h, hid, N30, expect=403)) == "not_named_receiver"
    assert code(daccept(client, x.adm, hid, N30, expect=403)) in {
        "not_named_receiver",
        "permission_denied",
    }  # no admin bypass
    assert (
        code(daccept(client, x.cas_h, hid, N30, expect=403)) == "permission_denied"
    )  # the maker holds neither receive...
    assert code(daccept(client, admin_headers(client, tenant_b), hid, N30, expect=404)) == "session_handover_not_found"
    assert code(daccept(client, x.nxt_h, 999_999, N30, expect=404)) == "session_handover_not_found"
    assert count("cash_sessions", "opening_source = 'handover'") == 0 and session_row(sid)["state"] == "closing"
    # the maker may even hold the receive+open permissions: still never their own handover (service AND database)
    with SessionLocal() as db:
        maker = actor_for(db, x.cas)
        maker = type(maker)(maker.user_id, maker.tenant_id, None, 0, tuple(maker.grants) + actor_for(db, x.nxt).grants)
        with pytest.raises(Exception) as bad:
            session_handovers.accept_session_handover(db, maker, hid, idempotency_key=key("t23"), denominations=N30)
        assert type(bad.value).__name__ == "MakerCannotAccept"
        db.rollback()
    refused(
        *insert_handover(x, sid, to_user_id=x.cas)[:1],
        match="other than its maker",
        **insert_handover(x, sid, to_user_id=x.cas)[1],
    )


def test_permissions_and_status_are_revalidated_at_acceptance(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    sid, hid, _ = declared(client, x)
    with SessionLocal() as db:
        stale = actor_for(db, x.nxt)  # the request principal predates the revocation
    sql("UPDATE user_role_assignments SET revoked_at = now() WHERE user_id = :u", u=x.nxt)
    before = money()
    with SessionLocal() as db, pytest.raises(PermissionDenied):
        session_handovers.accept_session_handover(db, stale, hid, idempotency_key=key("t23"), denominations=N30)
    # declining custody needs no permission: the same revoked receiver may still refuse it
    with SessionLocal() as db:
        out = session_handovers.decline_session_handover(db, stale, hid, idempotency_key=key("t23"), reason=REASON)
        db.commit()
    assert out["session_handover"]["declined"] is True and money() == before
    # a receiver disabled between request and acceptance
    y = hworld(client, sink, tenant_a, "d")
    sid2, hid2, _ = declared(client, y)
    with SessionLocal() as db:
        stale = actor_for(db, y.nxt)
    sql("UPDATE users SET status = 'disabled' WHERE id = :u", u=y.nxt)
    with SessionLocal() as db, pytest.raises(Exception) as bad:
        session_handovers.accept_session_handover(db, stale, hid2, idempotency_key=key("t23"), denominations=N30)
    assert type(bad.value).__name__ == "InvalidReceiver"
    assert handover_row(hid2)["state"] == "pending" and session_row(sid2)["state"] == "closing"


def test_a_suspended_cash_point_refuses_the_acceptance_but_not_the_recovery(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    sid, hid, _ = declared(client, x)
    suspend(client, x.adm, x.cp)
    before = money()
    assert code(daccept(client, x.nxt_h, hid, N30, expect=409)) == "cash_point_not_active"
    assert money() == before and handover_row(hid)["state"] == "pending"
    assert client.post(f"{V2}/cash-points/{x.cp}/resume", headers=x.adm).status_code == 200
    assert daccept(client, x.nxt_h, hid, N30).json()["session_handover"]["state"] == "confirmed"
    # recovery while suspended: the redirect to capital is not an opening
    y = hworld(client, sink, tenant_a, "s")
    sid2, hid2, _ = declared(client, y)
    suspend(client, y.adm, y.cp)
    out = dredirect(client, y.cas_h, hid2, "capital", y.rec).json()
    assert out["session_handover"]["state"] == "cancelled" and out["replacement"]["type"] == "capital_handover"


# ================================ 3. decline ==============================================================
def test_decline_is_an_immutable_annotation_and_a_declined_row_can_never_be_accepted(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    sid, hid, _ = declared(client, x)
    k = key("t23")
    out = ddecline(client, x.nxt_h, hid, k=k).json()
    sh = out["session_handover"]
    assert (sh["state"], sh["declined"], sh["declined_by"], sh["decline_reason"]) == ("pending", True, x.nxt, REASON)
    assert out["replayed"] is False and out["source_session"]["state"] == "closing"
    assert out["source_session"]["next_action"]["action"] == "redirect_session_handover"
    assert ddecline(client, x.nxt_h, hid, k=k).json()["replayed"] is True  # same command: a replay
    assert (
        code(ddecline(client, x.nxt_h, hid, k=k, reason="Otro motivo distinto del anterior", expect=409))
        == "idempotency_conflict"
    )
    assert code(ddecline(client, x.nxt_h, hid, expect=409)) == "handover_declined"  # already declined (another key)
    assert code(daccept(client, x.nxt_h, hid, N30, expect=409)) == "handover_declined"
    assert code(ddecline(client, x.alt_h, hid, expect=403)) == "not_named_receiver"
    assert code(ddecline(client, x.nxt_h, hid, reason="corto", expect=422)) == "handover_reason_required"
    assert code(ddecline(client, x.nxt_h, 999_999, expect=404)) == "session_handover_not_found"
    assert handover_row(hid)["state"] == "pending" and count("cash_movements", "session_handover_id IS NOT NULL") == 0
    (ev,) = events("cash.session_handover.declined")
    assert "reason" not in ev.details and "decline_reason" not in ev.details  # ids and structured facts only


# ================================ 4. redirect =============================================================
def test_redirect_authority_chain_and_forward_pointers(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    sid, h1, _ = declared(client, x)
    assert code(dredirect(client, x.alt_h, h1, "next_session", x.sup, expect=403)) == "redirect_not_authorized"
    for receiver, why in ((x.cas, "maker"), (x.rec, "no receive/open"), (999_999, "unknown")):
        assert code(dredirect(client, x.cas_h, h1, "next_session", receiver, expect=422)) == "invalid_receiver", why
    assert (
        code(dredirect(client, x.cas_h, h1, "capital", x.alt, expect=422)) == "invalid_receiver"
    )  # not a capital receiver
    assert code(dredirect(client, x.cas_h, h1, "capital", x.cas, expect=422)) == "invalid_receiver"  # never the maker
    assert (
        code(dredirect(client, x.cas_h, h1, "next_session", x.alt, reason="corto", expect=422))
        == "handover_reason_required"
    )
    # BEFORE a decline the maker may redirect (direct -> direct); the old row is cancelled and points forward
    shared = key("t23")
    out = dredirect(client, x.cas_h, h1, "next_session", x.alt, k=shared).json()
    h2 = out["replacement"]["handover"]["id"]
    assert out["replacement"]["type"] == "session_handover" and out["replacement"]["handover"]["state"] == "pending"
    old = handover_row(h1)
    assert (old["state"], old["redirected_by"], old["redirect_reason"]) == ("cancelled", x.cas, SUP_REASON)
    assert (old["redirected_to_session_handover_id"], old["redirected_to_capital_handover_id"]) == (h2, None)
    assert out["source_session"]["session_handover"]["id"] == h2 and out["source_session"]["state"] == "closing"
    assert dredirect(client, x.cas_h, h1, "next_session", x.alt, k=shared).json()["replayed"] is True
    assert code(dredirect(client, x.cas_h, h1, "next_session", x.nxt, expect=409)) == "session_handover_not_pending"
    assert (
        code(daccept(client, x.nxt_h, h1, N30, expect=409)) == "session_handover_not_pending"
    )  # a cancelled row never accepts
    assert code(ddecline(client, x.nxt_h, h1, expect=409)) == "session_handover_not_pending"
    # the new receiver declines: from now on the maker ALONE may not redirect the disputed cash
    ddecline(client, x.alt_h, h2, k=shared)  # decline and redirect keys live in independent namespaces
    assert code(dredirect(client, x.cas_h, h2, "next_session", x.nxt, expect=403)) == "redirect_not_authorized"
    assert code(dredirect(client, x.cas_h, h2, "capital", x.rec, expect=403)) == "redirect_not_authorized"
    assert handover_row(h2)["state"] == "pending"
    # supervision (cash.handovers.redirect) may: back to the FIRST receiver, as a NEW immutable row (chain h1 -> h2 -> h3)
    out = dredirect(client, x.sup_h, h2, "next_session", x.nxt).json()
    h3 = out["replacement"]["handover"]["id"]
    assert h3 not in (h1, h2)
    rows = {r: handover_row(r) for r in (h1, h2, h3)}
    assert [rows[r]["state"] for r in (h1, h2, h3)] == ["cancelled", "cancelled", "pending"]
    assert (rows[h1]["redirected_to_session_handover_id"], rows[h2]["redirected_to_session_handover_id"]) == (h2, h3)
    assert (
        rows[h2]["declined_by"] == x.alt and rows[h2]["decline_reason"] == REASON
    )  # the declined row is untouched history
    assert rows[h2]["redirected_by"] == x.sup
    assert (
        q("SELECT count(*) FROM cash_session_handovers WHERE source_session_id = :s AND state <> 'cancelled'", s=sid)[
            0
        ][0]
        == 1
    )
    # the chain is derivable from the unique forward pointers; the live row is the one shown on the session
    assert client.get(f"{CASH}/sessions/{sid}", headers=x.cas_h).json()["session_handover"]["id"] == h3
    got = [i["id"] for i in client.get(SH, headers=x.sup_h, params={"state": "cancelled"}).json()["items"]]
    assert got == [h2, h1]
    # the third row is accepted normally
    assert daccept(client, x.nxt_h, h3, N30).json()["session_handover"]["state"] == "confirmed"
    events_redirect = events("cash.session_handover.redirected")
    assert len(events_redirect) == 2 and all("reason" not in e.details for e in events_redirect)


def test_a_direct_handover_may_leave_for_capital_and_never_comes_back(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    sid, h1, _ = declared(client, x)
    ddecline(client, x.nxt_h, h1)
    capital_before = money()["capital_movements"]
    out = dredirect(client, x.sup_h, h1, "capital", x.rec).json()
    cap = out["replacement"]["handover"]
    assert (
        out["replacement"]["type"] == "capital_handover" and cap["state"] == "pending" and cap["amount"] == "30000.00"
    )
    old = handover_row(h1)
    assert (old["state"], old["redirected_to_session_handover_id"], old["redirected_to_capital_handover_id"]) == (
        "cancelled",
        None,
        cap["id"],
    )
    kind = q(
        "SELECT kind, session_id, to_user_id, from_user_id, provenance FROM cash_custody_transfers WHERE id = :i",
        i=cap["id"],
    )[0]
    assert tuple(kind) == (
        "closing_capital",
        sid,
        x.rec,
        x.cas,
        "v2",
    )  # an ordinary T-021 row: same session, same frozen cash
    session = client.get(f"{CASH}/sessions/{sid}", headers=x.cas_h).json()
    assert session["handover"]["id"] == cap["id"] and session["session_handover"] is None
    assert session["next_action"]["action"] == "accept_closing_handover"
    # no route back: the cancelled row and its detail cannot be redirected / accepted again, and T-021 rules apply from here
    assert code(dredirect(client, x.sup_h, h1, "next_session", x.nxt, expect=409)) == "session_handover_not_pending"
    assert code(daccept(client, x.nxt_h, h1, N30, expect=409)) == "session_handover_not_pending"
    assert (
        code(client.post(f"{CASH}/handovers/{cap['id']}/accept", headers=x.nxt_h, json={"idempotency_key": key("t23")}))
        == "permission_denied"
    )
    done = accept_(client, x.rec_h, cap["id"]).json()  # the existing T-021 endpoint: named receiver, no recount
    assert done["state"] == "closed" and done["handover"]["state"] == "confirmed"
    refused(
        *insert_handover(x, sid, to_user_id=x.alt)[:1],
        match="closing session",
        **insert_handover(x, sid, to_user_id=x.alt)[1],
    )
    assert money()["capital_movements"] == capital_before + 1 and len(capital_rows("from_cash")) == 1
    assert handover_row(h1)["state"] == "cancelled"  # the historical direct row stays exactly as redirected
    listed = client.get(f"{CASH}/handovers", headers=x.rec_h, params={"state": "confirmed"}).json()["items"]
    assert [i["id"] for i in listed] == [cap["id"]]  # the capital list is still capital-only


# ================================ 5. snapshot, active slot, history ========================================
def test_a_handover_opened_session_closes_with_the_right_snapshot(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    sid, hid, _ = declared(client, x)
    new = daccept(client, x.nxt_h, hid, N30).json()["receiving_session"]
    closed = client.post(
        f"{CASH}/sessions/{new['id']}/close",
        headers=x.nxt_h,
        json={"idempotency_key": key("t23"), "denominations": N30, "receiver_user_id": x.rec},
    )
    assert closed.status_code == 200 and closed.json()["state"] == "closing"
    snap = q("SELECT snapshot FROM cash_sessions WHERE id = :i", i=new["id"])[0][0]
    # opening_handover_fund is OPENING cash: not operating incoming (D6)
    assert (snap["opening"], snap["incoming"], snap["outgoing"], snap["expected"]) == (
        "30000.00",
        "0.00",
        "0.00",
        "30000.00",
    )
    assert snap["movement_ids"] == []
    assert accept_(client, x.rec_h, closed.json()["handover"]["id"]).json()["state"] == "closed"


def test_close_then_open_ordering_the_active_slot_is_held_end_to_end(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    sid, hid, _ = declared(client, x)
    assert (
        code(open_(client, x.cas_h, x.cp, source="zero", amount="0", expect=409)) == "cash_point_busy"
    )  # closing holds it
    new = daccept(client, x.nxt_h, hid, N30).json()["receiving_session"]
    assert (
        code(open_(client, x.cas_h, x.cp, source="zero", amount="0", expect=409)) == "cash_point_busy"
    )  # the new one does
    assert (
        client.get(f"{CASH}/sessions/current", headers=x.nxt_h, params={"cash_point_id": x.cp}).json()["session"]["id"]
        == new["id"]
    )
    # the active-slot index is non-deferrable, so the source must leave it BEFORE the receiving session enters
    refused(
        *insert_session(x, hid=hid)[:1],
        match="uq_cash_sessions_active_cash_point|cash_sessions",
        **insert_session(x, hid=hid)[1],
    )
    # a user may own sessions on different CashPoints (no user-wide uniqueness is invented)
    cp2 = mk_cp(client, x.adm, x.b, "SEGUNDA", currencies=["DOP"])["id"]
    assert open_(client, x.nxt_h, cp2, source="zero", amount="0").json()["state"] == "open"


def test_ordinary_open_can_never_create_a_handover_opening_and_the_database_agrees(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    sid, hid, _ = declared(client, x)
    body = {
        "idempotency_key": key("t23"),
        "cash_point_id": x.cp,
        "source": "handover",
        "amount": "30000.00",
        "denominations": N30,
    }
    assert client.post(f"{CASH}/sessions/open", headers=x.nxt_h, json=body).status_code == 422
    with SessionLocal() as db, pytest.raises(Exception) as bad:  # the service refuses it too (not only the schema)
        cash_core_open = __import__("app.modules.cash.sessions", fromlist=["open_session"]).open_session
        cash_core_open(
            db,
            actor_for(db, x.nxt),
            cash_point_id=x.cp,
            source="handover",
            amount="30000.00",
            denominations=N30,
            observation_note=None,
            idempotency_key=key("t23"),
        )
    assert type(bad.value).__name__ == "InvalidOpening"
    new = daccept(client, x.nxt_h, hid, N30).json()["receiving_session"]
    shut = client.post(
        f"{CASH}/sessions/{new['id']}/close",
        headers=x.nxt_h,
        json={"idempotency_key": key("t23"), "denominations": N30, "receiver_user_id": x.rec},
    ).json()
    accept_(
        client, x.rec_h, shut["handover"]["id"]
    )  # the slot is free again: only the unique index can refuse a 2nd session
    # database backstops of the `handover` opening source
    bad_sessions = (
        (insert_session(x, hid=None), "handover opening needs its confirmed"),  # source handover without its handover
        (
            insert_session(x, src="capital", hid=hid),
            "handover_opening_consistent",
        ),  # a handover id on a capital opening
        (insert_session(x, hid=hid, okey="k" * 12), "handover opening needs its confirmed"),  # no open key of its own
        (insert_session(x, hid=hid, exp=Decimal("29999.00")), "handover opening needs its confirmed"),  # exact amount
        (
            insert_session(
                x,
                hid=hid,
                cnt=Decimal("29999.00"),
                dens=json.dumps({"1000": 29, "500": 1, "200": 2, "50": 1, "25": 1, "10": 2, "1": 4}),
            ),
            "handover opening needs its confirmed",
        ),  # a coherent 29,999 count: only the handover amount refuses it
        (
            insert_session(x, hid=hid, dens=json.dumps({"1000": 29})),
            "handover opening needs its confirmed",
        ),  # exact count
        (insert_session(x, hid=hid, who=x.rec), "handover opening needs its confirmed"),  # the receiver owns it
        (insert_session(x, hid=hid), "uq_cash_sessions_opening_handover"),  # one receiving session per handover
    )
    for (statement, params), match in bad_sessions:
        refused(statement, match, **params)
    y = hworld(client, sink, tenant_a, "u")
    _s, pending, _ = declared(client, y)
    for statement, params, match in (
        (*insert_session(y, hid=pending), "confirmed direct handover"),  # a PENDING handover backs nothing
        (*insert_session(y, hid=pending, who=y.cas), "confirmed direct handover"),
    ):
        refused(statement, match, **params)
    assert new["opening_source"] == "handover"


def test_handover_history_cannot_be_updated_deleted_or_truncated(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    sid, h1, _ = declared(client, x)
    refused("UPDATE cash_session_handovers SET amount = 1 WHERE id = :i", "immutable", i=h1)
    refused("UPDATE cash_session_handovers SET to_user_id = :u WHERE id = :i", "immutable", i=h1, u=x.alt)
    refused("DELETE FROM cash_session_handovers WHERE id = :i", "cash history", i=h1)
    refused("TRUNCATE cash_session_handovers", "cannot be truncated|cannot truncate")
    refused(
        "TRUNCATE cash_session_handovers CASCADE", "cash_session_handovers is cash history"
    )  # OUR guard, not only the FK
    refused(
        "UPDATE cash_session_handovers SET state = 'confirmed', accepted_by = :u, accepted_at = now() WHERE id = :i",
        "ck_|claimed key",
        i=h1,
        u=x.nxt,
    )
    ddecline(client, x.nxt_h, h1)
    refused(
        "UPDATE cash_session_handovers SET decline_reason = 'Otra razon cualquiera' WHERE id = :i",
        "decline is recorded",
        i=h1,
    )
    refused(
        "UPDATE cash_session_handovers SET declined_at = NULL, declined_by = NULL, decline_idempotency_key = NULL, decline_request_digest = NULL, decline_reason = NULL WHERE id = :i",
        "decline is recorded",
        i=h1,
    )
    # a declined row cannot be claimed for acceptance (not even by the database)
    refused(
        "UPDATE cash_session_handovers SET accept_idempotency_key = 'k-claim-0001', accept_request_digest = 'sha256:x' WHERE id = :i",
        "declined_never_claimed",
        i=h1,
    )
    refused(
        "UPDATE cash_session_handovers SET state = 'confirmed', accepted_by = :u, accepted_at = now(), "
        "accept_idempotency_key = 'k-declined-001', accept_request_digest = 'sha256:x' WHERE id = :i",
        "was declined: it can never be confirmed",
        i=h1,
        u=x.nxt,
    )
    h2 = dredirect(client, x.sup_h, h1, "next_session", x.alt).json()["replacement"]["handover"]["id"]
    refused(
        "UPDATE cash_session_handovers SET redirected_to_session_handover_id = :j WHERE id = :i",
        "one forward pointer",
        i=h1,
        j=h1,
    )
    refused(
        "UPDATE cash_session_handovers SET redirected_to_capital_handover_id = 1 WHERE id = :i",
        "one forward pointer",
        i=h1,
    )
    refused(
        "UPDATE cash_session_handovers SET redirect_reason = 'Otro motivo de reasignacion' WHERE id = :i",
        "immutable",
        i=h1,
    )
    refused("UPDATE cash_session_handovers SET state = 'pending' WHERE id = :i", "cancelled history", i=h1)
    refused("DELETE FROM cash_session_handovers WHERE id = :i", "cash history", i=h2)
    # a pending row cannot point forward, and cannot be cancelled without a redirect command
    refused(
        "UPDATE cash_session_handovers SET redirected_to_session_handover_id = :j WHERE id = :i", "pointer", i=h2, j=h1
    )
    refused(
        "UPDATE cash_session_handovers SET state = 'cancelled' WHERE id = :i",
        "cancelled only by a redirect command",
        i=h2,
    )


def test_an_incomplete_confirmed_graph_is_refused_at_commit(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    sid, hid, _ = declared(client, x)
    claim = (
        "UPDATE cash_session_handovers SET accept_idempotency_key = 'k-graph-00001', accept_request_digest = 'sha256:x' WHERE id = :h",
        {"h": hid},
    )
    out = (
        "INSERT INTO cash_movements (box_id, session_id, kind, amount, actor_id, notes, reference, session_handover_id, created_at) "
        "VALUES (:b, :s, 'session_handover_out', -30000, :u, 'x', 'x', :h, now())",
        {"b": x.box, "s": sid, "u": x.nxt, "h": hid},
    )
    confirm = (
        "UPDATE cash_session_handovers SET state = 'confirmed', accepted_by = :u, accepted_at = now() WHERE id = :h",
        {"u": x.nxt, "h": hid},
    )
    close_source = ("UPDATE cash_sessions SET state = 'closed', balance = balance - 30000 WHERE id = :s", {"s": sid})
    # everything but the receiving session: COMMIT refuses the whole thing
    with pytest.raises(DBAPIError, match="exactly one receiving session"):
        tx(claim, out, confirm, close_source)
    assert handover_row(hid)["state"] == "pending" and session_row(sid)["state"] == "closing"
    # a confirmation without its source movement is refused immediately; the source cannot close without a confirmed handover
    with pytest.raises(DBAPIError, match="source movement first"):
        tx(claim, confirm)
    with pytest.raises(DBAPIError, match="closes only when its closing handover is confirmed"):
        tx(out, close_source)
    # a closing session has exactly ONE live destination: a second one (capital) next to the direct one is refused at COMMIT
    cap = (
        "INSERT INTO cash_custody_transfers (company_id, box_id, session_id, kind, from_user_id, to_user_id, amount, "
        "currency_code, provenance, state, notes, created_at, version) VALUES (:t, :b, :s, 'closing_capital', :c, :r, 30000, "
        "'DOP', 'v2', 'pending', '', now(), 1)",
        {"t": x.t, "b": x.box, "s": sid, "c": x.cas, "r": x.rec},
    )
    with pytest.raises(DBAPIError, match="exactly one live closing handover"):
        tx(cap)
    # ...and cancelling the direct row without any replacement is refused at COMMIT as well
    cancel = (
        "UPDATE cash_session_handovers SET state = 'cancelled', redirect_idempotency_key = 'k-bare-00001', "
        "redirect_request_digest = 'sha256:x', redirected_by = :u, redirected_at = now(), "
        "redirect_reason = 'Cancelacion sin reemplazo' WHERE id = :h",
        {"u": x.sup, "h": hid},
    )
    with pytest.raises(DBAPIError, match="must point to exactly one replacement|exactly one live closing handover"):
        tx(cancel)
    assert handover_row(hid)["state"] == "pending"


def manual_accept(x, sid, hid, *, skip_fund=False, skip_close=False, capital=False, skip_session=False):
    """The whole accept graph written by raw SQL in ONE transaction (positive control + single-defect variants)."""
    with SessionLocal() as db:

        def run(statement, **params):
            return db.execute(text(statement), params)

        run(
            "UPDATE cash_session_handovers SET accept_idempotency_key = :k, accept_request_digest = 'sha256:x' WHERE id = :h",
            k=key("t23g"),
            h=hid,
        )
        out_id = run(
            "INSERT INTO cash_movements (box_id, session_id, kind, amount, actor_id, notes, reference, session_handover_id, "
            "created_at) VALUES (:b, :s, 'session_handover_out', -30000, :u, 'x', 'x', :h, now()) RETURNING id",
            b=x.box,
            s=sid,
            u=x.nxt,
            h=hid,
        ).scalar()
        run(
            "UPDATE cash_session_handovers SET state = 'confirmed', accepted_by = :u, accepted_at = now() WHERE id = :h",
            u=x.nxt,
            h=hid,
        )
        if not skip_close:
            run("UPDATE cash_sessions SET state = 'closed', balance = balance - 30000 WHERE id = :s", s=sid)
        new_id = None
        if not skip_session:
            statement, params = insert_session(x, hid=hid)
            new_id = run(statement + " RETURNING id", **params).scalar()
            fund_id = None
            if not skip_fund:
                fund_id = run(
                    "INSERT INTO cash_movements (box_id, session_id, kind, amount, actor_id, notes, reference, "
                    "session_handover_id, created_at) VALUES (:b, :n, 'opening_handover_fund', 30000, :u, 'x', 'x', :h, now()) "
                    "RETURNING id",
                    b=x.box,
                    n=new_id,
                    u=x.nxt,
                    h=hid,
                ).scalar()
                run("UPDATE cash_sessions SET balance = 30000 WHERE id = :n", n=new_id)
            if capital:
                run(
                    "INSERT INTO capital_movements (company_id, kind, amount, notes, actor_id, cash_movement_id, created_at) "
                    "VALUES (:t, 'from_cash', 30000, 'x', :u, :m, now())",
                    t=x.t,
                    u=x.nxt,
                    m=fund_id or out_id,
                )
        db.commit()
        return new_id


def test_the_whole_confirmed_graph_is_validated_by_the_database_at_commit(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    cases = {
        "valid": (None, {}),
        "capital_link": ("moves no capital", {"capital": True}),
        "no_receiving_movement": ("positive receiving movement", {"skip_fund": True}),
        "source_left_closing": ("must be closed first", {"skip_close": True}),
        "no_receiving_session": ("exactly one receiving session", {"skip_session": True}),
    }
    for i, (name, (message, flags)) in enumerate(cases.items()):
        tag = f"g{i}"
        y = hworld(client, sink, tenant_a, tag)
        sid, hid, _ = declared(client, y)
        if message is None:
            new_id = manual_accept(y, sid, hid)  # the control: the same SQL, nothing missing, commits
            assert handover_row(hid)["state"] == "confirmed" and session_row(new_id)["opening_source"] == "handover"
            assert session_row(sid)["state"] == "closed"
        else:
            with pytest.raises(DBAPIError, match=message):
                manual_accept(y, sid, hid, **flags)
            assert handover_row(hid)["state"] == "pending" and session_row(sid)["state"] == "closing", name
    assert x.t == tenant_a["tenant_id"]


def test_acceptance_columns_follow_their_own_backstops(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    sid, hid, _ = declared(client, x)
    claim = (
        "UPDATE cash_session_handovers SET accept_idempotency_key = :k, accept_request_digest = 'sha256:x' WHERE id = :h",
        {"k": key("t23c"), "h": hid},
    )
    maker_confirms = (
        "UPDATE cash_session_handovers SET state = 'confirmed', accepted_by = :u, accepted_at = now() WHERE id = :h",
        {"u": x.nxt, "h": hid},
    )
    # a claimed key without a confirmation is refused at COMMIT (a claim never survives on its own)
    with pytest.raises(DBAPIError, match="exactly when it is confirmed"):
        tx(claim)
    # the confirmation is by the named receiver, never by anyone else
    for who in (x.cas, x.alt):
        with pytest.raises(DBAPIError, match="named receiver|accepted_by_receiver"):
            tx(
                claim,
                (
                    "UPDATE cash_session_handovers SET state = 'confirmed', accepted_by = :u, accepted_at = now() WHERE id = :h",
                    {"u": who, "h": hid},
                ),
            )
    assert maker_confirms and handover_row(hid)["state"] == "pending" and sid


def test_forward_pointers_form_a_chain_never_a_tree_and_never_point_to_another_sessions_row(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    sid, h1, _ = declared(client, x)
    # an OLDER confirmed handover of the same cash point and amount is not a replacement for a later session
    new = daccept(client, x.nxt_h, h1, N30).json()["receiving_session"]
    shut = client.post(
        f"{CASH}/sessions/{new['id']}/close",
        headers=x.nxt_h,
        json={"idempotency_key": key("t23"), "denominations": N30, "receiver_user_id": x.rec},
    ).json()
    accept_(client, x.rec_h, shut["handover"]["id"])
    sid3, h3, _ = declared(client, x)

    def cancel(handover, k):
        return (
            "UPDATE cash_session_handovers SET state = 'cancelled', redirect_idempotency_key = :k, "
            "redirect_request_digest = 'sha256:x', redirected_by = :u, redirected_at = now(), "
            "redirect_reason = 'Reasignacion hacia otro destino' WHERE id = :h",
            {"k": k, "u": x.sup, "h": handover},
        )

    older = (
        "UPDATE cash_session_handovers SET redirected_to_session_handover_id = :o WHERE id = :h",
        {"o": h1, "h": h3},
    )
    with pytest.raises(DBAPIError, match="redirected to a direct handover of the same cash, session and cash point"):
        tx(cancel(h3, key("t23p")), older)
    assert handover_row(h3)["state"] == "pending" and sid3 and sid
    # two cancelled rows may not point to the SAME replacement
    with SessionLocal() as db:

        def run(statement, **params):
            return db.execute(text(statement), params)

        run(*cancel(h3, key("t23p"))[:1], **cancel(h3, key("t23p"))[1])
        ins, params = insert_handover(x, sid3, to_user_id=x.alt)
        h4 = run(ins + " RETURNING id", **params).scalar()
        run(cancel(h4, key("t23p"))[0], **cancel(h4, key("t23p"))[1])
        ins, params = insert_handover(x, sid3, to_user_id=x.nxt)
        h5 = run(ins + " RETURNING id", **params).scalar()
        run("UPDATE cash_session_handovers SET redirected_to_session_handover_id = :o WHERE id = :h", o=h5, h=h3)
        with pytest.raises(IntegrityError, match="uq_cash_session_handovers_redirect_next"):
            run("UPDATE cash_session_handovers SET redirected_to_session_handover_id = :o WHERE id = :h", o=h5, h=h4)
        db.rollback()
    assert handover_row(h3)["state"] == "pending"


def test_every_declared_constraint_and_index_exists_in_the_database(client, sink, tenant_a):
    checks = [
        f"ck_cash_session_handovers_{n}"
        for n in (
            "state_valid currency_dop amount_positive receiver_not_maker accept_claim_complete accept_actor_complete "
            "confirmed_iff_accepted confirmed_has_key accepted_by_receiver declined_never_claimed declined_never_confirmed "
            "decline_complete declined_by_receiver decline_reason_required redirect_complete cancelled_iff_redirected "
            "redirect_reason_required one_forward_pointer pointer_only_when_cancelled"
        ).split()
    ] + [
        "ck_cash_sessions_handover_opening_consistent",
        "ck_cash_sessions_handover_opening_unkeyed",
        "ck_cash_sessions_opening_source_valid",
        "ck_cash_movements_handover_link_consistent",
        "ck_cash_movements_handover_out_negative",
        "ck_cash_movements_handover_fund_positive",
        "fk_cash_sessions_opening_handover_id",
        "fk_cash_movements_session_handover_id",
        "fk_cash_session_handovers_redirect_next",
        "fk_cash_session_handovers_redirect_capital",
        "fk_cash_session_handovers_tenant_cash_point",
    ]
    indexes = (
        "uq_cash_sessions_opening_handover uq_cash_movements_handover_out uq_cash_movements_handover_fund "
        "ix_cash_movements_session_handover uq_cash_session_handovers_live ix_cash_session_handovers_source_session "
        "ix_cash_session_handovers_receiver ix_cash_session_handovers_tenant uq_cash_session_handovers_accept_key "
        "uq_cash_session_handovers_decline_key uq_cash_session_handovers_redirect_key "
        "uq_cash_session_handovers_redirect_next uq_cash_session_handovers_redirect_capital"
    ).split()
    have_c = {r[0] for r in q("SELECT conname FROM pg_constraint")}
    have_i = {r[0] for r in q("SELECT indexname FROM pg_indexes")}
    assert [c for c in checks if c not in have_c] == [] and [i for i in indexes if i not in have_i] == []
    unique = {r[0] for r in q("SELECT indexname FROM pg_indexes WHERE indexdef LIKE 'CREATE UNIQUE%'")}
    assert {i for i in indexes if i.startswith("uq_")} <= unique


def test_a_confirmed_handover_and_its_opening_are_terminal_history(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    sid, hid, _ = declared(client, x)
    cp2 = mk_cp(client, x.adm, x.b, "OTRA", currencies=["DOP"])["id"]
    other_sid = open_(client, x.cas_h, cp2, amount="30000.00").json()["id"]
    other_h = dclose(client, x.cas_h, other_sid, N30, x.alt).json()["session_handover"]["id"]
    new = daccept(client, x.nxt_h, hid, N30).json()["receiving_session"]
    refused("UPDATE cash_session_handovers SET version = version + 1 WHERE id = :h", "confirmed is terminal", h=hid)
    refused("DELETE FROM cash_session_handovers WHERE id = :h", "cash history", h=hid)
    # the opening of the receiving session is immutable, including WHICH handover it came from
    refused(
        "UPDATE cash_sessions SET opening_handover_id = :o WHERE id = :n",
        "opening are immutable",
        o=other_h,
        n=new["id"],
    )
    refused("UPDATE cash_sessions SET opening_counted = 1 WHERE id = :n", "opening are immutable", n=new["id"])
    refused("UPDATE cash_sessions SET state = 'closed' WHERE id = :s", "closed is terminal", s=sid)
    # exactly one fund movement per receiving session / handover, enforced by a unique index as well
    dup = (
        "INSERT INTO cash_movements (box_id, session_id, kind, amount, actor_id, notes, reference, session_handover_id, "
        "created_at) VALUES (:b, :n, 'opening_handover_fund', 30000, :u, 'x', 'x', :h, now())"
    )
    refused(dup, "uq_cash_movements_handover_fund", b=x.box, n=new["id"], u=x.nxt, h=hid)
    refused(dup, "opening handover fund is positive", b=x.box, n=new["id"], u=x.nxt, h=other_h)  # not ITS handover
    refused(dup.replace("30000", "29999"), "opening handover fund is positive", b=x.box, n=new["id"], u=x.nxt, h=hid)
    assert other_sid and sid


def test_the_movement_backstops_of_the_two_handover_kinds(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    sid, hid, _ = declared(client, x)
    cp2 = mk_cp(client, x.adm, x.b, "OTRA", currencies=["DOP"])["id"]
    open_sid = open_(client, x.cas_h, cp2, amount="1000.00").json()["id"]  # an OPEN session

    def mv(session, kind, amount, handover):
        return (
            "INSERT INTO cash_movements (box_id, session_id, kind, amount, actor_id, notes, reference, session_handover_id, "
            "created_at) VALUES (:b, :s, :k, :a, :u, 'x', 'x', :h, now())"
        ), {"b": x.box, "s": session, "k": kind, "a": amount, "u": x.nxt, "h": handover}

    refused(
        *mv(open_sid, "session_handover_out", -30000, hid)[:1],
        match="only leaves a closing session",
        **mv(open_sid, "session_handover_out", -30000, hid)[1],
    )
    refused(
        *mv(sid, "session_handover_out", -29999, hid)[:1],
        match="pending, undeclined handover",
        **mv(sid, "session_handover_out", -29999, hid)[1],
    )
    refused(
        *mv(sid, "session_handover_out", 30000, hid)[:1],
        match="negative",
        **mv(sid, "session_handover_out", 30000, hid)[1],
    )
    refused(
        *mv(sid, "opening_handover_fund", 30000, hid)[:1],
        match="admits no movement|opening handover fund",
        **mv(sid, "opening_handover_fund", 30000, hid)[1],
    )
    refused(
        *mv(open_sid, "opening_handover_fund", 30000, hid)[:1],
        match="opening handover fund is positive",
        **mv(open_sid, "opening_handover_fund", 30000, hid)[1],
    )
    refused(
        *mv(sid, "session_handover_out", -30000, None)[:1],
        match="pending, undeclined handover",
        **mv(sid, "session_handover_out", -30000, None)[1],
    )
    refused(
        *mv(open_sid, "expense", 100, hid)[:1],
        match="only session handover movements carry",
        **mv(open_sid, "expense", 100, hid)[1],
    )
    # one valid exit (with the balance it backs) commits; a second one for the same handover meets the unique index
    first, first_params = mv(sid, "session_handover_out", -30000, hid)
    tx((first, first_params), ("UPDATE cash_sessions SET balance = balance - 30000 WHERE id = :s", {"s": sid}))
    refused(
        *mv(sid, "session_handover_out", -30000, hid)[:1],
        match="uq_cash_movements_handover_out",
        **mv(sid, "session_handover_out", -30000, hid)[1],
    )
    # a declined handover can never be backed by an exit
    y = hworld(client, sink, tenant_a, "q")
    sid2, hid2, _ = declared(client, y)
    ddecline(client, y.nxt_h, hid2)
    refused(
        "INSERT INTO cash_movements (box_id, session_id, kind, amount, actor_id, notes, reference, session_handover_id, created_at) "
        "VALUES (:b, :s, 'session_handover_out', -30000, :u, 'x', 'x', :h, now())",
        "pending, undeclined handover",
        b=y.box,
        s=sid2,
        u=y.nxt,
        h=hid2,
    )


def test_a_forward_pointer_must_lead_to_the_same_cash_session_and_cash_point(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    sid, h1, _ = declared(client, x)
    cp2 = mk_cp(client, x.adm, x.b, "OTRA", currencies=["DOP"])["id"]
    other_sid = open_(client, x.cas_h, cp2, amount="30000.00").json()["id"]
    other_h = dclose(client, x.cas_h, other_sid, N30, x.alt).json()["session_handover"]["id"]
    cp3 = mk_cp(client, x.adm, x.b, "TERCERA", currencies=["DOP"])["id"]
    third_sid = open_(client, x.cas_h, cp3, amount="30000.00").json()["id"]
    cap_other = dclose(client, x.cas_h, third_sid, N30, x.rec, destination="capital").json()["handover"]["id"]
    cancel = (
        "UPDATE cash_session_handovers SET state = 'cancelled', redirect_idempotency_key = :k, "
        "redirect_request_digest = 'sha256:x', redirected_by = :u, redirected_at = now(), "
        "redirect_reason = 'Reasignacion hacia otro destino' WHERE id = :h",
        {"k": key("t23p"), "u": x.sup, "h": h1},
    )
    to_direct = (
        "UPDATE cash_session_handovers SET redirected_to_session_handover_id = :o WHERE id = :h",
        {"o": other_h, "h": h1},
    )
    to_self = ("UPDATE cash_session_handovers SET redirected_to_session_handover_id = :h WHERE id = :h", {"h": h1})
    to_capital = (
        "UPDATE cash_session_handovers SET redirected_to_capital_handover_id = :o WHERE id = :h",
        {"o": cap_other, "h": h1},
    )
    with pytest.raises(DBAPIError, match="redirected to a direct handover of the same cash, session and cash point"):
        tx(cancel, to_direct)  # a direct row of ANOTHER session / cash point is not a replacement
    with pytest.raises(DBAPIError, match="redirected to a direct handover of the same cash, session and cash point"):
        tx(cancel, to_self)
    with pytest.raises(DBAPIError, match="redirected to a capital handover of the same cash and session"):
        tx(cancel, to_capital)  # a capital row of ANOTHER session is not a replacement either
    assert handover_row(h1)["state"] == "pending" and sid


def test_naming_a_receiver_serialises_with_disabling_that_user(client, sink, tenant_a, monkeypatch):
    """The receiver's user row is locked FOR SHARE by the close, so a concurrent disable waits and then meets the handover."""
    x = hworld(client, sink, tenant_a)
    s = open_(client, x.cas_h, x.cp, amount="30000.00").json()
    inside, release, results = threading.Event(), threading.Event(), {}
    real = cash_core._valid_direct_receiver

    def held(*args, **kwargs):
        real(*args, **kwargs)
        inside.set()
        release.wait(30)

    monkeypatch.setattr(cash_core, "_valid_direct_receiver", held)

    def closing():
        with SessionLocal() as db:
            try:
                results["close"] = cash_core.close_session(
                    db,
                    actor_for(db, x.cas),
                    s["id"],
                    denominations=N30,
                    observation_note=None,
                    receiver_user_id=x.nxt,
                    idempotency_key=key("t23"),
                    destination="next_session",
                )
                db.commit()
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                results["close"] = exc

    def disabling():
        with SessionLocal() as db:
            try:
                identity_admin.disable_user(db, actor_for(db, tenant_a["admin_id"]), x.nxt, None)
                results["disable"] = "disabled"
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                results["disable"] = exc

    t1 = threading.Thread(target=closing)
    t1.start()
    assert inside.wait(30)
    t2 = threading.Thread(target=disabling)
    t2.start()
    time.sleep(1.5)  # the disable is parked on the receiver's user row
    assert "disable" not in results
    release.set()
    t1.join(60)
    t2.join(60)
    assert results["close"]["state"] == "closing"
    assert type(results["disable"]).__name__ == "UserHasCashResponsibility"
    assert session_row(s["id"])["state"] == "closing"


# ================================ 6. the user lifecycle guard (D20) ========================================
def test_a_user_with_cash_responsibility_cannot_be_disabled(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)

    def disable(uid, expect):
        r = client.post(f"{V2}/users/{uid}/disable", headers=x.adm)
        assert r.status_code == expect, r.text
        return r

    s = open_(client, x.cas_h, x.cp, amount="30000.00").json()
    assert code(disable(x.cas, 409)) == "user_has_cash_responsibility"  # owner of an OPEN session
    hid = dclose(client, x.cas_h, s["id"], N30, x.nxt).json()["session_handover"]["id"]
    assert code(disable(x.cas, 409)) == "user_has_cash_responsibility"  # owner of a CLOSING session
    assert code(disable(x.nxt, 409)) == "user_has_cash_responsibility"  # pending, non-declined direct receiver
    ddecline(client, x.nxt_h, hid)
    disable(x.nxt, 200)  # a receiver who declined has discharged the acceptance responsibility
    dredirect(client, x.sup_h, hid, "capital", x.rec)
    assert code(disable(x.rec, 409)) == "user_has_cash_responsibility"  # named receiver of a pending CAPITAL handover
    assert code(disable(x.cas, 409)) == "user_has_cash_responsibility"  # the source is still closing
    new_cap = client.get(f"{CASH}/sessions/{s['id']}", headers=x.cas_h).json()["handover"]["id"]
    accept_(client, x.rec_h, new_cap)
    disable(x.cas, 200)  # everything closed: disables normally, responsibility is never transferred silently
    disable(x.rec, 200)
    assert disable(x.alt, 200)  # a user without any responsibility disables normally
    # a receiver who OWNS the session that a direct accept opened is guarded too
    y = hworld(client, sink, tenant_a, "g")
    _sid, h2, _ = declared(client, y)
    daccept(client, y.nxt_h, h2, N30)
    r = client.post(f"{V2}/users/{y.nxt}/disable", headers=y.adm)
    assert r.status_code == 409 and code(r) == "user_has_cash_responsibility"


# ================================ 7. historical capital digest (D22) =======================================
def legacy_digest(payload):
    """The T-021/T-021H formula, re-implemented here on purpose: a refactor of the app helper cannot make this lie."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def test_the_capital_close_digest_is_byte_identical_and_next_session_adds_one_key(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    s1 = open_(client, x.cas_h, x.cp, amount="1000.00").json()["id"]
    k1 = key("t23")
    plain = client.post(
        f"{CASH}/sessions/{s1}/close",
        headers=x.cas_h,
        json={"idempotency_key": k1, "denominations": {"1000": 1}, "receiver_user_id": x.rec},  # destination omitted
    )
    assert plain.status_code == 200
    expected = legacy_digest(
        {
            "operation": "close_cash_session",
            "session_id": s1,
            "denominations": {"1000": 1},
            "observation_note": "",
            "receiver_user_id": x.rec,
        }
    )
    assert session_row(s1)["close_request_digest"] == expected  # NO "destination" key in the capital / default digest
    # historical retry: the same key + payload replays, with or without the explicit default
    again = client.post(
        f"{CASH}/sessions/{s1}/close",
        headers=x.cas_h,
        json={"idempotency_key": k1, "denominations": {"1000": 1}, "receiver_user_id": x.rec, "destination": "capital"},
    )
    assert again.status_code == 200 and again.json()["replayed"] is True
    # next_session: the same canonical payload PLUS destination, a distinct digest
    accept_(client, x.rec_h, plain.json()["handover"]["id"])
    s2 = open_(client, x.cas_h, x.cp, amount="1000.00").json()["id"]
    k2 = key("t23")
    dclose(client, x.cas_h, s2, {"1000": 1}, x.nxt, k=k2)
    expected2 = legacy_digest(
        {
            "operation": "close_cash_session",
            "session_id": s2,
            "denominations": {"1000": 1},
            "observation_note": "",
            "receiver_user_id": x.nxt,
            "destination": "next_session",
        }
    )
    assert session_row(s2)["close_request_digest"] == expected2
    assert expected2 != legacy_digest(
        {
            "operation": "close_cash_session",
            "session_id": s2,
            "denominations": {"1000": 1},
            "observation_note": "",
            "receiver_user_id": x.nxt,
        }
    )
    assert dclose(client, x.cas_h, s2, {"1000": 1}, x.nxt, k=k2).json()["replayed"] is True
    # the same key with the OTHER destination is a different request
    assert (
        code(dclose(client, x.cas_h, s2, {"1000": 1}, x.rec, k=k2, destination="capital", expect=409))
        == "idempotency_conflict"
    )
    # command digests of the new commands (own formulas, own namespaces)
    h2 = handover_row(q("SELECT id FROM cash_session_handovers WHERE source_session_id = :s", s=s2)[0][0])
    ka, kd = key("t23"), key("t23")
    ddecline(client, x.nxt_h, h2["id"], k=kd)
    assert handover_row(h2["id"])["decline_request_digest"] == legacy_digest(
        {"operation": "decline_session_handover", "handover_id": h2["id"], "reason": REASON}
    )
    dredirect(client, x.sup_h, h2["id"], "next_session", x.alt, k=ka)
    assert handover_row(h2["id"])["redirect_request_digest"] == legacy_digest(
        {
            "operation": "redirect_session_handover",
            "handover_id": h2["id"],
            "destination": "next_session",
            "receiver_user_id": x.alt,
            "reason": SUP_REASON,
        }
    )
    h3 = handover_row(handover_row(h2["id"])["redirected_to_session_handover_id"])
    daccept(client, x.alt_h, h3["id"], {"500": 2}, k=ka)
    assert handover_row(h3["id"])["accept_request_digest"] == legacy_digest(
        {"operation": "accept_session_handover", "handover_id": h3["id"], "denominations": {"500": 2}}
    )


# ================================ 8. reads, privacy, permissions ===========================================
def test_reads_are_scoped_pure_and_never_expose_keys_or_digests(client, sink, tenant_a, tenant_b):
    x = hworld(client, sink, tenant_a)
    sid, hid, _ = declared(client, x)
    before = money()
    one = client.get(f"{SH}/{hid}", headers=x.nxt_h)
    assert one.status_code == 200 and one.json()["session_handover"]["id"] == hid
    for who in (x.cas_h, x.sup_h):  # the maker and a supervisor may read it
        assert client.get(f"{SH}/{hid}", headers=who).status_code == 200
    assert client.get(f"{SH}/{hid}", headers=x.alt_h).status_code == 200  # holds receive for the branch
    nobody_h, _nobody = mkuser(client, sink, x.adm, tenant_a, "nobody@x.com", [ACCEPT], scope="branch", branch_id=x.b)
    assert client.get(f"{SH}/{hid}", headers=nobody_h).status_code == 403  # holds none of receive / redirect / read
    assert client.get(f"{SH}/{hid}", headers=admin_headers(client, tenant_b)).status_code == 404
    pending = client.get(SH, headers=x.nxt_h).json()
    assert [i["id"] for i in pending["items"]] == [hid] and pending["next_before_id"] is None
    assert (
        client.get(SH, headers=nobody_h).status_code == 200 and client.get(SH, headers=nobody_h).json()["items"] == []
    )
    assert client.get(SH, headers=x.nxt_h, params={"state": "bogus"}).status_code == 422
    assert client.get(SH, headers=x.nxt_h, params={"limit": 0}).status_code == 422
    ddecline(client, x.nxt_h, hid)
    done = daccept
    assert code(done(client, x.nxt_h, hid, N30, expect=409)) == "handover_declined"
    dredirect(client, x.sup_h, hid, "next_session", x.alt)
    everything = [
        client.get(f"{SH}/{hid}", headers=x.sup_h).json(),
        client.get(SH, headers=x.sup_h, params={"state": "cancelled"}).json(),
        client.get(f"{CASH}/sessions/{sid}", headers=x.cas_h).json(),
    ]
    for payload in everything:
        names = keys_in(payload)
        assert not [n for n in names if "idempotency" in n or "digest" in n], names
    assert money()["cash_movements"] == before["cash_movements"]  # reads and refusals moved no cash


def test_new_permissions_are_sensitive_catalogued_and_granted_to_the_system_role_only(client, sink, tenant_a):
    sensitive = {p.code: p.sensitive for p in CATALOG}
    assert {RECEIVE, REDIRECT} <= CATALOG_CODES and sensitive[RECEIVE] is True and sensitive[REDIRECT] is True
    assert {p.code for p in CATALOG if p.code.startswith("cash.handovers.")} == {ACCEPT, RECEIVE, REDIRECT}
    rows = q("SELECT code, is_sensitive FROM permissions WHERE code IN (:a, :b)", a=RECEIVE, b=REDIRECT)
    assert dict(rows) == {RECEIVE: True, REDIRECT: True}
    granted = q(
        "SELECT r.system_defined, r.tenant_id IS NOT NULL FROM role_permissions rp JOIN roles r ON r.id = rp.role_id "
        "JOIN permissions p ON p.id = rp.permission_id WHERE p.code IN (:a, :b)",
        a=RECEIVE,
        b=REDIRECT,
    )
    assert granted and all(sd and tenant for sd, tenant in granted)
    mig = (ROOT / "alembic/versions/0022_session_handover_opening.py").read_text(encoding="utf-8")
    assert "WHERE r.system_defined AND r.tenant_id IS NOT NULL" in mig  # grants: the tenant system role only
    # an ordinary cashier role holds neither
    assert not q(
        "SELECT 1 FROM user_role_assignments a JOIN role_permissions rp ON rp.role_id = a.role_id "
        "JOIN permissions p ON p.id = rp.permission_id WHERE a.user_id = :u AND p.code IN (:a, :b)",
        u=x_cashier_id(client, sink, tenant_a),
        a=RECEIVE,
        b=REDIRECT,
    )


def x_cashier_id(client, sink, tenant):
    return hworld(client, sink, tenant, "p").cas


# ================================ 9. concurrency (D23) =====================================================
def two_worlds(client, sink, tenant):
    return hworld(client, sink, tenant, "a"), hworld(client, sink, tenant, "b")


def test_accept_identical_concurrent_retry_is_a_replay_not_handover_not_pending(client, sink, tenant_a, monkeypatch):
    x = hworld(client, sink, tenant_a)
    sid, hid, _ = declared(client, x)
    k, before = key("t23"), money()
    seam(
        monkeypatch, session_handovers, "_cash_point", when=lambda a, kw: not kw.get("lock")
    )  # both pass the first replay
    out = race((x.nxt, j_accept(hid, N30, k)), (x.nxt, j_accept(hid, N30, k)))
    winners, losers = verdict(out)
    assert losers == [] and sorted(o[1] for o in winners) == [False, True]
    assert winners[0][2]["receiving_session"]["id"] == winners[1][2]["receiving_session"]["id"]
    after = money()
    assert (
        after["cash_sessions"] - before["cash_sessions"] == 1
        and after["cash_movements"] - before["cash_movements"] == 2
    )
    assert after["capital_movements"] == before["capital_movements"]
    assert len(events("cash.session_handover.accepted")) == 1


def test_decline_identical_concurrent_retry_is_a_replay_not_handover_declined(client, sink, tenant_a, monkeypatch):
    x = hworld(client, sink, tenant_a)
    sid, hid, _ = declared(client, x)
    k = key("t23")
    seam(monkeypatch, session_handovers, "_cash_point", when=lambda a, kw: not kw.get("lock"))
    out = race((x.nxt, j_decline(hid, k)), (x.nxt, j_decline(hid, k)))
    winners, losers = verdict(out)
    assert losers == [] and sorted(o[1] for o in winners) == [False, True]
    assert len(events("cash.session_handover.declined")) == 1


def test_redirect_identical_concurrent_retry_is_a_replay_not_session_handover_not_pending(
    client, sink, tenant_a, monkeypatch
):
    x = hworld(client, sink, tenant_a)
    sid, hid, _ = declared(client, x)
    k = key("t23")
    seam(monkeypatch, session_handovers, "_cash_point", when=lambda a, kw: not kw.get("lock"))
    out = race((x.cas, j_redirect(hid, k, "next_session", x.alt)), (x.cas, j_redirect(hid, k, "next_session", x.alt)))
    winners, losers = verdict(out)
    assert losers == [] and sorted(o[1] for o in winners) == [False, True]
    assert count("cash_session_handovers", "source_session_id = :s", s=sid) == 2
    assert len(events("cash.session_handover.redirected")) == 1


def test_accept_same_key_from_two_branches_ends_as_idempotency_conflict(client, sink, tenant_a, monkeypatch):
    x1, x2 = two_worlds(client, sink, tenant_a)
    s1, h1, _ = declared(client, x1)
    s2, h2, _ = declared(client, x2)
    shared, before = key("t23"), money()
    seam(monkeypatch, session_handovers, "now_utc")  # after every validation, right before the key is claimed
    out = race((x1.nxt, j_accept(h1, N30, shared)), (x2.nxt, j_accept(h2, N30, shared)))
    winners, losers = verdict(out)
    assert len(winners) == 1 and winners[0][1] is False
    assert [(o[0], o[1]) for o in losers] == [("IdempotencyConflict", 409)]
    assert count("cash_session_handovers", "accept_idempotency_key = :k", k=shared) == 1
    lost = h2 if winners[0][2]["session_handover"]["id"] == h1 else h1
    row = handover_row(lost)
    assert (row["state"], row["accept_idempotency_key"], row["accepted_by"]) == ("pending", None, None)
    after = money()  # exactly the winner's two movements and its new session; nothing of the loser
    assert (after["cash_sessions"] - before["cash_sessions"], after["cash_movements"] - before["cash_movements"]) == (
        1,
        2,
    )
    assert after["capital_movements"] == before["capital_movements"]
    assert len(events("cash.session_handover.accepted")) == 1


def test_decline_same_key_from_two_branches_ends_as_idempotency_conflict(client, sink, tenant_a, monkeypatch):
    x1, x2 = two_worlds(client, sink, tenant_a)
    s1, h1, _ = declared(client, x1)
    s2, h2, _ = declared(client, x2)
    shared = key("t23")
    seam(monkeypatch, session_handovers, "now_utc")
    out = race((x1.nxt, j_decline(h1, shared)), (x2.nxt, j_decline(h2, shared)))
    winners, losers = verdict(out)
    assert len(winners) == 1 and [(o[0], o[1]) for o in losers] == [("IdempotencyConflict", 409)]
    assert count("cash_session_handovers", "decline_idempotency_key = :k", k=shared) == 1
    lost = h2 if winners[0][2]["session_handover"]["id"] == h1 else h1
    assert handover_row(lost)["declined_at"] is None and len(events("cash.session_handover.declined")) == 1


def test_redirect_same_key_from_two_branches_ends_as_idempotency_conflict(client, sink, tenant_a, monkeypatch):
    x1, x2 = two_worlds(client, sink, tenant_a)
    s1, h1, _ = declared(client, x1)
    s2, h2, _ = declared(client, x2)
    shared, before = key("t23"), money()
    seam(monkeypatch, session_handovers, "now_utc")
    out = race(
        (x1.cas, j_redirect(h1, shared, "next_session", x1.alt)),
        (x2.cas, j_redirect(h2, shared, "next_session", x2.alt)),
    )
    winners, losers = verdict(out)
    assert len(winners) == 1 and [(o[0], o[1]) for o in losers] == [("IdempotencyConflict", 409)]
    assert count("cash_session_handovers", "redirect_idempotency_key = :k", k=shared) == 1
    lost = h2 if winners[0][2]["session_handover"]["id"] == h1 else h1
    row = handover_row(lost)
    assert (row["state"], row["redirect_idempotency_key"], row["redirected_to_session_handover_id"]) == (
        "pending",
        None,
        None,
    )
    assert money()["cash_session_handovers"] - before["cash_session_handovers"] == 1  # the winner's replacement only
    assert len(events("cash.session_handover.redirected")) == 1


# ================================ 10. classification: only the named key constraint ========================
ACCEPT_KEY, DECLINE_KEY, REDIRECT_KEY = (
    "uq_cash_session_handovers_accept_key",
    "uq_cash_session_handovers_decline_key",
    "uq_cash_session_handovers_redirect_key",
)


def test_accept_real_key_collision_is_classified_before_any_economic_row(client, sink, tenant_a, monkeypatch):
    x1, x2 = two_worlds(client, sink, tenant_a)
    s1, h1, _ = declared(client, x1)
    s2, h2, _ = declared(client, x2)
    taken = key("t23")
    daccept(client, x1.nxt_h, h1, N30, k=taken)
    before = money()
    blind(
        monkeypatch, session_handovers, "_accept_replay"
    )  # the winner "was not visible": a REAL collision at the claim
    with SessionLocal() as db:
        actor = actor_for(db, x2.nxt)
        with pytest.raises(IdempotencyConflict):
            j_accept(h2, N30, taken)(db, actor)
        h, s = db.get(CashSessionHandover, h2), db.get(CashSession, s2)
        assert (h.state, h.accept_idempotency_key, h.accepted_by) == ("pending", None, None)
        assert (s.state, s.balance) == ("closing", 30000)
        assert not db.new  # no movement and no session ever left the savepoint
        ok = j_accept(h2, N30, key("t23"))(db, actor)  # the same transaction goes on to accept normally
        db.commit()
    assert ok["session_handover"]["state"] == "confirmed" and ok["replayed"] is False
    assert money()["cash_sessions"] == before["cash_sessions"] + 1


def test_decline_real_key_collision_is_classified_and_the_session_stays_usable(client, sink, tenant_a, monkeypatch):
    x1, x2 = two_worlds(client, sink, tenant_a)
    s1, h1, _ = declared(client, x1)
    s2, h2, _ = declared(client, x2)
    taken = key("t23")
    ddecline(client, x1.nxt_h, h1, k=taken)
    blind(monkeypatch, session_handovers, "_decline_replay")
    with SessionLocal() as db:
        actor = actor_for(db, x2.nxt)
        with pytest.raises(IdempotencyConflict):
            j_decline(h2, taken)(db, actor)
        assert db.get(CashSessionHandover, h2).declined_at is None
        ok = j_decline(h2, key("t23"))(db, actor)
        db.commit()
    assert ok["session_handover"]["declined"] is True and ok["replayed"] is False


def test_redirect_real_key_collision_is_classified_and_the_session_stays_usable(client, sink, tenant_a, monkeypatch):
    x1, x2 = two_worlds(client, sink, tenant_a)
    s1, h1, _ = declared(client, x1)
    s2, h2, _ = declared(client, x2)
    taken = key("t23")
    dredirect(client, x1.cas_h, h1, "next_session", x1.alt, k=taken)
    before = money()
    blind(monkeypatch, session_handovers, "_redirect_replay")
    with SessionLocal() as db:
        actor = actor_for(db, x2.cas)
        with pytest.raises(IdempotencyConflict):
            j_redirect(h2, taken, "next_session", x2.alt)(db, actor)
        row = db.get(CashSessionHandover, h2)
        assert (row.state, row.redirect_idempotency_key) == ("pending", None) and not db.new
        ok = j_redirect(h2, key("t23"), "next_session", x2.alt)(db, actor)
        db.commit()
    assert ok["session_handover"]["state"] == "cancelled" and ok["replayed"] is False
    assert money()["cash_session_handovers"] == before["cash_session_handovers"] + 1


@pytest.mark.parametrize(
    "constraint", [None, "uq_cash_session_handovers_live", "ck_cash_session_handovers_state_valid"]
)
def test_unrelated_integrity_errors_are_never_classified_as_idempotency_conflicts(
    client, sink, tenant_a, monkeypatch, constraint
):
    """A winner for the key EXISTS for each command, so a wrongly broad handler would answer IdempotencyConflict."""
    w, x1, x2, x3 = (hworld(client, sink, tenant_a, t) for t in "wijk")
    taken = key("t23")
    _s, hw, _ = declared(client, w)  # `w` owns `taken` as an accept key, a decline key AND a redirect key
    ddecline(client, w.nxt_h, hw, k=taken)
    hw2 = dredirect(client, w.sup_h, hw, "next_session", w.alt, k=taken).json()["replacement"]["handover"]["id"]
    daccept(client, w.alt_h, hw2, N30, k=taken)
    _s, ha, _ = declared(client, x1)
    _s, hd, _ = declared(client, x2)
    _s, hr, _ = declared(client, x3)
    for name in ("_accept_replay", "_decline_replay", "_redirect_replay"):
        blind(monkeypatch, session_handovers, name)
    attempts = (
        (
            x1.nxt,
            lambda s: any(isinstance(o, CashSessionHandover) and o.accept_idempotency_key == taken for o in s.dirty),
            j_accept(ha, N30, taken),
        ),
        (
            x2.nxt,
            lambda s: any(isinstance(o, CashSessionHandover) and o.decline_idempotency_key == taken for o in s.dirty),
            j_decline(hd, taken),
        ),
        (
            x3.cas,
            lambda s: any(isinstance(o, CashSessionHandover) and o.redirect_idempotency_key == taken for o in s.dirty),
            j_redirect(hr, taken, "next_session", x3.alt),
        ),
    )
    for user, trigger, job in attempts:
        with monkeypatch.context() as m, SessionLocal() as db:
            failing_flush(m, constraint, trigger)
            with pytest.raises(IntegrityError):
                job(db, actor_for(db, user))
            db.rollback()


@pytest.mark.parametrize("which", ["accept", "decline", "redirect"])
def test_the_key_constraint_without_a_winning_row_is_not_swallowed(client, sink, tenant_a, monkeypatch, which):
    x = hworld(client, sink, tenant_a)
    _sid, hid, _ = declared(client, x)
    if which == "accept":
        constraint, job, user, replay = ACCEPT_KEY, j_accept(hid, N30, key("t23")), x.nxt, "_accept_replay"
        trigger = lambda s: any(isinstance(o, CashSessionHandover) and o.accept_idempotency_key for o in s.dirty)  # noqa: E731
    elif which == "decline":
        constraint, job, user, replay = DECLINE_KEY, j_decline(hid, key("t23")), x.nxt, "_decline_replay"
        trigger = lambda s: any(isinstance(o, CashSessionHandover) and o.decline_idempotency_key for o in s.dirty)  # noqa: E731
    else:
        constraint, job, user, replay = (
            REDIRECT_KEY,
            j_redirect(hid, key("t23"), "next_session", x.alt),
            x.cas,
            "_redirect_replay",
        )
        trigger = lambda s: any(isinstance(o, CashSessionHandover) and o.redirect_idempotency_key for o in s.dirty)  # noqa: E731
    blind(monkeypatch, session_handovers, replay, calls=99)  # nothing is ever found: no winner to classify against
    failing_flush(monkeypatch, constraint, trigger)
    with SessionLocal() as db:
        with pytest.raises(IntegrityError):
            job(db, actor_for(db, user))
        db.rollback()


def test_replay_semantics_actor_and_digest_still_matter(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    sid, hid, _ = declared(client, x)
    ka = key("t23")
    ddecline(client, x.nxt_h, hid, k=ka)
    assert code(ddecline(client, x.alt_h, hid, k=ka, expect=409)) == "idempotency_conflict"  # another actor, same key
    kr = key("t23")
    out = dredirect(client, x.sup_h, hid, "next_session", x.alt, k=kr).json()
    assert dredirect(client, x.sup_h, hid, "next_session", x.alt, k=kr).json()["replayed"] is True
    assert code(dredirect(client, x.sup_h, hid, "next_session", x.nxt, k=kr, expect=409)) == "idempotency_conflict"
    assert (
        code(dredirect(client, x.cas_h, hid, "next_session", x.alt, k=kr, expect=409)) == "idempotency_conflict"
    )  # other actor
    h2 = out["replacement"]["handover"]["id"]
    kc = key("t23")
    daccept(client, x.alt_h, h2, {"500": 60}, k=kc)
    assert daccept(client, x.alt_h, h2, {"500": 60}, k=kc).json()["replayed"] is True
    assert (
        code(daccept(client, x.alt_h, h2, N30, k=kc, expect=409)) == "idempotency_conflict"
    )  # another count, same key
    assert code(daccept(client, x.sup_h, h2, {"500": 60}, k=kc, expect=409)) == "idempotency_conflict"  # another actor


# ================================ 11. migration 0022 =======================================================
def _load_migration():
    spec = importlib.util.spec_from_file_location("m0022", ROOT / "alembic/versions/0022_session_handover_opening.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_sql_is_a_verbatim_copy_of_the_model_ddl():
    mod = _load_migration()
    assert mod.revision == "0022" and mod.down_revision == "0021"
    assert mod.HANDOVER_FUNCTIONS == list(handover_ddl.FUNCTIONS)
    assert mod.HANDOVER_TRIGGERS == list(handover_ddl.TRIGGERS)
    assert mod.HANDOVER_NEW_FUNCTION_NAMES == list(handover_ddl.NEW_FUNCTION_NAMES)
    # the downgrade restores the exact 0020 definitions of the four functions 0022 replaces
    assert mod.PREVIOUS_FUNCTIONS == [
        ddl.SESSION_INSERT_FN,
        ddl.SESSION_UPDATE_FN,
        ddl.SESSION_CONSISTENCY_FN,
        ddl.MOVEMENT_INSERT_FN,
    ]
    assert [f.split("(")[0] for f in handover_ddl.REPLACED_FUNCTIONS] == [
        f.split("(")[0] for f in mod.PREVIOUS_FUNCTIONS
    ]
    assert [p[0] for p in mod.NEW_PERMISSIONS] == [RECEIVE, REDIRECT] and all(p[2] for p in mod.NEW_PERMISSIONS)


def _schema(eng):
    with eng.connect() as c:
        return {
            "functions": sorted(
                tuple(r)
                for r in c.execute(
                    text("SELECT proname, md5(prosrc) FROM pg_proc WHERE proname LIKE 'cash\\_%' ESCAPE '\\'")
                )
            ),
            "triggers": sorted(
                tuple(r)
                for r in c.execute(
                    text(
                        "SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                        "WHERE NOT t.tgisinternal AND c.relname LIKE 'cash\\_%' ESCAPE '\\'"
                    )
                )
            ),
            "constraints": sorted(
                tuple(r)
                for r in c.execute(
                    text(
                        "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conrelid::regclass::text LIKE 'cash\\_%' ESCAPE '\\'"
                    )
                )
            ),
            "indexes": sorted(
                tuple(r)
                for r in c.execute(
                    text(
                        "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE tablename LIKE 'cash\\_%' ESCAPE '\\'"
                    )
                )
            ),
            "columns": sorted(
                tuple(r)
                for r in c.execute(
                    text(
                        "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns WHERE table_name LIKE 'cash\\_%' ESCAPE '\\'"
                    )
                )
            ),
            "permissions": sorted(
                r[0] for r in c.execute(text("SELECT code FROM permissions WHERE code LIKE 'cash.handovers.%'"))
            ),
            "grants": c.execute(
                text(
                    "SELECT count(*) FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id WHERE p.code LIKE 'cash.handovers.%'"
                )
            ).scalar(),
            "version": c.execute(text("SELECT version_num FROM alembic_version")).scalar(),
        }


def test_migration_0022_upgrade_check_clean_downgrade_restores_0021_exactly_and_reupgrades(scratch_db):
    assert _alembic(scratch_db, "upgrade", "0021").returncode == 0
    eng = create_engine(scratch_db)
    at_0021 = _schema(eng)
    assert at_0021["version"] == "0021" and "cash_session_handovers" not in {r[0] for r in at_0021["columns"]}
    up = _alembic(scratch_db, "upgrade", "head")
    assert up.returncode == 0, up.stderr[-800:]
    assert _alembic(scratch_db, "check").returncode == 0
    at_0022 = _schema(eng)
    assert at_0022["version"] == "0022" and at_0022["permissions"] == [
        "cash.handovers.accept",
        "cash.handovers.receive",
        "cash.handovers.redirect",
    ]
    assert {t[1] for t in at_0022["triggers"]} >= {
        "trg_cash_session_handovers_insert_check",
        "trg_cash_session_handovers_update_check",
        "trg_cash_session_handovers_guard",
        "trg_cash_session_handovers_truncate",
        "trg_cash_session_handovers_consistency",
        "trg_cash_session_handovers_session_consistency",
        "trg_cash_custody_transfers_session_consistency",
    }
    assert at_0022 != at_0021
    down = _alembic(scratch_db, "downgrade", "0021")
    assert down.returncode == 0, down.stderr[-800:]
    assert (
        _schema(eng) == at_0021
    )  # EXACTLY 0021: functions, triggers, constraints, indexes, columns, permissions, grants
    assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    assert _schema(eng) == at_0022 and _alembic(scratch_db, "check").returncode == 0
    eng.dispose()


def _plant_history(c, kind):
    """Plant ONE kind of T-023 history (history creation: FKs and triggers off) on a database at 0022."""
    c.execute(text("SET LOCAL session_replication_role = replica"))
    session = dict(
        box_id=1,
        tenant_id=1,
        cash_point_id=1,
        currency_code="DOP",
        business_date=__import__("datetime").date(2026, 10, 1),
        state="open",
        opening_source="zero",
        opening_contract="v2",
        opening_expected=0,
        opening_counted=0,
        balance=0,
        balance_base=0,
        snapshot={},
        denominations={},
        notes="",
        opened_by=1,
        cashier_id=1,
        version=1,
    )
    if kind == "handover_row":
        _plant(
            c,
            "cash_session_handovers",
            state="pending",
            source_session_id=1,
            amount=1,
            version=1,
            currency_code="DOP",
            from_user_id=1,
            to_user_id=2,
        )
    elif kind == "handover_opening":
        _plant(c, "cash_sessions", **session | {"opening_source": "handover", "opening_handover_id": 1})
    else:
        sid = _plant(c, "cash_sessions", **session)
        if kind == "out_movement":
            _plant(
                c,
                "cash_movements",
                session_id=sid,
                kind="session_handover_out",
                amount=-1,
                session_handover_id=1,
                tenant_id=1,
                cash_point_id=1,
                currency_code="DOP",
                box_id=1,
            )
        elif kind == "fund_movement":
            _plant(
                c,
                "cash_movements",
                session_id=sid,
                kind="opening_handover_fund",
                amount=1,
                session_handover_id=1,
                tenant_id=1,
                cash_point_id=1,
                currency_code="DOP",
                box_id=1,
            )
        elif kind in ("event_declared", "event_accepted", "event_declined", "event_redirected"):
            c.execute(
                text(
                    "INSERT INTO security_events (event_type, outcome, tenant_id, details, occurred_at) VALUES (:e, 'success', 1, '{}'::jsonb, now())"
                ),
                {"e": f"cash.session_handover.{kind.split('_')[1]}"},
            )
        elif kind == "event_opened_from_handover":
            c.execute(
                text(
                    "INSERT INTO security_events (event_type, outcome, tenant_id, details, occurred_at) VALUES ('cash.session.opened', 'success', 1, '{\"opening_source\": \"handover\"}'::jsonb, now())"
                )
            )


@pytest.mark.parametrize(
    "kind",
    [
        "handover_row",
        "handover_opening",
        "out_movement",
        "fund_movement",
        "event_declared",
        "event_accepted",
        "event_declined",
        "event_redirected",
        "event_opened_from_handover",
    ],
)
def test_migration_0022_downgrade_refuses_every_class_of_handover_history(scratch_db, kind):
    assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    eng = create_engine(scratch_db)
    with eng.begin() as c:
        _plant_history(c, kind)
    before = _schema(eng)
    out = _alembic(scratch_db, "downgrade", "0021")
    assert out.returncode != 0 and "cannot downgrade 0022: session handover history exists" in out.stderr, out.stderr[
        -600:
    ]
    assert _schema(eng) == before and before["version"] == "0022"  # nothing changed: no deletion, rewrite or repair
    assert _alembic(scratch_db, "check").returncode == 0
    eng.dispose()


def test_migration_0022_downgrade_locks_the_history_tables_before_it_looks():
    src = (ROOT / "alembic/versions/0022_session_handover_opening.py").read_text(encoding="utf-8")
    lock = src.index("LOCK TABLE cash_session_handovers, cash_sessions, cash_movements IN ACCESS EXCLUSIVE MODE")
    assert lock < src.index("for problem, sql in DOWNGRADE_REFUSALS") < src.index("DROP TRIGGER IF EXISTS")


# ================================ 12. the earlier flows are untouched ======================================
def test_the_capital_flow_endpoints_and_defaults_stay_capital_only(client, sink, tenant_a):
    x = hworld(client, sink, tenant_a)
    s = open_(client, x.cas_h, x.cp, amount="1000.00").json()
    closed = client.post(
        f"{CASH}/sessions/{s['id']}/close",
        headers=x.cas_h,
        json={"idempotency_key": key("t23"), "denominations": {"1000": 1}, "receiver_user_id": x.rec},
    ).json()
    assert closed["handover"]["id"] and closed["session_handover"] is None
    assert closed["next_action"]["action"] == "accept_closing_handover"
    assert count("cash_session_handovers") == 0
    done = accept_(client, x.rec_h, closed["handover"]["id"]).json()
    assert done["state"] == "closed" and movements(s["id"], "closing_capital_transfer")[0].amount == Decimal("-1000.00")
    assert (
        q("SELECT count(*) FROM cash_movements WHERE kind IN ('session_handover_out', 'opening_handover_fund')")[0][0]
        == 0
    )
    assert (
        client.get(f"{CASH}/handovers", headers=x.rec_h, params={"state": "confirmed"}).json()["items"][0]["id"]
        == closed["handover"]["id"]
    )
