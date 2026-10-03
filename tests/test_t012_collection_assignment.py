"""T-012 Credit Collection Assignment tests (T012-*). PostgreSQL only.

Loan-level, effective-dated assignment history: at most ONE open row per loan, reassignment = close + insert (never an
overwrite), explicit ``end`` only, eligibility by modern RBAC, no access granted, no automation from payments / reversals /
assessment, idempotent commands with separate namespaces, tenant-safe database constraints, history-protecting downgrade.
No worklist filter, reason, backdating, team/provider, legacy pointer or money here.
"""

import json
import threading
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.db import SessionLocal, engine
from app.modules.loans import assignments as assignment_service
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
from tests.test_t006_origination import (  # noqa: F401
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
from tests.test_t007_disbursement import disburse
from tests.test_t008_payments import LEGACY, LOAN_LOCK, pay, schedule
from tests.test_t009_payment_reversal import audit, post_rev, rev, user_id
from tests.test_t010_overdue_projection import contractual_rows, money_counts, world10, write_listener
from tests.test_t011_collection_worklist import clock as clock11
from tests.test_t011_collection_worklist import local, wl

L = f"{V2}/loans"
ENDPOINT = "collection-assignment"
KEYS = iter(range(1, 10**6))
EXPECTED_KEYS = {
    "assignment_id",
    "loan_id",
    "assignee_user_id",
    "managing_branch_id",
    "assigned_by",
    "assigned_at",
    "ended_by",
    "ended_at",
    "current",
}


@pytest.fixture(autouse=True)
def _fresh_pool(fresh_db):
    """The schema is recreated for every test: start each one with new pooled connections, so no prepared statement
    cached against the previous schema is reused (psycopg: 'cached plan must not change result type')."""
    engine.dispose()
    yield


# ================================ helpers ============================================================
def key(prefix="asg"):
    return f"{prefix}-key-{next(KEYS):08d}"


def assign(client, hdr, w, assignee_id, expect=200, k=None, **extra):
    r = client.post(
        f"{L}/{w.loan['id']}/{ENDPOINT}",
        headers=hdr,
        json={"assignee_user_id": assignee_id, "idempotency_key": k or key()} | extra,
    )
    assert r.status_code == expect, f"assign: {r.status_code} {r.text}"
    return r.json()


def end(client, hdr, w, expect=200, k=None, **extra):
    r = client.post(
        f"{L}/{w.loan['id']}/{ENDPOINT}/end", headers=hdr, json={"idempotency_key": k or key("end")} | extra
    )
    assert r.status_code == expect, f"end: {r.status_code} {r.text}"
    return r.json()


def current(client, hdr, w, expect=200):
    r = client.get(f"{L}/{w.loan['id']}/{ENDPOINT}", headers=hdr)
    assert r.status_code == expect, f"current: {r.status_code} {r.text}"
    return r.json()


def history(client, hdr, w, expect=200):
    r = client.get(f"{L}/{w.loan['id']}/{ENDPOINT}/history", headers=hdr)
    assert r.status_code == expect, f"history: {r.status_code} {r.text}"
    return r.json()


def rows(loan_id=None):
    with SessionLocal() as db:
        sql = (
            "SELECT id, loan_id, assignee_user_id, managing_branch_id, assigned_by, ended_by, ended_at, "
            "idempotency_key, end_idempotency_key FROM credit_collection_assignments"
        )
        if loan_id:
            sql += " WHERE loan_id = :l"
        return [tuple(r) for r in db.execute(text(sql + " ORDER BY id"), {"l": loan_id})]


def set_loan_status(loan_id, status):
    with SessionLocal() as db:
        db.execute(text("UPDATE credit_loans SET status = :s WHERE id = :i"), {"s": status, "i": loan_id})
        db.commit()


def mkuser(client, sink, adm, tenant, email, perms, **scope):
    hdr = user_hdr(client, sink, adm, tenant, email, perms, **scope)
    return hdr, user_id(email)


def audit_count():
    return len(audit("loan.collection_"))


def state():
    return {"rows": len(rows()), "audit": audit_count(), "events": count("security_events")}


def loan_with_same_customer(client, adm, base, tag):
    from types import SimpleNamespace

    w = SimpleNamespace(b=base.b, c=base.c, p=base.p, v=base.v, cash=base.cash)
    d = review(client, adm, w, amount="10000")
    approve(client, adm, d, amount="7000")
    w.f = post(client, adm, d["id"], "formalize")
    w.loan = disburse(client, adm, w, key=f"disb-key-{tag}-0001")
    return w


# ================================ target, lifecycle, cardinality ======================================
def test_assignment_is_per_loan_and_assignable_only_in_active_or_past_due(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    _hdr, a = mkuser(client, sink, adm, tenant_a, "a@x.com", ["collections.read"])
    out = assign(client, adm, w, a)  # an `active` loan that is NOT overdue: allowed (the status is a lifecycle gate)
    assert set(out) == EXPECTED_KEYS | {"replayed"} and out["current"] is True and out["ended_at"] is None
    assert (out["loan_id"], out["assignee_user_id"], out["assigned_by"]) == (w.loan["id"], a, tenant_a["admin_id"])
    assert out["managing_branch_id"] is None and out["replayed"] is False
    set_loan_status(w.loan["id"], "past_due")
    _hdr, b = mkuser(client, sink, adm, tenant_a, "b@x.com", ["collections.read"])
    assert assign(client, adm, w, b)["assignee_user_id"] == b  # past_due: allowed (a reassignment here)
    for status in ("paid", "restructured", "refinanced"):
        set_loan_status(w.loan["id"], status)
        r = client.post(
            f"{L}/{w.loan['id']}/{ENDPOINT}", headers=adm, json={"assignee_user_id": a, "idempotency_key": key()}
        )
        assert r.status_code == 409 and r.json()["error"]["code"] == "loan_not_assignable", status
    assert len(rows()) == 2  # nothing was created for the rejected lifecycle states


def test_two_loans_of_the_same_customer_may_have_different_assignees_one_open_each(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w1 = world10(client, adm, tenant_a)
    w2 = loan_with_same_customer(client, adm, w1, "Dos")
    _h, a = mkuser(client, sink, adm, tenant_a, "a@x.com", ["collections.read"])
    _h, b = mkuser(client, sink, adm, tenant_a, "b@x.com", ["collections.read"])
    assign(client, adm, w1, a)
    assign(client, adm, w2, b)
    assert current(client, adm, w1)["assignment"]["assignee_user_id"] == a
    assert current(client, adm, w2)["assignment"]["assignee_user_id"] == b
    with SessionLocal() as db:
        assert db.execute(text("SELECT count(DISTINCT customer_id) FROM credit_loans")).scalar() == 1
        assert (
            db.execute(text("SELECT count(*) FROM credit_collection_assignments WHERE ended_at IS NULL")).scalar() == 2
        )


# ================================ idempotency, same assignee, reassignment ===========================
def test_assign_replay_conflict_and_the_same_assignee_with_a_new_key(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    _h, a = mkuser(client, sink, adm, tenant_a, "a@x.com", ["collections.read"])
    _h, b = mkuser(client, sink, adm, tenant_a, "b@x.com", ["collections.read"])
    first = assign(client, adm, w, a, k="assign-key-000001")
    after = state()
    again = assign(client, adm, w, a, k="assign-key-000001")  # exact replay
    assert again["replayed"] is True and again["assignment_id"] == first["assignment_id"] and state() == after
    conflict = client.post(
        f"{L}/{w.loan['id']}/{ENDPOINT}",
        headers=adm,
        json={"assignee_user_id": b, "idempotency_key": "assign-key-000001"},
    )
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
    same = client.post(  # the current assignee again with a NEW key: never a silent no-op
        f"{L}/{w.loan['id']}/{ENDPOINT}",
        headers=adm,
        json={"assignee_user_id": a, "idempotency_key": "assign-key-000002"},
    )
    assert same.status_code == 409 and same.json()["error"]["code"] == "already_assigned"
    assert state() == after  # no row, no audit, no hidden key
    assert not [r for r in rows() if "assign-key-000002" in (r[7], r[8])]


def test_reassignment_closes_the_old_row_inserts_a_new_one_and_keeps_the_history(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    _h, a = mkuser(client, sink, adm, tenant_a, "a@x.com", ["collections.read"])
    _h, b = mkuser(client, sink, adm, tenant_a, "b@x.com", ["collections.read"])
    t0 = datetime.now(UTC)
    monkeypatch.setattr(assignment_service, "now_utc", lambda: t0 + timedelta(seconds=1))
    first = assign(client, adm, w, a, k="assign-key-A00001")
    monkeypatch.setattr(assignment_service, "now_utc", lambda: t0 + timedelta(seconds=2))
    second = assign(client, adm, w, b, k="assign-key-B00001")  # A -> B
    monkeypatch.setattr(assignment_service, "now_utc", lambda: t0 + timedelta(seconds=3))
    third = assign(client, adm, w, a, k="assign-key-A00002")  # B -> A again: a NEW row, never a reopened one
    ids = [first["assignment_id"], second["assignment_id"], third["assignment_id"]]
    assert len(set(ids)) == 3
    h = history(client, adm, w)["assignments"]
    assert [x["assignment_id"] for x in h] == ids  # assigned_at ASC, id ASC
    assert [x["current"] for x in h] == [False, False, True]
    assert [x["assignee_user_id"] for x in h] == [a, b, a]  # nobody was overwritten
    assert (
        h[0]["ended_at"] == h[1]["assigned_at"] and h[1]["ended_at"] == h[2]["assigned_at"]
    )  # closed AT the reassignment
    assert all(x["ended_by"] == tenant_a["admin_id"] for x in h[:2]) and h[2]["ended_by"] is None
    assert current(client, adm, w)["assignment"]["assignment_id"] == ids[2]
    after = state()
    replay = assign(client, adm, w, b, k="assign-key-B00001")  # exact retry of the reassignment to B
    assert replay["replayed"] is True and replay["assignment_id"] == ids[1] and state() == after  # no second row
    kinds = [e.event_type for e in audit("loan.collection_")]
    assert kinds == ["loan.collection_assigned", "loan.collection_reassigned", "loan.collection_reassigned"]
    d = audit("loan.collection_reassigned")[0].details
    assert (d["assignment_id"], d["previous_assignment_id"], d["previous_assignee_user_id"], d["assignee_user_id"]) == (
        ids[1],
        ids[0],
        a,
        b,
    )


def test_end_closes_without_deleting_replays_and_never_hides_a_stale_command(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    _h, a = mkuser(client, sink, adm, tenant_a, "a@x.com", ["collections.read"])
    assigned = assign(client, adm, w, a)
    out = end(client, adm, w, k="end-key-0000001")
    assert out["current"] is False and out["ended_by"] == tenant_a["admin_id"] and out["ended_at"] is not None
    assert out["assignment_id"] == assigned["assignment_id"] and len(rows()) == 1  # the row is kept
    assert current(client, adm, w) == {"loan_id": w.loan["id"], "assignment": None}  # unassigned: 200 + null
    after = state()
    replay = end(client, adm, w, k="end-key-0000001")  # exact retry
    assert replay["replayed"] is True and replay["assignment_id"] == out["assignment_id"] and state() == after
    stale = client.post(f"{L}/{w.loan['id']}/{ENDPOINT}/end", headers=adm, json={"idempotency_key": "end-key-0000002"})
    assert (
        stale.status_code == 409 and stale.json()["error"]["code"] == "no_active_assignment"
    )  # a NEW key: not success
    assert state() == after
    w2 = loan_with_same_customer(client, adm, w, "Dos")  # the same end key on ANOTHER loan: conflict, never a replay
    assign(client, adm, w2, a)
    r = client.post(f"{L}/{w2.loan['id']}/{ENDPOINT}/end", headers=adm, json={"idempotency_key": "end-key-0000001"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_conflict"
    assert current(client, adm, w2)["assignment"] is not None  # untouched
    # an assign key reused as an end key (another operation = another digest) conflicts too
    assign(client, adm, w, a, k="assign-key-reuse01")
    r = client.post(f"{L}/{w.loan['id']}/{ENDPOINT}/end", headers=adm, json={"idempotency_key": "assign-key-reuse01"})
    assert r.status_code == 200  # a different namespace: it is a brand new END key, valid
    set_loan_status(w.loan["id"], "paid")  # closing must stay possible in any lifecycle state
    assign_state = rows(w.loan["id"])
    assert all(x[6] is not None for x in assign_state)
    assert [e.event_type for e in audit("loan.collection_assignment_ended")] == ["loan.collection_assignment_ended"] * 2


def test_end_is_allowed_when_the_loan_is_already_paid(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    _h, a = mkuser(client, sink, adm, tenant_a, "a@x.com", ["collections.read"])
    assign(client, adm, w, a)
    set_loan_status(w.loan["id"], "paid")
    assert end(client, adm, w)["current"] is False


# ================================ races (real PostgreSQL) =============================================
def test_assign_and_reassign_races_leave_exactly_one_open_row(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    users = [mkuser(client, sink, adm, tenant_a, f"u{i}@x.com", ["collections.read"])[1] for i in range(4)]
    lock = tracked_connect()
    lock.execute(text("SELECT id FROM credit_loans WHERE id = :i FOR UPDATE"), {"i": w.loan["id"]})
    ts = [
        in_thread(
            lambda u=u: client.post(
                f"{L}/{w.loan['id']}/{ENDPOINT}", headers=adm, json={"assignee_user_id": u, "idempotency_key": key()}
            )
        )
        for u in users[:3]
    ]
    wait_blocked_n(LOAN_LOCK, 3)  # all queue behind the loan row: the first step of the lock order
    lock.rollback()
    lock.close()
    for t, _ in ts:
        t.join(60)
    assert [o["resp"].status_code for _, o in ts] == [200, 200, 200]  # assign, then two reassignments, serialised
    open_rows = [r for r in rows(w.loan["id"]) if r[6] is None]
    assert len(open_rows) == 1 and len(rows(w.loan["id"])) == 3  # one open, two closed: nothing lost
    # the SAME key from two clients at once: one row, one replay
    barrier = threading.Barrier(3)

    def same():
        barrier.wait(10)
        return client.post(
            f"{L}/{w.loan['id']}/{ENDPOINT}",
            headers=adm,
            json={"assignee_user_id": users[3], "idempotency_key": "same-key-0000001"},
        )

    a, b = in_thread(same), in_thread(same)
    barrier.wait(10)
    a[0].join(60), b[0].join(60)
    assert [o["resp"].status_code for o in (a[1], b[1])] == [200, 200]  # one creates, the other is the exact replay
    assert sorted(o["resp"].json()["replayed"] for o in (a[1], b[1])) == [False, True]
    assert len([r for r in rows(w.loan["id"]) if r[6] is None]) == 1


def test_assignment_racing_a_payment_and_a_reversal_is_consistent_and_never_auto_closes(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    _h, a = mkuser(client, sink, adm, tenant_a, "a@x.com", ["collections.read"])
    rows_due = schedule(client, adm, w.loan["id"])
    clock11(monkeypatch, local(date.fromisoformat(rows_due[0]["due_date"]) + timedelta(days=400)))
    total = sum(r["total_due"] for r in rows_due)
    # assignment vs the payment that settles the loan
    lock = tracked_connect()
    lock.execute(text("SELECT id FROM credit_loans WHERE id = :i FOR UPDATE"), {"i": w.loan["id"]})
    a_t = in_thread(
        lambda: client.post(
            f"{L}/{w.loan['id']}/{ENDPOINT}", headers=adm, json={"assignee_user_id": a, "idempotency_key": key()}
        )
    )
    p_t = in_thread(
        lambda: client.post(
            f"{L}/{w.loan['id']}/payments",
            headers=adm,
            json={
                "idempotency_key": "race-pay-key-0001",
                "amount": str(total),
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
    assert p_t[1]["resp"].status_code == 200
    got = a_t[1]["resp"].status_code
    assert got in (200, 409)  # payment first -> loan_not_assignable; assignment first -> created
    with SessionLocal() as db:
        assert db.execute(text("SELECT status FROM credit_loans")).scalar() == "paid"
    open_rows = [r for r in rows() if r[6] is None]
    assert len(open_rows) == (1 if got == 200 else 0)  # the payment never closed an assignment
    payment_id = p_t[1]["resp"].json()["id"]
    # assignment vs the reversal that reopens the debt
    lock = tracked_connect()
    lock.execute(text("SELECT id FROM credit_loans WHERE id = :i FOR UPDATE"), {"i": w.loan["id"]})
    b_users = mkuser(client, sink, adm, tenant_a, "b@x.com", ["collections.read"])[1]
    a2 = in_thread(
        lambda: client.post(
            f"{L}/{w.loan['id']}/{ENDPOINT}", headers=adm, json={"assignee_user_id": b_users, "idempotency_key": key()}
        )
    )
    r2 = in_thread(lambda: post_rev(client, adm, payment_id, w, session=None))
    wait_blocked_n(LOAN_LOCK, 2)
    lock.rollback()
    lock.close()
    a2[0].join(60), r2[0].join(60)
    assert r2[1]["resp"].status_code == 200
    got2 = a2[1]["resp"].status_code
    assert got2 in (200, 409)  # assignment first -> loan still paid -> 409; reversal first -> assignable
    with SessionLocal() as db:
        assert db.execute(text("SELECT status FROM credit_loans")).scalar() in ("active", "past_due")
    assert len([r for r in rows() if r[6] is None]) <= 1  # one open row at most, whatever the order


# ================================ database invariants =================================================
def test_the_database_enforces_history_tenant_safety_and_the_single_open_row(client, sink, tenant_a, tenant_b):
    adm = admin_headers(client, tenant_a)
    b_mgr = mk_branch(client, adm, "MGR")
    w = world10(client, adm, tenant_a, managing=b_mgr)
    _h, a = mkuser(client, sink, adm, tenant_a, "a@x.com", ["collections.read"])
    _h, b = mkuser(client, sink, adm, tenant_a, "b@x.com", ["collections.read"])
    first = assign(client, adm, w, a)
    assign(client, adm, w, b)  # `first` is now closed, the second is open
    base = {"t": tenant_a["tenant_id"], "l": w.loan["id"], "m": b_mgr["id"], "by": tenant_a["admin_id"]}
    insert = (
        "INSERT INTO credit_collection_assignments (tenant_id, loan_id, assignee_user_id, managing_branch_id, assigned_by, "
        "assigned_at, idempotency_key, request_digest) VALUES (:t, :l, :a, :m, :by, now(), :k, 'd')"
    )

    def attempt(sql, params, match):
        with SessionLocal() as db:
            with pytest.raises(DBAPIError, match=match):
                db.execute(text(sql), params)
                db.commit()
            db.rollback()

    open_id = [r for r in rows() if r[6] is None][0][0]
    attempt(
        "UPDATE credit_collection_assignments SET assignee_user_id = :a WHERE id = :i",
        {"a": a, "i": open_id},
        "immutable",
    )
    attempt(
        "UPDATE credit_collection_assignments SET ended_at = NULL, ended_by = NULL, end_idempotency_key = NULL, "
        "end_request_digest = NULL WHERE id = :i",
        {"i": first["assignment_id"]},
        "closed|immutable",
    )  # no reopen
    attempt(
        "UPDATE credit_collection_assignments SET ended_by = :by WHERE id = :i",
        {"by": a, "i": first["assignment_id"]},
        "closed",
    )
    attempt(
        "DELETE FROM credit_collection_assignments WHERE id = :i",
        {"i": first["assignment_id"]},
        "history|cannot be deleted",
    )
    attempt("DELETE FROM credit_collection_assignments", {}, "history|cannot be deleted")
    attempt("UPDATE credit_collection_assignments SET assigned_at = now() WHERE id = :i", {"i": open_id}, "immutable")
    attempt(
        "UPDATE credit_collection_assignments SET idempotency_key = 'other-key-000001' WHERE id = :i",
        {"i": open_id},
        "immutable",
    )
    attempt(  # partial end fields (ended_at without the idempotency pair)
        "UPDATE credit_collection_assignments SET ended_at = now(), ended_by = :by WHERE id = :i",
        {"by": base["by"], "i": open_id},
        "end_fields_together",
    )
    # a second OPEN row for the same loan: the partial UNIQUE is the backstop of the loan lock
    attempt(insert, base | {"a": a, "k": "raw-key-000000001"}, "uq_credit_collection_assignments_open")
    # tenant mismatch: another tenant's user as assignee / assigned_by; the managing-branch snapshot must be the loan's
    with SessionLocal() as db:
        db.execute(
            text(
                "UPDATE credit_collection_assignments SET ended_at = now(), ended_by = :by, end_idempotency_key = 'close-key-000001', end_request_digest = 'd' WHERE id = :i"
            ),
            {"by": base["by"], "i": open_id},
        )
        db.commit()  # the open row is closed legitimately, so the next inserts reach the other checks
    attempt(
        insert,
        base | {"a": tenant_b["admin_id"], "k": "raw-key-000000002"},
        "fk_credit_collection_assignments_assignee",
    )
    attempt(
        insert.replace(":by", str(tenant_b["admin_id"])),
        base | {"a": a, "k": "raw-key-000000003"},
        "fk_credit_collection_assignments_assigned_by",
    )
    attempt(insert, base | {"a": a, "k": "raw-key-000000004", "m": None}, "managing branch must be the loan")
    attempt(
        insert,
        base | {"a": a, "k": "raw-key-000000005", "t": tenant_b["tenant_id"]},
        "fk_credit_collection_assignments|managing branch must be the loan",
    )
    attempt(  # born closed
        insert.replace(
            "assigned_at,", "assigned_at, ended_at, ended_by, end_idempotency_key, end_request_digest,"
        ).replace("now(), :k", "now(), now(), :by, 'e-key-0000000001', 'd', :k"),
        base | {"a": a, "k": "raw-key-000000006"},
        "born open",
    )
    with SessionLocal() as db:  # and a correct insert is accepted (so the checks above were the ones refusing)
        db.execute(text(insert), base | {"a": a, "k": "raw-key-000000007"})
        db.commit()
    assert len([r for r in rows() if r[6] is None]) == 1


# ================================ eligibility and authorization =======================================
def test_assignee_eligibility_uses_modern_rbac_at_command_time(client, sink, tenant_a, tenant_b):
    adm = admin_headers(client, tenant_a)
    b_mgr, b_other = mk_branch(client, adm, "MGR"), mk_branch(client, adm, "OTHER")
    w = world10(client, adm, tenant_a, managing=b_mgr)
    before = state()
    for status in ("pending", "locked", "disabled"):
        _h, u = mkuser(client, sink, adm, tenant_a, f"{status}@x.com", ["collections.read"])
        with SessionLocal() as db:
            db.execute(text("UPDATE users SET status = :s WHERE id = :i"), {"s": status, "i": u})
            db.commit()
        r = client.post(
            f"{L}/{w.loan['id']}/{ENDPOINT}", headers=adm, json={"assignee_user_id": u, "idempotency_key": key()}
        )
        assert r.status_code == 422 and r.json()["error"]["code"] == "assignee_not_eligible", status
    _h, no_read = mkuser(client, sink, adm, tenant_a, "noread@x.com", ["loans.read"])  # no collections.read
    _h, elsewhere = mkuser(
        client, sink, adm, tenant_a, "else@x.com", ["collections.read"], scope="branch", branch_id=b_other["id"]
    )
    for u in (no_read, elsewhere):
        r = client.post(
            f"{L}/{w.loan['id']}/{ENDPOINT}", headers=adm, json={"assignee_user_id": u, "idempotency_key": key()}
        )
        assert r.status_code == 422 and r.json()["error"]["code"] == "assignee_not_eligible"
    foreign = client.post(  # another tenant's user: a 404
        f"{L}/{w.loan['id']}/{ENDPOINT}",
        headers=adm,
        json={"assignee_user_id": tenant_b["admin_id"], "idempotency_key": key()},
    )
    assert foreign.status_code == 404
    legacy = mkuser(client, sink, adm, tenant_a, "legacy@x.com", ["users.read"])[
        1
    ]  # the legacy role string is not eligibility
    with SessionLocal() as db:
        db.execute(text("UPDATE users SET role = 'collector' WHERE id = :i"), {"i": legacy})
        db.commit()
    r = client.post(
        f"{L}/{w.loan['id']}/{ENDPOINT}", headers=adm, json={"assignee_user_id": legacy, "idempotency_key": key()}
    )
    assert r.status_code == 422
    assert state()["rows"] == before["rows"] == 0 and audit_count() == 0  # nothing written, nothing audited
    _h, at_mgr = mkuser(
        client, sink, adm, tenant_a, "mgr@x.com", ["collections.read"], scope="branch", branch_id=b_mgr["id"]
    )
    _h, tenant_wide = mkuser(client, sink, adm, tenant_a, "wide@x.com", ["collections.read"])
    assert assign(client, adm, w, at_mgr)["assignee_user_id"] == at_mgr  # branch grant on the MANAGING branch
    assert assign(client, adm, w, tenant_wide)["assignee_user_id"] == tenant_wide  # tenant-wide grant
    # a loan WITHOUT managing branch needs a tenant-level read on the assignee
    n = loan_with_same_customer(client, adm, w, "Nulo")
    assert n.loan["managing_branch_id"] is None
    r = client.post(
        f"{L}/{n.loan['id']}/{ENDPOINT}", headers=adm, json={"assignee_user_id": at_mgr, "idempotency_key": key()}
    )
    assert r.status_code == 422
    assert assign(client, adm, n, tenant_wide)["managing_branch_id"] is None


def test_authorization_scope_managing_branch_null_rule_and_no_access_from_being_assigned(
    client, sink, tenant_a, tenant_b
):
    adm = admin_headers(client, tenant_a)
    b_mgr, b_other = mk_branch(client, adm, "MGR"), mk_branch(client, adm, "OTHER")
    w = world10(client, adm, tenant_a, managing=b_mgr)
    n = loan_with_same_customer(client, adm, w, "Nulo")  # no managing branch
    _h, assignee = mkuser(client, sink, adm, tenant_a, "assignee@x.com", ["collections.read"])
    can_assign_mgr, _ = mkuser(
        client,
        sink,
        adm,
        tenant_a,
        "am@x.com",
        ["collections.assign", "collections.read"],
        scope="branch",
        branch_id=b_mgr["id"],
    )
    wrong_branch, _ = mkuser(
        client,
        sink,
        adm,
        tenant_a,
        "wb@x.com",
        ["collections.assign", "collections.read"],
        scope="branch",
        branch_id=b_other["id"],
    )
    at_origin, _ = mkuser(
        client,
        sink,
        adm,
        tenant_a,
        "or@x.com",
        ["collections.assign", "collections.read"],
        scope="branch",
        branch_id=w.b["id"],
    )
    tenant_assigner, _ = mkuser(client, sink, adm, tenant_a, "ta@x.com", ["collections.assign", "collections.read"])
    reader_only, _ = mkuser(client, sink, adm, tenant_a, "ro@x.com", ["collections.read"])
    loans_only, _ = mkuser(client, sink, adm, tenant_a, "lo@x.com", ["loans.read", "loans.delinquency.assess"])
    legacy, legacy_id = mkuser(client, sink, adm, tenant_a, "lg@x.com", ["users.read"])
    with SessionLocal() as db:
        db.execute(text("UPDATE users SET role = 'collector' WHERE id = :i"), {"i": legacy_id})
        db.commit()
    body = {"assignee_user_id": assignee, "idempotency_key": "auth-key-0000001"}
    for denied in (
        wrong_branch,
        at_origin,
        reader_only,
        loans_only,
        legacy,
    ):  # origin/disbursement never substitute managing
        r = client.post(f"{L}/{w.loan['id']}/{ENDPOINT}", headers=denied, json=body)
        assert r.status_code == 403, denied
        assert (
            client.post(
                f"{L}/{w.loan['id']}/{ENDPOINT}/end", headers=denied, json={"idempotency_key": key("end")}
            ).status_code
            == 403
        )
    assert client.post(f"{L}/{w.loan['id']}/{ENDPOINT}", json=body).status_code == 401
    assert state()["rows"] == 0
    assert (
        client.post(f"{L}/{w.loan['id']}/{ENDPOINT}", headers=can_assign_mgr, json=body).status_code == 200
    )  # managing branch
    # NULL managing branch: only a tenant-level collections.assign
    assert (
        client.post(
            f"{L}/{n.loan['id']}/{ENDPOINT}", headers=can_assign_mgr, json=body | {"idempotency_key": key()}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{L}/{n.loan['id']}/{ENDPOINT}", headers=tenant_assigner, json=body | {"idempotency_key": key()}
        ).status_code
        == 200
    )
    # reads: collections.read with the SAME boundary; loans.read alone, the legacy role and another branch are not enough
    assert current(client, can_assign_mgr, w)["assignment"]["assignee_user_id"] == assignee
    assert len(history(client, can_assign_mgr, w)["assignments"]) == 1
    for denied in (wrong_branch, loans_only, legacy):
        current(client, denied, w, expect=403)
        history(client, denied, w, expect=403)
    current(client, can_assign_mgr, n, expect=403)  # a branch-scoped reader cannot see a loan without managing branch
    assert current(client, tenant_assigner, n)["assignment"] is not None
    # cross-tenant loan: a safe 404 everywhere; the tenant comes from the session, never from the client
    foreign = admin_headers(client, tenant_b)
    assert (
        client.post(
            f"{L}/{w.loan['id']}/{ENDPOINT}", headers=foreign, json=body | {"idempotency_key": key()}
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{L}/{w.loan['id']}/{ENDPOINT}/end", headers=foreign, json={"idempotency_key": key("end")}
        ).status_code
        == 404
    )
    current(client, foreign, w, expect=404)
    history(client, foreign, w, expect=404)
    for extra in (
        {"tenant_id": 1},
        {"reason": "x"},
        {"assigned_at": "2020-01-01T00:00:00Z"},
        {"ended_at": "2030-01-01T00:00:00Z"},
        {"managing_branch_id": b_other["id"]},
        {"effective_from": "2030-01-01"},
    ):
        r = client.post(f"{L}/{n.loan['id']}/{ENDPOINT}", headers=adm, json=body | {"idempotency_key": key()} | extra)
        assert r.status_code == 422, extra  # no client tenant, reason, backdated / future / scheduled dates, branch
    # being assigned grants NOTHING: the assignee loses collections.read -> no access, and the row is untouched
    assignee_hdr = h(login(client, "assignee@x.com", slug=tenant_a["slug"]))
    assert current(client, assignee_hdr, w)["assignment"]["assignee_user_id"] == assignee
    with SessionLocal() as db:
        db.execute(text("UPDATE user_role_assignments SET revoked_at = now() WHERE user_id = :i"), {"i": assignee})
        db.commit()
    assignee_hdr = h(login(client, "assignee@x.com", slug=tenant_a["slug"]))
    current(client, assignee_hdr, w, expect=403)  # assigned to it, yet no access
    assert [r for r in rows(w.loan["id"]) if r[6] is None] and len(rows(w.loan["id"])) == 1  # not closed, not edited


# ================================ no automation, nothing else changes =================================
def test_payments_reversals_and_assessment_never_touch_assignments(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    _h, a = mkuser(client, sink, adm, tenant_a, "a@x.com", ["collections.read"])
    rows_due = schedule(client, adm, w.loan["id"])
    d1 = datetime.fromisoformat(rows_due[0]["due_date"]).date()
    clock11(monkeypatch, local(d1 + timedelta(days=40)))
    assigned = assign(client, adm, w, a)  # active loan, assigned
    assert client.post(f"{L}/{w.loan['id']}/delinquency/assess", headers=adm).status_code == 200  # -> past_due
    p1 = pay(client, adm, w, rows_due[0]["total_due"] + rows_due[1]["total_due"], origin="field")  # past_due -> active
    snapshot = rows()
    assert [r for r in snapshot if r[6] is None] and snapshot[0][0] == assigned["assignment_id"]  # still open
    clock11(monkeypatch, local(d1 + timedelta(days=400)))
    total_rest = sum(r["total_due"] for r in rows_due[2:])
    p2 = pay(client, adm, w, total_rest, origin="field")  # active/past_due -> paid
    with SessionLocal() as db:
        assert db.execute(text("SELECT status FROM credit_loans")).scalar() == "paid"
    assert rows() == snapshot  # the paid loan keeps its open assignment: only an explicit end closes it
    rev(client, adm, p2["id"], w, session=None)  # the debt reappears
    assert rows() == snapshot and current(client, adm, w)["assignment"]["assignment_id"] == assigned["assignment_id"]
    end(client, adm, w)  # explicit end
    ended = rows()
    pay(client, adm, w, total_rest, origin="field")  # paid again
    rev(client, adm, p1["id"], w, session=None)
    with SessionLocal() as db:
        pid = db.execute(text("SELECT max(id) FROM credit_payments")).scalar()
    rev(client, adm, pid, w, session=None)
    assert rows() == ended  # a reversal never revives a closed row nor creates a new one
    assert current(client, adm, w)["assignment"] is None
    assert [e.event_type for e in audit("loan.collection_")] == [
        "loan.collection_assigned",
        "loan.collection_assignment_ended",
    ]


def test_assignment_changes_no_debt_status_money_worklist_or_legacy_pointer_and_exposes_no_pii(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    d1 = datetime.fromisoformat(schedule(client, adm, w.loan["id"])[0]["due_date"]).date()
    clock11(monkeypatch, local(d1 + timedelta(days=5)))
    assignee_hdr, a = mkuser(client, sink, adm, tenant_a, "a@x.com", ["collections.read"])
    t0 = datetime(2031, 1, 2, 3, 4, 5, tzinfo=UTC)
    monkeypatch.setattr(assignment_service, "now_utc", lambda: t0)
    with SessionLocal() as db:  # a LEGACY customer row with the same id: the old pointer must stay exactly as it is
        db.execute(
            text(
                'INSERT INTO customers (id, full_name, phone, address, "references", version, company_id, created_by_id, '
                "created_at) VALUES (:c, 'Legacy', '', '', CAST('[]' AS json), 1, :t, :u, now())"
            ),
            {"c": w.c["id"], "t": tenant_a["tenant_id"], "u": tenant_a["admin_id"]},
        )
        db.commit()
    with SessionLocal() as db:
        pointers = db.execute(text("SELECT id, assigned_collector_id, route_id FROM customers ORDER BY id")).all()
        stored_status = db.execute(text("SELECT status FROM credit_loans")).scalar()
    assert pointers and pointers[0][1] is None
    worklist_before = wl(client, adm)
    balances_before = client.get(f"{L}/{w.loan['id']}/balances", headers=adm).json()
    legacy_before, money_before, contract_before = {t: count(t) for t in LEGACY}, money_counts(), contractual_rows()
    out = assign(client, adm, w, a)
    assert out["assigned_at"].startswith("2031-01-02T03:04:05")  # generated by the server, not the client
    end(client, adm, w)
    assert wl(client, adm) == worklist_before  # T-011 is unchanged by an assignment (no filter, no extra field)
    assert client.get(f"{L}/{w.loan['id']}/balances", headers=adm).json() == balances_before  # debt / overdue untouched
    with SessionLocal() as db:
        assert db.execute(text("SELECT status FROM credit_loans")).scalar() == stored_status  # loan.status untouched
        assert (
            db.execute(text("SELECT id, assigned_collector_id, route_id FROM customers ORDER BY id")).all() == pointers
        )
    assert (
        {t: count(t) for t in LEGACY} == legacy_before
        and money_counts() == money_before
        and contractual_rows() == contract_before
    )
    # IDs only, no PII, no keys, no digests, no role strings
    raw = json.dumps([out, current(client, adm, w), history(client, adm, w)], default=str)
    for pii in (
        "Juan",
        "Perez",
        "001-0000001-1",
        "@x.com",
        "request_digest",
        "idempotency",
        "collector",
        "sha256",
        "phone",
    ):
        assert pii not in raw, pii
    assert set(history(client, adm, w)["assignments"][0]) == EXPECTED_KEYS
    for e in audit("loan.collection_"):
        assert "Perez" not in str(e.details) and "@" not in str(e.details)
    assert audit_count() == 2
    # worklist filters are NOT part of T-012 (T-013): the endpoint ignores an unknown assignee parameter, never applies it


def test_reads_never_write_and_failed_commands_leave_no_success_audit(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    _h, a = mkuser(client, sink, adm, tenant_a, "a@x.com", ["collections.read"])
    assign(client, adm, w, a)
    before = state()
    statements, stop = write_listener()
    try:
        current(client, adm, w)
        history(client, adm, w)
    finally:
        stop()
    assert statements == [] and state() == before  # 0 INSERT / UPDATE / DELETE, no read audit
    client.post(
        f"{L}/{w.loan['id']}/{ENDPOINT}", headers=adm, json={"assignee_user_id": 999999, "idempotency_key": key()}
    )
    client.post(f"{L}/{w.loan['id']}/{ENDPOINT}/end", headers=adm, json={"idempotency_key": "x"})  # too short: 422
    assert state() == before
    d = audit("loan.collection_assigned")[0].details
    assert {"assignment_id", "loan_id", "assignee_user_id", "managing_branch_id"} <= set(d)


# ================================ migration 0014 ======================================================
def _seed(eng):
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


def test_migration_0014_empty_downgrade_reupgrade_and_alembic_check(scratch_db):
    from sqlalchemy import create_engine

    assert _alembic(scratch_db, "upgrade", "0013").returncode == 0
    eng = create_engine(scratch_db)
    try:
        _seed(eng)
        up = _alembic(scratch_db, "upgrade", "0014")
        assert up.returncode == 0, up.stderr
        with eng.connect() as c:
            assert c.execute(text("SELECT count(*) FROM permissions WHERE code = 'collections.assign'")).scalar() == 1
            assert (
                c.execute(text("SELECT is_sensitive FROM permissions WHERE code = 'collections.assign'")).scalar()
                is True
            )
            assert (
                c.execute(
                    text(
                        "SELECT count(*) FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id WHERE p.code = 'collections.assign'"
                    )
                ).scalar()
                == 1
            )
            assert c.execute(text("SELECT to_regclass('credit_collection_assignments')")).scalar() is not None
            trg = {
                r[0]
                for r in c.execute(
                    text("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'trg_credit_collection_assignments%'")
                )
            }
            assert trg == {"trg_credit_collection_assignments_guard", "trg_credit_collection_assignments_insert_check"}
            uq = {
                r[0] for r in c.execute(text("SELECT conname FROM pg_constraint WHERE conname = 'uq_users_tenant_id'"))
            }
            assert uq == {"uq_users_tenant_id"}  # the only change to identity: the unique target of the tenant-safe FKs
            idx = {
                r[0]
                for r in c.execute(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = 'credit_collection_assignments'")
                )
            }
            assert {"uq_credit_collection_assignments_open", "uq_credit_collection_assignments_end_key"} <= idx
        # (alembic check compares against the models at head, which also hold the T-013 index: it runs after the re-upgrade)
        down = _alembic(scratch_db, "downgrade", "0013")  # no history: a clean downgrade
        assert down.returncode == 0, down.stderr
        with eng.connect() as c:
            assert c.execute(text("SELECT count(*) FROM permissions WHERE code = 'collections.assign'")).scalar() == 0
            assert c.execute(text("SELECT to_regclass('credit_collection_assignments')")).scalar() is None
            assert (
                c.execute(text("SELECT count(*) FROM pg_constraint WHERE conname = 'uq_users_tenant_id'")).scalar() == 0
            )
            assert (
                c.execute(
                    text("SELECT count(*) FROM pg_proc WHERE proname LIKE 'credit_collection_assignments%'")
                ).scalar()
                == 0
            )
        again = _alembic(scratch_db, "upgrade", "head")
        assert again.returncode == 0, again.stderr
        assert _alembic(scratch_db, "check").returncode == 0
    finally:
        eng.dispose()


def test_downgrade_0014_is_refused_before_any_ddl_when_assignment_history_exists(scratch_db):
    from sqlalchemy import create_engine

    assert _alembic(scratch_db, "upgrade", "0014").returncode == 0
    eng = create_engine(scratch_db)
    try:
        _seed(eng)
        with eng.begin() as c:
            c.execute(text("SET session_replication_role = replica"))  # the point is the guard, not a whole loan chain
            c.execute(
                text(
                    "INSERT INTO credit_collection_assignments (tenant_id, loan_id, assignee_user_id, managing_branch_id, "
                    "assigned_by, assigned_at, idempotency_key, request_digest) SELECT id, 1, 1, NULL, 1, now(), "
                    "'downgrade-key-0001', 'd' FROM companies"
                )
            )
        refused = _alembic(scratch_db, "downgrade", "0013")
        assert refused.returncode != 0 and "Cannot downgrade 0014" in refused.stderr
        with eng.connect() as c:  # nothing was dropped, nothing was erased
            assert c.execute(text("SELECT count(*) FROM credit_collection_assignments")).scalar() == 1
            assert c.execute(text("SELECT count(*) FROM permissions WHERE code = 'collections.assign'")).scalar() == 1
            assert (
                c.execute(text("SELECT count(*) FROM pg_constraint WHERE conname = 'uq_users_tenant_id'")).scalar() == 1
            )
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0014"
    finally:
        eng.dispose()


def test_the_assignment_code_touches_no_legacy_money_or_worklist_filter():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    src = (root / "app/modules/loans/assignments.py").read_text(encoding="utf-8")
    import io
    import tokenize

    names = {t.string for t in tokenize.generate_tokens(io.StringIO(src).readline) if t.type == tokenize.NAME}
    for banned in (
        "assigned_collector_id",
        "route_id",
        "UserRole",
        "role",
        "refresh_loan_state",
        "LoanSettings",
        "CreditPayment",
        "CashMovement",
        "cash_port",
        "ledger",
        "allocation",
        "today",
    ):
        assert banned not in names, banned
    worklist = (root / "app/modules/loans/worklist.py").read_text(encoding="utf-8")
    assert "assign" not in {
        t.string for t in tokenize.generate_tokens(io.StringIO(worklist).readline) if t.type == tokenize.NAME
    }
    for module in ("payments.py", "reversals.py", "overdue.py"):  # nothing closes or revives assignments automatically
        module_names = {
            t.string
            for t in tokenize.generate_tokens(
                io.StringIO((root / "app/modules/loans" / module).read_text(encoding="utf-8")).readline
            )
            if t.type == tokenize.NAME
        }
        assert not [n for n in module_names if "assign" in n.lower()], module
