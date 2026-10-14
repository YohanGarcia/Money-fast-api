"""T-021H cash session idempotency hardening (T021H-*). PostgreSQL only.

T-021 anchors three commands on TENANT-GLOBAL idempotency keys (open, close, capital-handover accept) while its locks are
BRANCH-LOCAL (the cash box). Two classes of race follow:

* the same key claimed from two branches: the loser used to surface a raw ``IntegrityError`` (500) -> now a canonical
  ``IdempotencyConflict`` (409), with nothing of the loser left behind;
* two IDENTICAL concurrent requests for the same resource: the second used to meet ``cash_point_busy`` /
  ``cash_session_not_open`` / ``cash_handover_not_pending`` after the first committed -> now a ``replayed`` answer.

Business semantics and the historical digests are untouched. The races are made deterministic with a barrier placed on a
seam that exists both before and after the fix (so the regressions fail on the base code).
"""

import hashlib
import json
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.errors import IdempotencyConflict
from app.models.cash import CashCustodyTransfer, CashSession
from app.modules.cash import sessions as cash_core
from app.modules.identity.authorization import build_principal
from app.modules.identity.models import UserAccount
from tests import pg_env  # noqa: F401  (must precede app imports)
from tests.test_t002_identity import (  # noqa: F401  (fixtures + helpers shared with the earlier suites)
    client,
    events,
    fresh_db,
    sink,
    tenant_a,
    tenant_b,
)
from tests.test_t006_origination import count
from tests.test_t012_collection_assignment import mkuser
from tests.test_t021_cashpoint_session_lifecycle import (
    accept_,
    close_,
    code,
    cworld,
    key,
    open_,
)

COUNTS = {"1000": 1}
OPEN_KEY = "uq_cash_sessions_open_key"
CLOSE_KEY = "uq_cash_sessions_close_key"
ACCEPT_KEY = "uq_cash_custody_transfers_accept_key"


# ================================ harness =================================================================
def seam(monkeypatch, obj, name, parties=2, when=lambda args, kwargs: True):
    """Wrap ``obj.name`` so each thread waits (once) on a barrier right AFTER the real call returns."""
    real, barrier, local = getattr(obj, name), threading.Barrier(parties), threading.local()

    def wrapper(*args, **kwargs):
        out = real(*args, **kwargs)
        if when(args, kwargs) and not getattr(local, "done", False):
            local.done = True
            barrier.wait(30)
        return out

    monkeypatch.setattr(obj, name, wrapper)


def race(*jobs):
    """Run each ``job(db, principal_for)`` in its own thread and Session; commit on success, roll back on failure."""
    out: list = [None] * len(jobs)

    def run(i, user_id, job):
        with SessionLocal() as db:
            actor = build_principal(db, db.get(UserAccount, user_id), 0)
            try:
                result = job(db, actor)
                db.commit()
                out[i] = ("ok", result.get("replayed"), result)
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                out[i] = (type(exc).__name__, getattr(exc, "status_code", None), None)

    threads = [threading.Thread(target=run, args=(i, uid, job)) for i, (uid, job) in enumerate(jobs)]
    [t.start() for t in threads]
    [t.join(90) for t in threads]
    assert all(o is not None for o in out), f"a worker never finished: {out}"
    return out


def opener(cp, k, amount="1000.00"):
    return lambda db, actor: cash_core.open_session(
        db,
        actor,
        cash_point_id=cp,
        source="capital",
        amount=amount,
        denominations=COUNTS,
        observation_note=None,
        idempotency_key=k,
    )


def closer(sid, receiver, k):
    return lambda db, actor: cash_core.close_session(
        db, actor, sid, denominations=COUNTS, observation_note=None, receiver_user_id=receiver, idempotency_key=k
    )


def accepter(hid, k):
    return lambda db, actor: cash_core.accept_handover(db, actor, hid, idempotency_key=k)


def two_branches(client, sink, tenant):
    return cworld(client, sink, tenant, "a"), cworld(client, sink, tenant, "b")


def closing_handover(client, x):
    """An open capital session of 1000 closed with the exact count: a pending closing handover to ``x.rec``."""
    s = open_(client, x.cas_h, x.cp, amount="1000.00").json()
    closed = close_(client, x.cas_h, s["id"], COUNTS, receiver=x.rec).json()
    assert closed["state"] == "closing"
    return s["id"], closed["handover"]["id"]


def money():
    return {t: count(t) for t in ("cash_movements", "capital_movements", "cash_sessions", "cash_custody_transfers")}


def verdict(out):
    winners = [o for o in out if o[0] == "ok"]
    losers = [o for o in out if o[0] != "ok"]
    return winners, losers


# ================================ 1. cross-branch: the same tenant key from two branches ==================
def test_open_same_key_from_two_branches_ends_as_idempotency_conflict(client, sink, tenant_a, monkeypatch):
    x1, x2 = two_branches(client, sink, tenant_a)
    shared, before = key("t21h"), money()
    # both pass the replay lookups and the locks of THEIR OWN box, then claim the key together
    seam(monkeypatch, cash_core.capital_service, "balance")
    out = race((x1.cas, opener(x1.cp, shared)), (x2.cas, opener(x2.cp, shared)))
    winners, losers = verdict(out)
    assert len(winners) == 1 and winners[0][1] is False
    assert [(o[0], o[1]) for o in losers] == [("IdempotencyConflict", 409)]
    assert count("cash_sessions", "open_idempotency_key = :k", k=shared) == 1
    after = money()  # the loser left nothing: one session, one fund movement, one capital to_cash
    assert (after["cash_sessions"] - before["cash_sessions"], after["cash_movements"] - before["cash_movements"]) == (
        1,
        1,
    )
    assert after["capital_movements"] - before["capital_movements"] == 1
    assert len(events("cash.session.opened")) == 1


def test_close_same_key_from_two_branches_ends_as_idempotency_conflict(client, sink, tenant_a, monkeypatch):
    x1, x2 = two_branches(client, sink, tenant_a)
    s1 = open_(client, x1.cas_h, x1.cp, amount="1000.00").json()["id"]
    s2 = open_(client, x2.cas_h, x2.cp, amount="1000.00").json()["id"]
    shared, before = key("t21h"), money()
    seam(monkeypatch, cash_core, "_valid_receiver")  # after the locks, right before the key is claimed
    out = race((x1.cas, closer(s1, x1.rec, shared)), (x2.cas, closer(s2, x2.rec, shared)))
    winners, losers = verdict(out)
    assert len(winners) == 1 and winners[0][1] is False
    assert [(o[0], o[1]) for o in losers] == [("IdempotencyConflict", 409)]
    assert count("cash_sessions", "close_idempotency_key = :k", k=shared) == 1
    won = winners[0][2]["id"]
    lost = ({s1, s2} - {won}).pop()
    with SessionLocal() as db:
        loser = db.get(CashSession, lost)
        assert (loser.state, loser.close_idempotency_key, loser.counted, loser.closed_by) == ("open", None, None, None)
    assert count("cash_custody_transfers", "session_id = :s", s=lost) == 0
    assert count("cash_session_differences", "session_id = :s", s=lost) == 0
    after = money()
    assert after["cash_custody_transfers"] - before["cash_custody_transfers"] == 1  # the winner's handover only
    assert len(events("cash.session.closing_counted")) == 1


def test_capital_accept_same_key_from_two_branches_ends_as_idempotency_conflict(client, sink, tenant_a, monkeypatch):
    x1, x2 = two_branches(client, sink, tenant_a)
    s1, h1 = closing_handover(client, x1)
    s2, h2 = closing_handover(client, x2)
    shared, before = key("t21h"), money()
    seam(monkeypatch, cash_core, "now_utc")  # after validation, before any movement / key claim
    out = race((x1.rec, accepter(h1, shared)), (x2.rec, accepter(h2, shared)))
    winners, losers = verdict(out)
    assert len(winners) == 1 and winners[0][1] is False
    assert [(o[0], o[1]) for o in losers] == [("IdempotencyConflict", 409)]
    assert count("cash_custody_transfers", "accept_idempotency_key = :k", k=shared) == 1
    won_session = winners[0][2]["id"]
    lost_session, lost_handover = (s2, h2) if won_session == s1 else (s1, h1)
    with SessionLocal() as db:
        h, s = db.get(CashCustodyTransfer, lost_handover), db.get(CashSession, lost_session)
        assert (h.state, h.accepted_by, h.cash_movement_id, h.capital_movement_id) == ("pending", None, None, None)
        assert (s.state, s.balance) == ("closing", 1000)
    after = money()  # exactly the winner's closing movement and capital from_cash, nothing from the loser
    assert after["cash_movements"] - before["cash_movements"] == 1
    assert after["capital_movements"] - before["capital_movements"] == 1
    assert len(events("cash.handover.accepted")) == 1


# ================================ 2. identical concurrent retries =========================================
def test_open_identical_concurrent_retry_is_a_replay_not_cash_point_busy(client, sink, tenant_a, monkeypatch):
    x = cworld(client, sink, tenant_a)
    k, before = key("t21h"), money()
    # both pass the FIRST replay lookup (nobody has committed), then serialise on the box lock
    seam(monkeypatch, cash_core, "_cash_point", when=lambda a, kw: not kw.get("lock"))
    out = race((x.cas, opener(x.cp, k)), (x.cas, opener(x.cp, k)))
    winners, losers = verdict(out)
    assert losers == [] and sorted(o[1] for o in winners) == [False, True]
    assert winners[0][2]["id"] == winners[1][2]["id"]
    after = money()
    assert (after["cash_sessions"] - before["cash_sessions"], after["cash_movements"] - before["cash_movements"]) == (
        1,
        1,
    )
    assert after["capital_movements"] - before["capital_movements"] == 1
    assert len(events("cash.session.opened")) == 1


def test_close_identical_concurrent_retry_is_a_replay_not_session_not_open(client, sink, tenant_a, monkeypatch):
    x = cworld(client, sink, tenant_a)
    sid = open_(client, x.cas_h, x.cp, amount="1000.00").json()["id"]
    k, before = key("t21h"), money()
    seam(monkeypatch, cash_core, "_cash_point", when=lambda a, kw: not kw.get("lock"))
    out = race((x.cas, closer(sid, x.rec, k)), (x.cas, closer(sid, x.rec, k)))
    winners, losers = verdict(out)
    assert losers == [] and sorted(o[1] for o in winners) == [False, True]
    assert winners[0][2]["handover"]["id"] == winners[1][2]["handover"]["id"]
    assert count("cash_custody_transfers", "session_id = :s", s=sid) == 1
    assert money()["cash_custody_transfers"] - before["cash_custody_transfers"] == 1
    assert len(events("cash.session.closing_counted")) == 1


def test_capital_accept_identical_concurrent_retry_is_a_replay_not_handover_not_pending(
    client, sink, tenant_a, monkeypatch
):
    x = cworld(client, sink, tenant_a)
    sid, hid = closing_handover(client, x)
    k, before = key("t21h"), money()
    seam(monkeypatch, cash_core, "_session", when=lambda a, kw: not kw.get("lock"))
    out = race((x.rec, accepter(hid, k)), (x.rec, accepter(hid, k)))
    winners, losers = verdict(out)
    assert losers == [] and sorted(o[1] for o in winners) == [False, True]
    after = money()
    assert after["cash_movements"] - before["cash_movements"] == 1  # one closing transfer
    assert after["capital_movements"] - before["capital_movements"] == 1  # one linked capital from_cash
    assert len(events("cash.handover.accepted")) == 1
    with SessionLocal() as db:
        assert db.get(CashSession, sid).state == "closed"


# ================================ 3. classification: only the named key constraint ========================
def failing_flush(monkeypatch, constraint, trigger):
    """Make the claim flush fail like PostgreSQL would for ``constraint`` (the real winner row is created by the caller)."""
    real = Session.flush

    def flush(self, *args, **kwargs):
        if trigger(self):
            raise IntegrityError("UPDATE", {}, SimpleNamespace(diag=SimpleNamespace(constraint_name=constraint)))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Session, "flush", flush)


def blind(monkeypatch, obj, name, calls=2):
    """The first ``calls`` replay lookups see nothing (the winner has not committed yet); later ones are real."""
    real, seen = getattr(obj, name), []

    def lookup(*args, **kwargs):
        seen.append(1)
        return None if len(seen) <= calls else real(*args, **kwargs)

    monkeypatch.setattr(obj, name, lookup)


def test_open_real_key_collision_is_classified_and_the_session_stays_usable(client, sink, tenant_a, monkeypatch):
    x1, x2 = two_branches(client, sink, tenant_a)
    taken = key("t21h")
    won = open_(client, x1.cas_h, x1.cp, amount="1000.00", k=taken).json()
    before = money()
    blind(monkeypatch, cash_core, "_open_replay")  # the winner "was not visible" before the INSERT: a REAL collision
    with SessionLocal() as db:
        actor = build_principal(db, db.get(UserAccount, x2.cas), 0)
        with pytest.raises(IdempotencyConflict):
            opener(x2.cp, taken)(db, actor)
        # the outer transaction survived the nested rollback and holds nothing of the loser
        assert db.scalar(select(func.count()).select_from(CashSession)) == before["cash_sessions"]
        assert not db.new and not db.dirty
        ok = opener(x2.cp, key("t21h"))(db, actor)  # ...and can keep working: a different key opens normally
        db.commit()
    assert ok["replayed"] is False and ok["id"] != won["id"]
    assert money()["cash_sessions"] == before["cash_sessions"] + 1


def test_close_real_key_collision_is_classified_and_the_session_stays_usable(client, sink, tenant_a, monkeypatch):
    x1, x2 = two_branches(client, sink, tenant_a)
    s1 = open_(client, x1.cas_h, x1.cp, amount="1000.00").json()["id"]
    s2 = open_(client, x2.cas_h, x2.cp, amount="1000.00").json()["id"]
    taken = key("t21h")
    close_(client, x1.cas_h, s1, COUNTS, receiver=x1.rec, k=taken)
    before = money()
    blind(monkeypatch, cash_core, "_close_replay")
    with SessionLocal() as db:
        actor = build_principal(db, db.get(UserAccount, x2.cas), 0)
        with pytest.raises(IdempotencyConflict):
            closer(s2, x2.rec, taken)(db, actor)
        loser = db.get(CashSession, s2)  # restored by the savepoint: open, no close record
        assert (loser.state, loser.close_idempotency_key, loser.counted) == ("open", None, None)
        assert db.scalar(select(func.count()).select_from(CashCustodyTransfer)) == before["cash_custody_transfers"]
        ok = closer(s2, x2.rec, key("t21h"))(db, actor)
        db.commit()
    assert ok["state"] == "closing" and ok["replayed"] is False


def test_capital_accept_real_key_collision_is_classified_before_any_economic_row(client, sink, tenant_a, monkeypatch):
    x1, x2 = two_branches(client, sink, tenant_a)
    s1, h1 = closing_handover(client, x1)
    s2, h2 = closing_handover(client, x2)
    taken = key("t21h")
    accept_(client, x1.rec_h, h1, k=taken)
    before = money()
    blind(monkeypatch, cash_core, "_accept_replay")
    with SessionLocal() as db:
        actor = build_principal(db, db.get(UserAccount, x2.rec), 0)
        with pytest.raises(IdempotencyConflict):
            accepter(h2, taken)(db, actor)
        h, s = db.get(CashCustodyTransfer, h2), db.get(CashSession, s2)
        assert (h.state, h.accept_idempotency_key, h.accepted_by) == ("pending", None, None)
        assert (s.state, s.balance) == ("closing", 1000)
        assert not db.new  # no CashMovement and no CapitalMovement ever left the savepoint
        ok = accepter(h2, key("t21h"))(db, actor)  # the same transaction goes on to accept normally
        db.commit()
    assert ok["state"] == "closed" and ok["replayed"] is False
    assert money()["cash_movements"] == before["cash_movements"] + 1


@pytest.mark.parametrize("constraint", [None, "uq_cash_sessions_active_cash_point", "ck_cash_sessions_state_valid"])
def test_unrelated_integrity_errors_are_never_classified_as_idempotency_conflicts(
    client, sink, tenant_a, monkeypatch, constraint
):
    """A winner for the key EXISTS for each command, so a wrongly broad handler would answer IdempotencyConflict."""
    x1, x2 = two_branches(client, sink, tenant_a)
    xo = cworld(client, sink, tenant_a, "c")  # a free CashPoint to attempt an open on
    taken = key("t21h")
    # branch 1 owns `taken` as an open key, a close key and an accept key (each lives in its own column)
    s1 = open_(client, x1.cas_h, x1.cp, amount="1000.00", k=taken).json()["id"]
    hid1 = close_(client, x1.cas_h, s1, COUNTS, receiver=x1.rec, k=taken).json()["handover"]["id"]
    accept_(client, x1.rec_h, hid1, k=taken)
    so = open_(client, x2.cas_h, x2.cp, amount="1000.00").json()["id"]  # a session of branch 2 to attempt a close on
    _sx, hx = closing_handover(client, cworld(client, sink, tenant_a, "d"))  # a handover to attempt an accept on
    with SessionLocal() as db:
        receiver = db.get(CashCustodyTransfer, hx).to_user_id
    for name in ("_open_replay", "_close_replay", "_accept_replay"):
        blind(monkeypatch, cash_core, name)
    attempts = (
        (xo.cas, lambda s: any(isinstance(o, CashSession) for o in s.new), opener(xo.cp, taken)),
        (
            x2.cas,
            lambda s: any(isinstance(o, CashSession) and o.close_idempotency_key == taken for o in s.dirty),
            closer(so, x2.rec, taken),
        ),
        (
            receiver,
            lambda s: any(isinstance(o, CashCustodyTransfer) and o.accept_idempotency_key == taken for o in s.dirty),
            accepter(hx, taken),
        ),
    )
    for user, trigger, job in attempts:
        with monkeypatch.context() as m, SessionLocal() as db:
            failing_flush(m, constraint, trigger)
            actor = build_principal(db, db.get(UserAccount, user), 0)
            with pytest.raises(IntegrityError):
                job(db, actor)
            db.rollback()


@pytest.mark.parametrize("which", ["open", "close", "accept"])
def test_the_key_constraint_without_a_winning_row_is_not_swallowed(client, sink, tenant_a, monkeypatch, which):
    x = cworld(client, sink, tenant_a)
    if which == "open":
        constraint, job, user = OPEN_KEY, opener(x.cp, key("t21h")), x.cas
        trigger = lambda s: any(isinstance(o, CashSession) for o in s.new)  # noqa: E731
        replay = "_open_replay"
    elif which == "close":
        sid = open_(client, x.cas_h, x.cp, amount="1000.00").json()["id"]
        constraint, job, user = CLOSE_KEY, closer(sid, x.rec, key("t21h")), x.cas
        trigger = lambda s: any(isinstance(o, CashSession) and o.close_idempotency_key for o in s.dirty)  # noqa: E731
        replay = "_close_replay"
    else:
        _sid, hid = closing_handover(client, x)
        constraint, job, user = ACCEPT_KEY, accepter(hid, key("t21h")), x.rec
        trigger = lambda s: any(isinstance(o, CashCustodyTransfer) and o.accept_idempotency_key for o in s.dirty)  # noqa: E731
        replay = "_accept_replay"
    blind(monkeypatch, cash_core, replay, calls=99)  # nothing is ever found: no winner to classify against
    failing_flush(monkeypatch, constraint, trigger)
    with SessionLocal() as db:
        actor = build_principal(db, db.get(UserAccount, user), 0)
        with pytest.raises(IntegrityError):
            job(db, actor)
        db.rollback()


# ================================ 3b. the replay contract that was already in T-021 stays exact ===========
def test_replay_semantics_are_unchanged_owner_digest_and_actor_still_matter(client, sink, tenant_a):
    x = cworld(client, sink, tenant_a)
    other_h, other = mkuser(
        client,
        sink,
        x.adm,
        tenant_a,
        "cas2-t21h@x.com",
        ["cash.sessions.open", "cash.sessions.close"],
        scope="branch",
        branch_id=x.b,
    )
    ko, kc, ka = key("t21h"), key("t21h"), key("t21h")
    s = open_(client, x.cas_h, x.cp, amount="1000.00", k=ko).json()
    # OPEN: the same key + payload by ANOTHER cashier is a conflict, never a replay of someone else's session
    assert open_(client, x.cas_h, x.cp, amount="1000.00", k=ko).json()["replayed"] is True
    assert code(open_(client, other_h, x.cp, amount="1000.00", k=ko, expect=409)) == "idempotency_conflict"
    # CLOSE: the same key with another payload is a conflict
    closed = close_(client, x.cas_h, s["id"], COUNTS, receiver=x.rec, k=kc).json()
    assert close_(client, x.cas_h, s["id"], COUNTS, receiver=x.rec, k=kc).json()["replayed"] is True
    assert (
        code(close_(client, x.cas_h, s["id"], {"500": 2}, receiver=x.rec, k=kc, expect=409)) == "idempotency_conflict"
    )
    # ACCEPT: the same key by another actor is a conflict
    hid = closed["handover"]["id"]
    accept_(client, x.rec_h, hid, k=ka)
    assert accept_(client, x.rec_h, hid, k=ka).json()["replayed"] is True
    assert code(accept_(client, x.cas_h, hid, k=ka, expect=409)) == "idempotency_conflict"


# ================================ 4. historical digests are frozen ========================================
def legacy_digest(payload):
    """The T-021 formula, re-implemented here on purpose: a refactor of the app helper cannot make this test lie."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def test_persisted_digests_still_follow_the_t021_formulas_byte_for_byte(client, sink, tenant_a):
    x = cworld(client, sink, tenant_a)
    s = open_(client, x.cas_h, x.cp, amount="1000.00").json()
    closed = close_(client, x.cas_h, s["id"], COUNTS, receiver=x.rec).json()
    hid = closed["handover"]["id"]
    accept_(client, x.rec_h, hid)
    zero = open_(client, x.cas_h, x.cp, source="zero", amount="0").json()
    shut = close_(
        client, x.cas_h, zero["id"], {"1000": 0}
    ).json()  # counted = 0: the receiver is not part of the payload
    with SessionLocal() as db:
        row = lambda sql, **p: db.execute(text(sql), p).scalar()  # noqa: E731
        assert row("SELECT open_request_digest FROM cash_sessions WHERE id = :i", i=s["id"]) == legacy_digest(
            {
                "operation": "open_cash_session",
                "cash_point_id": x.cp,
                "source": "capital",
                "amount": "1000.00",
                "denominations": COUNTS,
                "observation_note": "",
            }
        )
        assert row("SELECT open_request_digest FROM cash_sessions WHERE id = :i", i=zero["id"]) == legacy_digest(
            {
                "operation": "open_cash_session",
                "cash_point_id": x.cp,
                "source": "zero",
                "amount": "0",  # the digest keeps the textual form of the amount ("0" != "0.00"), exactly as in T-021
                "denominations": {},
                "observation_note": "",
            }
        )
        assert row("SELECT close_request_digest FROM cash_sessions WHERE id = :i", i=s["id"]) == legacy_digest(
            {
                "operation": "close_cash_session",
                "session_id": s["id"],
                "denominations": COUNTS,
                "observation_note": "",
                "receiver_user_id": x.rec,
            }
        )
        assert row("SELECT close_request_digest FROM cash_sessions WHERE id = :i", i=shut["id"]) == legacy_digest(
            {
                "operation": "close_cash_session",
                "session_id": shut["id"],
                "denominations": {},
                "observation_note": "",
                # counted = 0: receiver_user_id is None and the canonical form DROPS None values, so the key is absent
            }
        )
        assert row("SELECT accept_request_digest FROM cash_custody_transfers WHERE id = :i", i=hid) == legacy_digest(
            {"operation": "accept_closing_handover", "handover_id": hid}
        )
