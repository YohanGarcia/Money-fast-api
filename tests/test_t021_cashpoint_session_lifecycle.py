"""T-021 CashPoint-anchored cash core session lifecycle (T021-*). PostgreSQL only.

CashPoint is the operational cash position: at most ONE ``open|closing`` session per CashPoint (D1); ``open -> closing ->
closed``, closed terminal; openings are ``zero`` or ``capital`` (one fund movement + one capital ``to_cash``), never
anonymous (D3); counts are exact by denomination and a difference is a separate record with an observation (D4, DR-007),
never a balancing movement; a non-zero close hands the counted cash to capital through ONE handover accepted by its named,
authorised, different receiver (D2/D5); suspension blocks only NEW openings (D6); migration 0020 maps legacy history
without rewriting it (D7-D12). Every invariant has a database backstop.
"""

import json
import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.db import SessionLocal, engine
from app.models.capital import CapitalMovement
from app.models.cash import CashBox, CashConfig, CashMovement, CashSession
from app.modules.cash import ddl
from app.modules.cash import port as cash_port
from app.modules.cash import sessions as cash_core
from app.modules.identity.authorization import Grant, Principal
from app.modules.identity.catalog import CATALOG_CODES
from tests import pg_env  # noqa: F401  (must precede app imports)
from tests.cash_fixtures import close_session_now, open_v2_session
from tests.test_t001_foundation import _alembic, scratch_db  # noqa: F401
from tests.test_t002_identity import (  # noqa: F401  (fixtures + helpers shared with the earlier suites)
    admin_headers,
    client,
    fresh_db,
    sink,
    tenant_a,
    tenant_b,
)
from tests.test_t003_organization import mk_branch, mk_cp
from tests.test_t006_origination import (  # noqa: F401
    _release_tracked_connections,
    count,
)
from tests.test_t012_collection_assignment import _fresh_pool, mkuser  # noqa: F401
from tests.test_t019_field_cash_custody import decide, declare, fpay
from tests.test_t020_field_cash_refund import refund, reverse, rworld

ROOT = Path(__file__).resolve().parent.parent
V2 = "/api/v2"
CASH = f"{V2}/cash"
OPEN, CLOSE, READ = "cash.sessions.open", "cash.sessions.close", "cash.sessions.read"
ACCEPT, DIFF_READ = "cash.handovers.accept", "cash.differences.read"
_keys = iter(range(1, 10**9))


def key(prefix="t21"):
    return f"{prefix}-key-{next(_keys):08d}"


def code(r):
    return r.json()["error"]["code"]


def sql(statement, **params):
    with SessionLocal() as db:
        out = db.execute(text(statement), params)
        db.commit()
        return out


def refused(statement, match=None, **params):
    with pytest.raises(DBAPIError, match=match):
        sql(statement, **params)


def capital_balance(tenant_id):
    with SessionLocal() as db:
        from app.services import capital_service

        return capital_service.balance(db, tenant_id)


def cworld(client, sink, tenant, tag="a"):
    """A branch with cash enabled, its box (and base CashPoint), 100000 of capital, a cashier and a receiver."""
    adm = admin_headers(client, tenant)
    b = mk_branch(client, adm, f"T21{tag.upper()}")["id"]
    with SessionLocal() as db:
        if db.get(CashConfig, tenant["tenant_id"]) is None:
            db.add(CashConfig(company_id=tenant["tenant_id"], activated_by=tenant["admin_id"]))
        box = CashBox(company_id=tenant["tenant_id"], branch_id=b, initial_balance=Decimal(0))
        db.add(box)
        db.flush()
        cp = cash_core.base_cash_point_id(db, box.id)
        db.add(
            CapitalMovement(
                company_id=tenant["tenant_id"],
                kind="injection",
                amount=Decimal("100000.00"),
                actor_id=tenant["admin_id"],
                notes="Capital QA",
            )
        )
        db.commit()
        box_id = box.id
    cas_h, cas = mkuser(client, sink, adm, tenant, f"cas-{tag}@x.com", [OPEN, CLOSE, READ], scope="branch", branch_id=b)
    rec_h, rec = mkuser(
        client, sink, adm, tenant, f"rec-{tag}@x.com", [ACCEPT, READ, DIFF_READ], scope="branch", branch_id=b
    )
    return SimpleNamespace(
        adm=adm, b=b, box=box_id, cp=cp, cas_h=cas_h, cas=cas, rec_h=rec_h, rec=rec, t=tenant["tenant_id"]
    )


def open_(client, hdr, cp, source="capital", amount="1000.00", denominations=None, note=None, expect=200, k=None):
    if denominations is None:
        denominations = {"1000": int(Decimal(amount) // 1000)} if source == "capital" else {}
    body = {
        "idempotency_key": k or key(),
        "cash_point_id": cp,
        "source": source,
        "amount": amount,
        "denominations": denominations,
    }
    if note is not None:
        body["observation_note"] = note
    r = client.post(f"{CASH}/sessions/open", headers=hdr, json=body)
    assert r.status_code == expect, f"open: {r.status_code} {r.text}"
    return r


def close_(client, hdr, sid, denominations, receiver=None, note=None, expect=200, k=None):
    body = {"idempotency_key": k or key(), "denominations": denominations}
    if receiver is not None:
        body["receiver_user_id"] = receiver
    if note is not None:
        body["observation_note"] = note
    r = client.post(f"{CASH}/sessions/{sid}/close", headers=hdr, json=body)
    assert r.status_code == expect, f"close: {r.status_code} {r.text}"
    return r


def accept_(client, hdr, hid, expect=200, k=None):
    r = client.post(f"{CASH}/handovers/{hid}/accept", headers=hdr, json={"idempotency_key": k or key()})
    assert r.status_code == expect, f"accept: {r.status_code} {r.text}"
    return r


def movements(session_id, kind=None):
    with SessionLocal() as db:
        q = "SELECT id, kind, amount FROM cash_movements WHERE session_id = :s" + (" AND kind = :k" if kind else "")
        return db.execute(text(q + " ORDER BY id"), {"s": session_id, "k": kind}).all()


def capital_rows(kind):
    with SessionLocal() as db:
        return db.execute(
            text("SELECT id, amount, cash_movement_id FROM capital_movements WHERE kind = :k ORDER BY id"), {"k": kind}
        ).all()


def suspend(client, adm, cp, reason="Investigacion de seguridad"):
    r = client.post(f"{V2}/cash-points/{cp}/suspend", headers=adm, json={"reason": reason})
    assert r.status_code == 200, r.text


# ================================ opening (D3) ==========================================================
def test_zero_and_capital_openings_trace_their_origin_and_replay_safely(client, sink, tenant_a):
    x = cworld(client, sink, tenant_a)
    cp2 = mk_cp(client, x.adm, x.b, "T21-Z")["id"]
    assert (
        code(open_(client, x.rec_h, cp2, source="zero", amount="0", expect=403)) == "permission_denied"
    )  # no open grant
    zero = open_(client, x.cas_h, cp2, source="zero", amount="0").json()
    assert (zero["state"], zero["opening_source"], zero["opening_contract"], zero["balance"]) == (
        "open",
        "zero",
        "v2",
        "0.00",
    )
    assert movements(zero["id"]) == [] and capital_rows("to_cash") == []
    assert code(open_(client, x.cas_h, x.cp, source="zero", amount="5.00", expect=422)) == "invalid_cash_opening"
    assert (
        code(open_(client, x.cas_h, x.cp, source="zero", amount="0", denominations={"100": 1}, expect=422))
        == "invalid_cash_opening"
    )
    assert open_(client, x.cas_h, x.cp, source="declared", expect=422).status_code == 422  # no anonymous source exists
    assert (
        code(open_(client, x.cas_h, x.cp, source="capital", amount="1500.00", denominations={}, expect=422))
        == "invalid_cash_opening"
    )
    before = capital_balance(x.t)
    k = key()
    s = open_(client, x.cas_h, x.cp, amount="1500.00", denominations={"1000": 1, "500": 1}, k=k).json()
    assert (s["state"], s["opening_source"], s["balance"], s["opening_counted"], s["cashier_user_id"]) == (
        "open",
        "capital",
        "1500.00",
        "1500.00",
        x.cas,
    )
    ((mid, kind, amount),) = movements(s["id"])
    assert (kind, amount) == ("opening_capital_fund", Decimal("1500.00"))
    ((_, cap_amount, cap_link),) = capital_rows("to_cash")
    assert (cap_amount, cap_link) == (Decimal("1500.00"), mid)
    assert capital_balance(x.t) == before - Decimal("1500.00")  # no double funding, no inflation
    replay = open_(client, x.cas_h, x.cp, amount="1500.00", denominations={"500": 1, "1000": 1}, k=k).json()
    assert (
        replay["replayed"] is True
        and replay["id"] == s["id"]
        and len(movements(s["id"])) == 1
        and len(capital_rows("to_cash")) == 1
    )
    assert (
        code(open_(client, x.cas_h, x.cp, amount="2000.00", denominations={"1000": 2}, k=k, expect=409))
        == "idempotency_conflict"
    )
    # insufficient capital: nothing persists
    cp3 = mk_cp(client, x.adm, x.b, "T21-POOR")["id"]
    sessions = count("cash_sessions")
    assert (
        code(open_(client, x.cas_h, cp3, amount="200000.00", denominations={"2000": 100}, expect=409))
        == "insufficient_capital"
    )
    assert count("cash_sessions") == sessions and len(capital_rows("to_cash")) == 1


def test_anonymous_opening_cash_is_impossible_even_in_the_database(client, sink, tenant_a):
    x = cworld(client, sink, tenant_a)
    base = dict(b=x.box, cp=x.cp, u=x.cas, t=x.t)
    insert = (
        "INSERT INTO cash_sessions (box_id, tenant_id, cash_point_id, currency_code, business_date, state, opening_source, "
        "opening_contract, opening_expected, opening_counted, opening_denominations, balance, balance_base, snapshot, denominations, "
        "notes, opened_by, cashier_id, opened_at, version) VALUES (:b, :t, :cp, 'DOP', current_date, 'open', :src, :contract, "
        ":exp, :cnt, CAST(:den AS jsonb), :bal, 0, '{}', '{}', '', :u, :u, now(), 1)"
    )
    legacy = dict(src="legacy", contract="legacy", exp=500, cnt=500, den="{}", bal=500)
    refused(insert, match="only migration 0020", **(base | legacy))  # the legacy contract is history only
    refused(
        insert,
        match="zero opening",
        **(base | dict(src="zero", contract="v2", exp=0, cnt=500, den='{"500": 1}', bal=0)),
    )
    refused(insert, match="born empty", **(base | dict(src="zero", contract="v2", exp=0, cnt=0, den="{}", bal=500)))
    refused(
        insert,
        match="anonymous",
        **(base | dict(src="declared", contract="v2", exp=500, cnt=500, den='{"500": 1}', bal=0)),
    )
    # a capital opening without its fund + capital leg cannot commit
    refused(
        insert,
        match="one fund movement",
        **(base | dict(src="capital", contract="v2", exp=500, cnt=500, den='{"500": 1}', bal=0)),
    )
    assert count("cash_sessions") == 0


# ================================ D1 one active session per CashPoint ====================================
def test_one_active_session_per_cash_point_and_parallel_points_in_a_branch(client, sink, tenant_a):
    x = cworld(client, sink, tenant_a)
    cas2_h, cas2 = mkuser(
        client, sink, x.adm, tenant_a, "cas2@x.com", [OPEN, CLOSE, READ], scope="branch", branch_id=x.b
    )
    first = open_(client, x.cas_h, x.cp).json()
    assert code(open_(client, cas2_h, x.cp, expect=409)) == "cash_point_busy"
    with SessionLocal() as db, pytest.raises(DBAPIError, match="uq_cash_sessions_active_cash_point"):
        open_v2_session(db, box_id=x.box, cashier_id=cas2, cash_point_id=x.cp)
        db.commit()
    cp2 = mk_cp(client, x.adm, x.b, "T21-2")["id"]
    second = open_(client, cas2_h, cp2).json()  # several cashiers of one branch = several CashPoints
    assert second["cash_point_id"] == cp2 and first["cash_point_id"] == x.cp and second["box_id"] == first["box_id"]
    # a race on one free CashPoint: exactly one session
    cp3 = mk_cp(client, x.adm, x.b, "T21-3")["id"]
    cas3_h, _ = mkuser(client, sink, x.adm, tenant_a, "cas3@x.com", [OPEN, READ], scope="branch", branch_id=x.b)
    barrier, out = threading.Barrier(2), []

    def go(hdr):
        barrier.wait()
        out.append(
            client.post(
                f"{CASH}/sessions/open",
                headers=hdr,
                json={
                    "idempotency_key": key(),
                    "cash_point_id": cp3,
                    "source": "zero",
                    "amount": "0",
                    "denominations": {},
                },
            ).status_code
        )

    threads = [threading.Thread(target=go, args=(h,)) for h in (x.cas_h, cas3_h)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert sorted(out) == [200, 409] and count("cash_sessions", "cash_point_id = :c", c=cp3) == 1


# ================================ close, handover, closed terminal (D2/D5) ===============================
def test_close_zero_is_direct_close_with_cash_hands_over_and_closed_is_terminal(client, sink, tenant_a):
    x = cworld(client, sink, tenant_a)
    cp2 = mk_cp(client, x.adm, x.b, "T21-Z")["id"]
    zero = open_(client, x.cas_h, cp2, source="zero", amount="0").json()
    closed = close_(client, x.cas_h, zero["id"], {"1000": 0}).json()
    assert (closed["state"], closed["handover"], closed["counted"]) == ("closed", None, "0.00")
    assert count("cash_custody_transfers") == 0 and movements(zero["id"]) == []  # no zero-value handover or movement
    s = open_(client, x.cas_h, x.cp, amount="1000.00").json()
    capital = capital_balance(x.t)
    k = key()
    c = close_(client, x.cas_h, s["id"], {"500": 2}, receiver=x.rec, k=k).json()
    assert (
        c["state"],
        c["difference"],
        c["handover"]["state"],
        c["handover"]["amount"],
        c["handover"]["to_user_id"],
    ) == ("closing", "0.00", "pending", "1000.00", x.rec)
    assert c["next_action"] == {
        "action": "accept_closing_handover",
        "handover_id": c["handover"]["id"],
        "receiver_user_id": x.rec,
        "endpoint": f"/api/v2/cash/handovers/{c['handover']['id']}/accept",
        "legacy_command": "confirm_closing_transfer",
    }
    assert close_(client, x.cas_h, s["id"], {"500": 2}, receiver=x.rec, k=k).json()["replayed"] is True
    assert code(close_(client, x.cas_h, s["id"], {"1000": 1}, receiver=x.rec, expect=409)) == "cash_session_not_open"
    # closing still holds the CashPoint slot (the cash is not in capital yet)
    assert code(open_(client, x.cas_h, x.cp, expect=409)) == "cash_point_busy"
    assert (
        client.get(f"{CASH}/sessions/current", headers=x.cas_h, params={"cash_point_id": x.cp}).json()["session"][
            "state"
        ]
        == "closing"
    )
    a = accept_(client, x.rec_h, c["handover"]["id"]).json()
    assert (a["state"], a["balance"], a["handover"]["state"], a["handover"]["accepted_by"]) == (
        "closed",
        "0.00",
        "confirmed",
        x.rec,
    )
    assert [(m.kind, m.amount) for m in movements(s["id"])] == [
        ("opening_capital_fund", Decimal("1000.00")),
        ("closing_capital_transfer", Decimal("-1000.00")),
    ]
    (fc,) = capital_rows("from_cash")
    assert fc.amount == Decimal("1000.00") and fc.cash_movement_id == a["handover"]["cash_movement_id"]
    assert capital_balance(x.t) == capital + Decimal("1000.00")  # the cash returned to capital exactly once
    # closed is terminal: no reopen, no edit, no delete, no movement
    refused("UPDATE cash_sessions SET state = 'open' WHERE id = :s", match="terminal", s=s["id"])
    refused("UPDATE cash_sessions SET notes = 'x' WHERE id = :s", match="terminal", s=s["id"])
    refused("DELETE FROM cash_sessions WHERE id = :s", match="history", s=s["id"])
    refused(
        "INSERT INTO cash_movements (box_id, session_id, kind, amount, actor_id, notes, reference, created_at) "
        "VALUES (:b, :s, 'expense', -1, :u, 'x', 'x', now())",
        match="admits no movement",
        b=x.box,
        s=s["id"],
        u=x.cas,
    )
    nxt = open_(client, x.cas_h, x.cp, source="zero", amount="0").json()  # the slot is free again
    assert nxt["state"] == "open"


def test_closing_never_returns_to_open_and_its_record_is_immutable(client, sink, tenant_a):
    x = cworld(client, sink, tenant_a)
    s = open_(client, x.cas_h, x.cp).json()
    close_(client, x.cas_h, s["id"], {"1000": 1}, receiver=x.rec)
    refused(
        "UPDATE cash_sessions SET state = 'open' WHERE id = :s", match="cannot move from closing to open", s=s["id"]
    )
    refused("UPDATE cash_sessions SET counted = 999 WHERE id = :s", match="immutable", s=s["id"])
    refused("UPDATE cash_sessions SET state = 'closed' WHERE id = :s", match="handover is confirmed", s=s["id"])
    refused("UPDATE cash_sessions SET cashier_id = :u WHERE id = :s", match="immutable", u=x.rec, s=s["id"])


# ================================ physical count and differences (D4, DR-007) ============================
def test_counts_are_exact_differences_are_records_and_never_block_the_next_session(client, sink, tenant_a):
    x = cworld(client, sink, tenant_a)
    s = open_(client, x.cas_h, x.cp, amount="1000.00").json()
    for bad in ({"3": 1}, {"100": -1}, {"100": True}):
        assert close_(client, x.cas_h, s["id"], bad, receiver=x.rec, expect=422).status_code == 422
    assert (
        code(close_(client, x.cas_h, s["id"], {"500": 1, "200": 2}, receiver=x.rec, expect=422))
        == "observation_required"
    )
    c = close_(
        client, x.cas_h, s["id"], {"500": 1, "200": 2}, receiver=x.rec, note="Faltan 100 sin explicacion aun"
    ).json()
    assert (c["state"], c["counted"], c["closing_expected"], c["difference"]) == (
        "closing",
        "900.00",
        "1000.00",
        "-100.00",
    )
    assert c["handover"]["amount"] == "900.00"  # the physical count leaves, never the expected amount
    (d,) = c["differences"]
    assert (d["phase"], d["status"], d["expected"], d["counted"], d["difference"], d["provenance"]) == (
        "closing",
        "pending_review",
        "1000.00",
        "900.00",
        "-100.00",
        "v2",
    )
    assert d["observation_note"] == "Faltan 100 sin explicacion aun" and "resolution_reason" not in d
    listed = client.get(f"{CASH}/differences", headers=x.rec_h, params={"branch_id": x.b}).json()["items"]
    assert [i["id"] for i in listed] == [d["id"]]
    accept_(client, x.rec_h, c["handover"]["id"])
    with SessionLocal() as db:
        closed = db.get(CashSession, s["id"])
        assert (closed.state, closed.balance) == ("closed", Decimal("100.00"))  # the ledger keeps the shortage visible
    assert count("cash_movements", "kind = 'closing_adjustment'") == 0
    refused("UPDATE cash_session_differences SET status = 'resolved' WHERE id = :d", match="history", d=d["id"])
    # the pending difference does not block the next session (closed + pending_review frees the slot)
    nxt = open_(client, x.cas_h, x.cp, amount="1000.00", denominations={"500": 1, "200": 2}, expect=422)
    assert code(nxt) == "observation_required"  # an opening count different from the fund is also a difference
    o = open_(
        client, x.cas_h, x.cp, amount="1000.00", denominations={"500": 1, "200": 2}, note="Fondo incompleto"
    ).json()
    assert o["state"] == "open" and o["balance"] == "1000.00" and o["differences"][0]["phase"] == "opening"
    assert o["differences"][0]["difference"] == "-100.00"
    # DB: a close whose denominations do not add up is refused; a retired adjustment kind is refused
    refused(
        "UPDATE cash_sessions SET state = 'closing', counted = 1000, closing_expected = balance, difference = 1000 - balance, "
        "denominations = '{\"500\": 1}', close_contract = 'v2', closed_by = cashier_id, closed_at = now(), "
        "close_idempotency_key = 'k-123456789012' WHERE id = :s",
        match="sum of its denominations",
        s=o["id"],
    )
    refused(
        "INSERT INTO cash_movements (box_id, session_id, kind, amount, actor_id, notes, reference, created_at) "
        "VALUES (:b, :s, 'closing_adjustment', -100, :u, 'x', 'x', now())",
        match="retired",
        b=x.box,
        s=o["id"],
        u=x.cas,
    )


# ================================ maker-checker (D5) =====================================================
def test_the_closing_handover_needs_a_named_authorised_different_receiver(client, sink, tenant_a):
    x = cworld(client, sink, tenant_a)
    reader_h, reader = mkuser(client, sink, x.adm, tenant_a, "reader@x.com", [READ], scope="branch", branch_id=x.b)
    b2 = mk_branch(client, x.adm, "T21OTHER")["id"]
    far_h, far = mkuser(client, sink, x.adm, tenant_a, "far@x.com", [ACCEPT, READ], scope="branch", branch_id=b2)
    rec2_h, rec2 = mkuser(client, sink, x.adm, tenant_a, "rec2@x.com", [ACCEPT, READ], scope="branch", branch_id=x.b)
    both_h, both = mkuser(
        client, sink, x.adm, tenant_a, "both@x.com", [OPEN, CLOSE, ACCEPT, READ], scope="branch", branch_id=x.b
    )
    s = open_(client, x.cas_h, x.cp).json()
    assert code(close_(client, x.cas_h, s["id"], {"1000": 1}, expect=422)) == "receiver_required"
    for bad in (x.cas, reader, far, 999999):  # self, no permission, other branch scope, unknown
        assert code(close_(client, x.cas_h, s["id"], {"1000": 1}, receiver=bad, expect=422)) == "invalid_receiver"
    assert code(close_(client, x.rec_h, s["id"], {"1000": 1}, receiver=x.rec, expect=403)) == "permission_denied"
    assert code(close_(client, both_h, s["id"], {"1000": 1}, receiver=x.rec, expect=403)) == "cash_session_not_owned"
    h = close_(client, x.cas_h, s["id"], {"1000": 1}, receiver=x.rec).json()["handover"]["id"]
    assert code(accept_(client, x.cas_h, h, expect=403)) == "permission_denied"  # the cashier has no accept permission
    assert code(accept_(client, far_h, h, expect=403)) == "permission_denied"  # wrong branch scope
    assert code(accept_(client, rec2_h, h, expect=403)) == "not_named_receiver"
    assert code(accept_(client, x.adm, h, expect=403)) == "not_named_receiver"  # no admin bypass
    k = key()
    ok = accept_(client, x.rec_h, h, k=k).json()
    assert ok["state"] == "closed" and accept_(client, x.rec_h, h, k=k).json()["replayed"] is True
    assert code(accept_(client, x.rec_h, h, expect=409)) == "cash_handover_not_pending"
    assert len(movements(s["id"], "closing_capital_transfer")) == 1 and len(capital_rows("from_cash")) == 1
    # the maker never accepts their own cash, even holding the permission
    cp2 = mk_cp(client, x.adm, x.b, "T21-B")["id"]
    s2 = open_(client, both_h, cp2).json()
    h2 = close_(client, both_h, s2["id"], {"1000": 1}, receiver=x.rec).json()["handover"]["id"]
    assert code(accept_(client, both_h, h2, expect=403)) == "maker_checker_violation"
    # the receiver can never be the cashier, even one holding the accept permission (D5)
    assert code(close_(client, both_h, s2["id"], {"1000": 1}, receiver=both, expect=409)) == "cash_session_not_open"
    cp3 = mk_cp(client, x.adm, x.b, "T21-C")["id"]
    s3 = open_(client, both_h, cp3).json()
    assert code(close_(client, both_h, s3["id"], {"1000": 1}, receiver=both, expect=422)) == "invalid_receiver"
    # an acceptance key already used on another handover is a conflict, never a second acceptance
    h3 = close_(client, both_h, s3["id"], {"1000": 1}, receiver=x.rec).json()["handover"]["id"]
    assert code(accept_(client, x.rec_h, h3, k=k, expect=409)) == "idempotency_conflict"
    assert accept_(client, x.rec_h, h3).json()["state"] == "closed"
    refused(
        "UPDATE cash_custody_transfers SET state = 'confirmed', accepted_by = from_user_id, accepted_at = now(), "
        "accept_idempotency_key = 'k-123456789012' WHERE id = :h",
        match="maker",
        h=h2,
    )
    refused("UPDATE cash_custody_transfers SET amount = 1 WHERE id = :h", match="immutable", h=h2)
    refused("UPDATE cash_custody_transfers SET state = 'pending' WHERE id = :h", match="terminal", h=h)


# ================================ suspension (D6) + T-019 / T-020 ========================================
def test_suspension_blocks_new_openings_only_and_never_strands_an_active_session(client, sink, tenant_a, monkeypatch):
    x = rworld(client, sink, tenant_a, monkeypatch)  # T-019/T-020 world: x.sess (acceptor) and x.rsess (refunder)
    with SessionLocal() as db:
        cp_sess, cp_rsess = db.get(CashSession, x.sess).cash_point_id, db.get(CashSession, x.rsess).cash_point_id
    suspend(client, x.adm, cp_sess)
    suspend(client, x.adm, cp_rsess)
    # T-019: an accepted rendition still enters the suspended point's open session
    p = fpay(client, x.rcol_h, x.w, "40.00")
    r = declare(client, x.rcol_h, x.b, [p["id"]])
    acc = decide(client, x.cas_h, r["id"], "accept", cash_session_id=x.sess, counted_amount="40.00")
    assert acc["state"] == "accepted" and acc["cash_session_id"] == x.sess
    # T-020: a branch refund still leaves it, exact negative, no reverses_id
    rv = reverse(client, x.rev_h, x, p)
    rf = refund(client, x.rcas_h, rv["id"], cash_session_id=x.rsess)
    with SessionLocal() as db:
        m = db.get(CashMovement, rf["cash_movement_id"])
        assert (m.kind, m.amount, m.reverses_id, m.session_id) == (
            "credit_field_refund",
            Decimal("-40.00"),
            None,
            x.rsess,
        )
    # the Credit port keeps working on an active session of a suspended point
    with SessionLocal() as db:
        out = cash_port.deposit(
            db,
            tenant_id=tenant_a["tenant_id"],
            branch_id=x.b,
            session_id=x.rsess,
            amount=Decimal("5.00"),
            currency="DOP",
            actor_user_id=x.rcas,
            kind="credit_payment_receipt",
            reference="t",
            notes="t",
        )
        db.commit()
        assert out.balance_after == Decimal("965.00")
    # a NEW session cannot open on a suspended point (API and database)
    with SessionLocal() as db, pytest.raises(DBAPIError, match="suspended: no new session"):
        open_v2_session(db, box_id=x.w.cash.box_id, cashier_id=x.rcas, cash_point_id=cp_rsess)
        db.commit()
    hdr, _ = mkuser(client, sink, x.adm, tenant_a, "opener@x.com", [OPEN], scope="branch", branch_id=x.b)
    assert code(open_(client, hdr, cp_rsess, source="zero", amount="0", expect=409)) == "cash_point_not_active"
    # ... and the session can still close and hand its cash to capital
    close_session_now(x.rsess)
    with SessionLocal() as db:
        assert db.get(CashSession, x.rsess).state == "closed"
        assert db.execute(text("SELECT status FROM cash_points WHERE id = :c"), {"c": cp_rsess}).scalar() == "suspended"


# ================================ append-only and consistency backstops ==================================
def test_database_backstops_movements_balance_handovers_and_differences(client, sink, tenant_a):
    x = cworld(client, sink, tenant_a)
    s = open_(client, x.cas_h, x.cp).json()
    (fund,) = movements(s["id"])
    refused("UPDATE cash_movements SET amount = 1 WHERE id = :m", match="history", m=fund.id)
    refused("DELETE FROM cash_movements WHERE id = :m", match="history", m=fund.id)
    ins = (
        "INSERT INTO cash_movements (box_id, session_id, kind, amount, actor_id, notes, reference, created_at{extra}) "
        "VALUES (:b, :s, :k, :a, :u, 'x', 'x', now(){vals})"
    )
    p = dict(b=x.box, s=s["id"], u=x.cas)
    refused(
        ins.format(extra="", vals=""), match="not backed by its movements", **(p | dict(k="expense", a=-1))
    )  # balance untouched
    refused(
        ins.format(extra=", cash_point_id", vals=", :cp"),
        match="carries the tenant",
        **(p | dict(k="expense", a=-1, cp=x.cp + 999)),
    )
    refused(
        ins.format(extra="", vals=""),
        match="uq_cash_movements_opening_fund",
        **(p | dict(k="opening_capital_fund", a=5)),
    )
    cp2 = mk_cp(client, x.adm, x.b, "T21-Z")["id"]
    z = open_(client, x.cas_h, cp2, source="zero", amount="0").json()  # only a v2 capital opening admits its fund
    refused(
        ins.format(extra="", vals=""),
        match="opening capital fund",
        **(p | dict(s=z["id"], k="opening_capital_fund", a=5)),
    )
    refused(
        ins.format(extra="", vals=""),
        match="closing capital transfer",
        **(p | dict(k="closing_capital_transfer", a=-5)),
    )
    refused("UPDATE cash_sessions SET balance = balance + 1 WHERE id = :s", match="not backed", s=s["id"])
    with SessionLocal() as db:  # a writer that omits the anchors gets the session's (compatibility bridge)
        db.execute(text(ins.format(extra="", vals="")), p | dict(k="expense", a=-10))
        db.execute(text("UPDATE cash_sessions SET balance = balance - 10 WHERE id = :s"), {"s": s["id"]})
        db.commit()
        row = db.execute(
            text("SELECT tenant_id, cash_point_id, currency_code FROM cash_movements WHERE kind = 'expense'")
        ).one()
        assert tuple(row) == (x.t, x.cp, "DOP")
    refused(
        "INSERT INTO cash_custody_transfers (company_id, box_id, session_id, kind, from_user_id, to_user_id, amount, state, "
        "notes, created_at, version, currency_code, provenance) VALUES (:t, :b, :s, 'closing_capital', :u, :r, 5, 'pending', '', "
        "now(), 1, 'DOP', 'legacy')",
        match="only migration 0020",
        t=x.t,
        b=x.box,
        s=s["id"],
        u=x.cas,
        r=x.rec,
    )
    refused(
        "INSERT INTO cash_session_differences (tenant_id, session_id, cash_point_id, phase, currency_code, expected, counted, "
        "difference, observation_note, status, provenance, detected_by, detected_at, created_at) VALUES (:t, :s, :cp, 'closing', "
        "'DOP', 10, 5, -5, 'x', 'pending_review', 'legacy_migration', :u, now(), now())",
        match="only migration 0020",
        t=x.t,
        s=s["id"],
        cp=x.cp,
        u=x.cas,
    )
    with SessionLocal() as db:  # a cash box always brings its base CashPoint (no box without its operational position)
        row = db.execute(text("SELECT origin, box_id, code, status FROM cash_points WHERE id = :c"), {"c": x.cp}).one()
        assert tuple(row) == ("legacy_box", x.box, f"CAJA-{x.box}", "active")


# ================================ reads, scope, purity and query counts ==================================
def _statements():
    seen: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    return seen, lambda: event.remove(engine, "before_cursor_execute", before)


def test_reads_are_scoped_pure_and_operations_use_a_bounded_number_of_queries(client, sink, tenant_a, tenant_b):
    x = cworld(client, sink, tenant_a)
    s = open_(client, x.cas_h, x.cp).json()
    for m in range(30):  # a busier session must not cost more queries to close or read
        with SessionLocal() as db:
            cash_port.deposit(
                db,
                tenant_id=x.t,
                branch_id=x.b,
                session_id=s["id"],
                amount=Decimal("1.00"),
                currency="DOP",
                actor_user_id=x.cas,
                kind="credit_payment_receipt",
                reference=f"r{m}",
                notes="n",
            )
            db.commit()
    seen, stop = _statements()
    try:
        assert client.get(f"{CASH}/sessions/{s['id']}", headers=x.cas_h).json()["balance"] == "1030.00"
        client.get(f"{CASH}/sessions/current", headers=x.cas_h, params={"cash_point_id": x.cp})
        client.get(f"{CASH}/handovers", headers=x.rec_h)
        client.get(f"{CASH}/differences", headers=x.rec_h)
        assert not [
            q for q in seen if q.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]  # GET never writes
        seen.clear()
        c = close_(client, x.cas_h, s["id"], {"1000": 1, "10": 3}, receiver=x.rec).json()
        close_queries = len(seen)
        seen.clear()
        accept_(client, x.rec_h, c["handover"]["id"])
        accept_queries = len(seen)
        seen.clear()
        open_(client, x.cas_h, x.cp, source="zero", amount="0")
        open_queries = len(seen)
    finally:
        stop()
    assert close_queries <= 45 and accept_queries <= 45 and open_queries <= 45, (
        close_queries,
        accept_queries,
        open_queries,
    )
    # scope: a reader of another branch or tenant sees nothing of this session
    b2 = mk_branch(client, x.adm, "T21FAR")["id"]
    far_h, _ = mkuser(
        client, sink, x.adm, tenant_a, "farread@x.com", [READ, ACCEPT, DIFF_READ], scope="branch", branch_id=b2
    )
    assert client.get(f"{CASH}/sessions/{s['id']}", headers=far_h).status_code == 403
    assert client.get(f"{CASH}/handovers", headers=far_h, params={"branch_id": x.b}).status_code == 403
    assert client.get(f"{CASH}/handovers", headers=far_h, params={"state": "confirmed"}).json()["items"] == []
    assert client.get(f"{CASH}/sessions/{s['id']}", headers=admin_headers(client, tenant_b)).status_code == 404


# ================================ permissions =============================================================
def test_permissions_are_catalogued_and_only_the_system_role_receives_them(client, sink, tenant_a):
    assert {OPEN, CLOSE, READ, ACCEPT, DIFF_READ} <= CATALOG_CODES
    with SessionLocal() as db:
        rows = db.execute(
            text(
                "SELECT p.code, p.is_sensitive FROM permissions p WHERE p.code LIKE 'cash.sessions.%' "
                "OR p.code IN ('cash.handovers.accept', 'cash.differences.read')"
            )
        ).all()
        assert dict(rows) == {READ: False, OPEN: True, CLOSE: True, ACCEPT: True, DIFF_READ: False}
    mig = (ROOT / "alembic/versions/0020_cashpoint_session_lifecycle.py").read_text(encoding="utf-8")
    assert "WHERE r.system_defined AND r.tenant_id IS NOT NULL" in mig  # grants: the tenant system role only


# ================================ migration 0020 =========================================================
def test_migration_sql_is_a_verbatim_copy_of_the_model_ddl():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "m0020", ROOT / "alembic/versions/0020_cashpoint_session_lifecycle.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.CASH_FUNCTIONS == list(ddl.FUNCTIONS)
    assert mod.CASH_TRIGGERS == list(ddl.TRIGGERS)
    assert mod.CASH_FUNCTION_NAMES == list(ddl.FUNCTION_NAMES)


FILL = {
    "int4": 0,
    "int2": 0,
    "int8": 0,
    "varchar": "x",
    "text": "x",
    "bool": False,
    "numeric": 0,
    "date": date(2026, 9, 1),
    "timestamptz": datetime(2026, 9, 1, 12, tzinfo=UTC),
    "json": "{}",
    "jsonb": "{}",
}


def _plant(c, table, **values):
    """Insert a row filling every NOT NULL column without default (history planting; FKs/triggers off by replica mode)."""
    cols = c.execute(
        text("SELECT column_name, udt_name FROM information_schema.columns WHERE table_name = :t"), {"t": table}
    ).all()
    types = dict(cols)
    for name, udt in c.execute(
        text(
            "SELECT column_name, udt_name FROM information_schema.columns WHERE table_name = :t "
            "AND is_nullable = 'NO' AND column_default IS NULL"
        ),
        {"t": table},
    ).all():
        values.setdefault(name, FILL[udt])
    values = {
        k: (json.dumps(v) if types[k] in ("json", "jsonb") and not isinstance(v, str) else v) for k, v in values.items()
    }
    names = list(values)
    return c.execute(
        text(
            f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join(f'CAST(:{n} AS {types[n]})' for n in names)}) "
            "RETURNING id"
        ),
        values,
    ).scalar()


def _legacy_world(url):
    """Legacy cash history at 0019 (planted): box1 with its 0005 CashPoint and four concurrently active sessions (two open, one
    closing_transfer_pending, one closing_review holding cash), a closing_review without cash, a closed day with a
    closing_adjustment; box2 created after 0005 (no CashPoint) with one open session."""
    eng = create_engine(url)
    w = SimpleNamespace()
    with eng.begin() as c:
        c.execute(text("SET LOCAL session_replication_role = replica"))
        w.t = _plant(
            c,
            "companies",
            name="Vieja",
            slug="vieja",
            status="active",
            base_currency_code="DOP",
            default_timezone="America/Santo_Domingo",
        )
        c.execute(
            text("INSERT INTO tenant_currencies (tenant_id, currency_code, enabled_at) VALUES (:t, 'DOP', now())"),
            {"t": w.t},
        )
        w.b1 = _plant(c, "branches", company_id=w.t, code="SUC-1", status="active", name="Centro")
        w.b2 = _plant(c, "branches", company_id=w.t, code="SUC-2", status="active", name="Norte")
        w.u = [
            _plant(c, "users", company_id=w.t, email=f"u{i}@x.com", status="active", full_name=f"U{i}")
            for i in range(5)
        ]
        w.sys_role = _plant(
            c, "roles", tenant_id=w.t, name="Administrador de agencia", status="active", system_defined=True
        )
        w.custom_role = _plant(c, "roles", tenant_id=w.t, name="Cajeros", status="active", system_defined=False)
        c.execute(
            text("INSERT INTO cash_configs (company_id, activated_at, activated_by) VALUES (:t, now(), :u)"),
            {"t": w.t, "u": w.u[0]},
        )
        w.box1 = _plant(c, "cash_boxes", company_id=w.t, branch_id=w.b1, initial_balance=0, version=1)
        w.box2 = _plant(c, "cash_boxes", company_id=w.t, branch_id=w.b2, initial_balance=0, version=1)
        w.cp1 = _plant(
            c, "cash_points", tenant_id=w.t, branch_id=w.b1, code=f"CAJA-{w.box1}", name="Caja Centro", status="active"
        )

        def sess(
            box,
            state,
            at,
            opened=1000,
            balance=None,
            counted=None,
            difference=None,
            snapshot=None,
            cashier=None,
            notes="",
        ):
            return _plant(
                c,
                "cash_sessions",
                box_id=box,
                business_date=date(2026, 9, 1),
                state=state,
                opening_expected=opened,
                opening_counted=opened,
                balance=opened if balance is None else balance,
                counted=counted,
                difference=difference,
                snapshot=snapshot or {},
                denominations={},
                notes=notes,
                opened_by=w.u[0],
                cashier_id=cashier,
                opened_at=datetime(2026, 9, 1, at, tzinfo=UTC),
                version=1,
            )

        w.s_open1 = sess(w.box1, "open", 8, cashier=w.u[1], balance=1100)
        _plant(
            c,
            "cash_movements",
            box_id=w.box1,
            session_id=w.s_open1,
            kind="counter_payment",
            amount=100,
            actor_id=w.u[1],
            notes="cobro",
            reference="",
            created_at=datetime(2026, 9, 1, 9, tzinfo=UTC),
        )
        w.s_open2 = sess(w.box1, "open", 9, cashier=None)  # ownerless legacy session: owner falls back to opened_by
        w.s_ctp = sess(
            w.box1,
            "closing_transfer_pending",
            10,
            cashier=w.u[2],
            counted=1000,
            difference=0,
            snapshot={"expected": "1000.00"},
        )
        w.t_ctp = _plant(
            c,
            "cash_custody_transfers",
            company_id=w.t,
            box_id=w.box1,
            session_id=w.s_ctp,
            kind="closing_capital",
            from_user_id=w.u[2],
            to_user_id=w.u[4],
            amount=1000,
            state="pending",
            notes="",
            version=1,
        )
        w.s_cr = sess(
            w.box1,
            "closing_review",
            11,
            cashier=w.u[3],
            counted=700,
            difference=-200,
            snapshot={"expected": "900.00"},
            balance=900,
            opened=900,
            notes="Faltan 200",
        )
        w.s_cr0 = sess(
            w.box1,
            "closing_review",
            7,
            cashier=w.u[3],
            counted=0,
            difference=-50,
            snapshot={"expected": "50.00"},
            balance=50,
            opened=50,
            notes="Todo faltante",
        )
        w.s_closed = sess(
            w.box1,
            "closed",
            6,
            cashier=w.u[1],
            counted=480,
            difference=-20,
            snapshot={"expected": "500.00"},
            balance=0,
            opened=500,
        )
        w.adj = _plant(
            c,
            "cash_movements",
            box_id=w.box1,
            session_id=w.s_closed,
            kind="closing_adjustment",
            amount=-20,
            actor_id=w.u[0],
            notes="ajuste legacy",
            reference="",
            created_at=datetime(2026, 9, 1, 6, tzinfo=UTC),
        )
        _plant(
            c,
            "cash_movements",
            box_id=w.box1,
            session_id=w.s_closed,
            kind="capital_transfer",
            amount=-480,
            actor_id=w.u[0],
            notes="cierre legacy",
            reference="",
            created_at=datetime(2026, 9, 1, 6, tzinfo=UTC),
        )
        w.s_box2 = sess(w.box2, "open", 8, cashier=w.u[1], opened=0, balance=0)
    return eng, w


def _rows(eng, sql_, **p):
    with eng.connect() as c:
        return c.execute(text(sql_), p).all()


def test_migration_0020_maps_legacy_history_without_rewriting_it_and_downgrades_losslessly(scratch_db):
    assert _alembic(scratch_db, "upgrade", "0019").returncode == 0
    eng, w = _legacy_world(scratch_db)
    movements_before = _rows(eng, "SELECT id, session_id, kind, amount FROM cash_movements ORDER BY id")
    capital_before = _rows(eng, "SELECT count(*) FROM capital_movements")
    up = _alembic(scratch_db, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert _alembic(scratch_db, "check").returncode == 0
    cps = {
        r.code: r
        for r in _rows(eng, "SELECT id, code, origin, box_id, status, suspension_reason, branch_id FROM cash_points")
    }
    assert (cps[f"CAJA-{w.box1}"].origin, cps[f"CAJA-{w.box1}"].box_id) == (
        "legacy_box",
        w.box1,
    )  # the 0005 point adopted
    assert (cps[f"CAJA-{w.box2}"].origin, cps[f"CAJA-{w.box2}"].box_id, cps[f"CAJA-{w.box2}"].status) == (
        "legacy_box",
        w.box2,
        "active",
    )
    # D8 + D12: first active by (opened_at, id) keeps the base point; the others get suspended MIG-S points
    for sid in (w.s_open2, w.s_ctp, w.s_cr):
        mig = cps[f"MIG-S{sid}"]
        assert (mig.origin, mig.status, mig.box_id, mig.branch_id) == ("legacy_session_split", "suspended", None, w.b1)
        assert mig.suspension_reason == "Posición generada por migración para sesión legacy concurrente"
    s = {
        r.id: r
        for r in _rows(
            eng,
            "SELECT id, state, cash_point_id, cashier_id, opening_contract, close_contract, balance, "
            "balance_base, tenant_id, currency_code FROM cash_sessions",
        )
    }
    assert (
        s[w.s_open1].cash_point_id == cps[f"CAJA-{w.box1}"].id
        and s[w.s_open2].cash_point_id == cps[f"MIG-S{w.s_open2}"].id
    )
    assert s[w.s_box2].cash_point_id == cps[f"CAJA-{w.box2}"].id
    assert {sid: s[sid].state for sid in s} == {
        w.s_open1: "open",
        w.s_open2: "open",
        w.s_ctp: "closing",
        w.s_cr: "closing",
        w.s_cr0: "closed",
        w.s_closed: "closed",
        w.s_box2: "open",
    }
    assert s[w.s_open2].cashier_id == w.u[0] and all(
        r.opening_contract == "legacy" and r.tenant_id == w.t for r in s.values()
    )
    assert s[w.s_open1].balance == s[w.s_open1].balance_base + 100  # frozen base: balance = base + movements
    assert (
        _rows(
            eng,
            "SELECT cash_point_id FROM cash_sessions WHERE state IN ('open', 'closing') GROUP BY 1 HAVING count(*) > 1",
        )
        == []
    )
    d = {
        r.session_id: r
        for r in _rows(
            eng,
            "SELECT session_id, phase, expected, counted, difference, status, provenance, "
            "observation_note FROM cash_session_differences",
        )
    }
    assert set(d) == {w.s_cr, w.s_cr0}
    assert (d[w.s_cr].expected, d[w.s_cr].counted, d[w.s_cr].difference, d[w.s_cr].status, d[w.s_cr].provenance) == (
        Decimal("900.00"),
        Decimal("700.00"),
        Decimal("-200.00"),
        "pending_review",
        "legacy_migration",
    )
    h = {
        r.session_id: r
        for r in _rows(eng, "SELECT id, session_id, provenance, amount, state, to_user_id FROM cash_custody_transfers")
    }
    assert (h[w.s_ctp].id, h[w.s_ctp].provenance) == (w.t_ctp, "legacy")  # D9: adopted, same identity
    assert (h[w.s_cr].provenance, h[w.s_cr].amount, h[w.s_cr].state, h[w.s_cr].to_user_id) == (
        "migration",
        Decimal("700.00"),
        "pending",
        None,
    )
    assert w.s_cr0 not in h  # D11: no cash, no handover
    assert (
        _rows(eng, "SELECT id, session_id, kind, amount FROM cash_movements ORDER BY id") == movements_before
    )  # nothing booked
    assert _rows(eng, "SELECT count(*) FROM capital_movements") == capital_before
    grants = {
        r.role_id
        for r in _rows(
            eng,
            "SELECT rp.role_id FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id "
            "WHERE p.code = 'cash.handovers.accept'",
        )
    }
    assert grants == {w.sys_role}
    # lossless downgrade of purely migrated data, then the same mapping again
    down = _alembic(scratch_db, "downgrade", "0019")
    assert down.returncode == 0, down.stderr
    assert {r.id: r.state for r in _rows(eng, "SELECT id, state FROM cash_sessions")} == {
        w.s_open1: "open",
        w.s_open2: "open",
        w.s_ctp: "closing_transfer_pending",
        w.s_cr: "closing_review",
        w.s_cr0: "closing_review",
        w.s_closed: "closed",
        w.s_box2: "open",
    }
    assert _rows(eng, "SELECT count(*) FROM cash_points WHERE code LIKE 'MIG-S%'") == [(0,)]
    assert [r.id for r in _rows(eng, "SELECT id FROM cash_custody_transfers")] == [w.t_ctp]
    assert _rows(eng, "SELECT id, session_id, kind, amount FROM cash_movements ORDER BY id") == movements_before
    assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    # the migrated handovers are completed through the T-021 flow; the sessions close and the MIG point stays suspended
    receiver = Principal(user_id=w.u[4], tenant_id=w.t, person_id=None, session_id=0, grants=(Grant(ACCEPT, "tenant"),))
    with Session(eng) as db:
        mh = db.execute(text("SELECT id FROM cash_custody_transfers WHERE provenance = 'migration'")).scalar()
        maker = Principal(
            user_id=w.u[3], tenant_id=w.t, person_id=None, session_id=0, grants=(Grant(ACCEPT, "tenant"),)
        )
        with pytest.raises(Exception) as err:
            cash_core.accept_handover(db, maker, mh, idempotency_key="t21-mig-accept-0")
        assert err.value.code == "maker_checker_violation"
        db.rollback()
        out = cash_core.accept_handover(db, receiver, mh, idempotency_key="t21-mig-accept-1")
        legacy_out = cash_core.accept_handover(db, receiver, w.t_ctp, idempotency_key="t21-mig-accept-2")
        db.commit()
    assert (out["state"], out["handover"]["amount"], legacy_out["state"]) == ("closed", "700.00", "closed")
    assert _rows(eng, "SELECT kind, amount FROM cash_movements WHERE session_id = :s ORDER BY id", s=w.s_cr) == [
        ("closing_capital_transfer", Decimal("-700.00"))
    ]
    assert _rows(eng, "SELECT status FROM cash_points WHERE code = :c", c=f"MIG-S{w.s_cr}") == [("suspended",)]
    assert _rows(eng, "SELECT amount FROM cash_movements WHERE id = :m", m=w.adj) == [
        (Decimal("-20.00"),)
    ]  # D7 history intact
    refused_down = _alembic(scratch_db, "downgrade", "0019")
    assert refused_down.returncode != 0 and "closing handovers created or accepted under T-021" in refused_down.stderr
    assert _rows(eng, "SELECT version_num FROM alembic_version") == [("0022",)]
    eng.dispose()


@pytest.mark.parametrize(
    "bad, problem",
    [
        ("UPDATE cash_sessions SET state = 'weird' WHERE id = {s_open2}", "unknown cash session states"),
        ("UPDATE cash_sessions SET state = 'opening_review' WHERE id = {s_open2}", "opening_review"),
        (
            "DELETE FROM cash_custody_transfers WHERE id = {t_ctp}",
            "without exactly one pending closing_capital transfer",
        ),
        ("UPDATE cash_sessions SET state = 'open' WHERE id = {s_ctp}", "orphans"),
        ("UPDATE cash_custody_transfers SET session_id = {s_cr} WHERE id = {t_ctp}", "closing_capital transfer"),
        ("UPDATE cash_sessions SET counted = NULL WHERE id = {s_cr}", "NULL or negative physical count"),
        ("UPDATE cash_sessions SET difference = -199 WHERE id = {s_cr}", "missing or inconsistent"),
        ("UPDATE cash_sessions SET snapshot = '{{}}' WHERE id = {s_cr}", "missing or inconsistent"),
        ("UPDATE cash_custody_transfers SET amount = 999 WHERE id = {t_ctp}", "differs from the stored count"),
        (
            "UPDATE cash_points SET branch_id = {b2} WHERE id = {cp1}",
            "CAJA-<box_id> codes taken by a cash point of another branch",
        ),
        (
            "INSERT INTO cash_points (tenant_id, branch_id, code, name, status, created_at, updated_at) "
            "VALUES ({t}, {b1}, 'MIG-S{s_open2}', 'x', 'active', now(), now())",
            "MIG-S<session_id> codes already taken",
        ),
        ("UPDATE cash_sessions SET cashier_id = NULL, opened_by = 999999 WHERE id = {s_open2}", "valid owner"),
    ],
)
def test_migration_0020_preflight_refuses_incompatible_legacy_data_without_changing_it(scratch_db, bad, problem):
    assert _alembic(scratch_db, "upgrade", "0019").returncode == 0
    eng, w = _legacy_world(scratch_db)
    with eng.begin() as c:
        c.execute(text("SET LOCAL session_replication_role = replica"))
        c.execute(text(bad.format(**vars(w))))
    before = _rows(eng, "SELECT id, state, counted FROM cash_sessions ORDER BY id")
    out = _alembic(scratch_db, "upgrade", "head")
    assert out.returncode != 0 and "preflight refused" in out.stderr and problem in out.stderr, out.stderr[-600:]
    assert _rows(eng, "SELECT version_num FROM alembic_version") == [("0019",)]
    assert _rows(eng, "SELECT id, state, counted FROM cash_sessions ORDER BY id") == before  # nothing was mutated
    assert _rows(
        eng,
        "SELECT count(*) FROM information_schema.columns WHERE table_name = 'cash_points' AND column_name = 'origin'",
    ) == [(0,)]
    eng.dispose()


def test_migration_0020_empty_upgrade_downgrade_reupgrade_and_v2_history_refuses_downgrade(scratch_db):
    assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    assert _alembic(scratch_db, "check").returncode == 0
    assert _alembic(scratch_db, "downgrade", "0019").returncode == 0
    assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    eng = create_engine(scratch_db)
    with eng.begin() as c:
        c.execute(
            text("SET LOCAL session_replication_role = replica")
        )  # plant one v2 session (history created under T-021)
        _plant(
            c,
            "cash_sessions",
            box_id=1,
            tenant_id=1,
            cash_point_id=1,
            currency_code="DOP",
            business_date=date(2026, 10, 1),
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
    out = _alembic(scratch_db, "downgrade", "0019")
    assert out.returncode != 0 and "v2 cash sessions exist" in out.stderr
    with eng.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0022"
    eng.dispose()
