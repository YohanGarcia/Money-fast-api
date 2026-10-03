"""T-014 Credit Collection Activity tests (T014-*). PostgreSQL only.

One management (call, visit, ...) recorded on ONE loan as append-only history: closed type enum, no outcome / free text /
contact data / client time, server-side snapshots of the managing branch and of the open T-012 assignment (checked by an INSERT
trigger), ``collections.actions.create`` to write and ``collections.read`` to read (both on the managing branch), idempotent
commands, audit without sensitive data, no money / status / assignment / worklist / legacy effect. No promise, follow-up, GPS,
messaging or attachment.
"""

import base64
import io
import json
import re
import threading
import tokenize
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from app.core.db import SessionLocal, engine
from app.modules.loans import activities as activity_service
from tests import pg_env  # noqa: F401  (must precede app imports)
from tests.test_t001_foundation import _alembic, scratch_db  # noqa: F401
from tests.test_t002_identity import (  # noqa: F401  (fixtures + helpers shared with the earlier suites)
    V2,
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
    tracked_connect,
    wait_blocked_n,
)
from tests.test_t008_payments import LEGACY, LOAN_LOCK, schedule
from tests.test_t009_payment_reversal import audit
from tests.test_t010_overdue_projection import contractual_rows, money_counts, world10, write_listener
from tests.test_t011_collection_worklist import clock, first_due, local, mkloan, wl
from tests.test_t012_collection_assignment import (
    _fresh_pool,  # noqa: F401  (autouse: new pooled connections for every recreated schema)
    _seed,
    assign,
    end,
    mkuser,
    rows,
    set_loan_status,
)

ROOT = Path(__file__).resolve().parent.parent
L = f"{V2}/loans"
ENDPOINT = "collection-activities"
KEYS = iter(range(1, 10**6))
TYPES = ("phone_call", "whatsapp", "sms", "email", "in_person_visit", "office_visit", "no_contact", "other")
OUT_KEYS = {
    "activity_id",
    "loan_id",
    "managing_branch_id",
    "recorded_by",
    "assignment_id",
    "activity_type",
    "created_at",
}
CREATE = "collections.actions.create"


# ================================ helpers ============================================================
def key(prefix="act"):
    return f"{prefix}-key-{next(KEYS):08d}"


def act(client, hdr, w, activity_type="phone_call", expect=200, k=None, **extra):
    r = client.post(
        f"{L}/{w.loan['id']}/{ENDPOINT}",
        headers=hdr,
        json={"activity_type": activity_type, "idempotency_key": k or key()} | extra,
    )
    assert r.status_code == expect, f"activity: {r.status_code} {r.text}"
    return r.json()


def lst(client, hdr, w, expect=200, **params):
    r = client.get(f"{L}/{w.loan['id']}/{ENDPOINT}", headers=hdr, params=params)
    assert r.status_code == expect, f"list: {r.status_code} {r.text}"
    return r.json()


def detail(client, hdr, w, activity_id, expect=200):
    r = client.get(f"{L}/{w.loan['id']}/{ENDPOINT}/{activity_id}", headers=hdr)
    assert r.status_code == expect, f"detail: {r.status_code} {r.text}"
    return r.json()


def arows(loan_id=None):
    with SessionLocal() as db:
        sql = (
            "SELECT id, loan_id, managing_branch_id, recorded_by, assignment_id, activity_type, created_at, "
            "idempotency_key FROM credit_collection_activities"
        )
        if loan_id:
            sql += " WHERE loan_id = :l"
        return [tuple(r) for r in db.execute(text(sql + " ORDER BY id"), {"l": loan_id})]


def lid(w):
    return w.loan["id"]


def state():
    return {"rows": len(arows()), "audit": len(audit("loan.collection_activity")), "events": count("security_events")}


def revoke(user_id, code):
    with SessionLocal() as db:
        db.execute(
            text(
                "DELETE FROM role_permissions WHERE role_id IN (SELECT role_id FROM user_role_assignments "
                "WHERE user_id = :u) AND permission_id = (SELECT id FROM permissions WHERE code = :c)"
            ),
            {"u": user_id, "c": code},
        )
        db.commit()


# ================================ creation, types, closed input ========================================
def test_every_approved_type_is_recorded_by_the_authenticated_actor_with_server_time_only(
    client, sink, tenant_a
):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    stored_status = _status(lid(w))
    for t in TYPES:  # phone_call, whatsapp, sms, email, in_person_visit, office_visit, no_contact, other
        out = act(client, adm, w, t)
        assert set(out) == OUT_KEYS | {"replayed"} and out["replayed"] is False
        assert (out["loan_id"], out["activity_type"], out["recorded_by"]) == (lid(w), t, tenant_a["admin_id"])
        assert out["managing_branch_id"] is None and out["assignment_id"] is None  # no branch, no open assignment
    assert [r[5] for r in arows()] == list(TYPES) and all(r[3] == tenant_a["admin_id"] for r in arows())
    assert _status(lid(w)) == stored_status
    # the database also guards the enum
    with SessionLocal() as db:
        with pytest.raises(DBAPIError, match="activity_type_valid"):
            db.execute(
                text(
                    "INSERT INTO credit_collection_activities (tenant_id, loan_id, managing_branch_id, recorded_by, "
                    "assignment_id, activity_type, created_at, idempotency_key, request_digest) "
                    "VALUES (:t, :l, NULL, :u, NULL, 'promise_created', now(), 'db-enum-key-0001', 'd')"
                ),
                {"t": tenant_a["tenant_id"], "l": lid(w), "u": tenant_a["admin_id"]},
            )
            db.commit()
        db.rollback()


def _status(loan_id):
    with SessionLocal() as db:
        return db.execute(text("SELECT status FROM credit_loans WHERE id = :i"), {"i": loan_id}).scalar()


def test_unapproved_types_and_every_unknown_field_are_a_422_that_writes_nothing(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    before = state()
    for bad in ("promise_created", "promise_broken", "payment_received", "note", "visit", "PHONE_CALL", "", None):
        r = client.post(
            f"{L}/{lid(w)}/{ENDPOINT}", headers=adm, json={"activity_type": bad, "idempotency_key": key()}
        )
        assert r.status_code == 422, bad
    for field, value in (
        ("outcome", "contacted"),
        ("observation", "x"),
        ("note", "x"),
        ("notes", "x"),
        ("comment", "x"),
        ("description", "x"),
        ("free_text", "x"),
        ("occurred_at", "2026-01-01T00:00:00Z"),
        ("activity_date", "2026-01-01"),
        ("business_date", "2026-01-01"),
        ("scheduled_at", "2999-01-01T00:00:00Z"),
        ("created_at", "2026-01-01T00:00:00Z"),
        ("managing_branch_id", 1),
        ("assignment_id", 1),
        ("recorded_by", 1),
        ("performed_by", 1),
        ("tenant_id", 1),
        ("phone", "809"),
        ("promised_amount", "10"),
        ("latitude", 1.0),
    ):
        act(client, adm, w, expect=422, **{field: value})
    r = client.post(f"{L}/{lid(w)}/{ENDPOINT}", headers=adm, json={"activity_type": "other"})
    assert r.status_code == 422  # the idempotency key is mandatory
    assert state() == before  # not a row, not an audit event


# ================================ idempotency =========================================================
def test_replay_conflict_and_authorization_before_replay(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    other = mkloan(client, adm, tenant_a, "Otro", base=w)
    first = act(client, adm, w, "phone_call", k="replay-key-000001")
    before = state()
    again = act(client, adm, w, "phone_call", k="replay-key-000001")
    assert again == {**first, "replayed": True} | {"replayed": True} and again["activity_id"] == first["activity_id"]
    assert state() == before  # replay: 0 INSERT, 0 audit
    for body_type, loan in (("sms", w), ("phone_call", other)):  # other type / other loan: 409
        r = client.post(
            f"{L}/{lid(loan)}/{ENDPOINT}",
            headers=adm,
            json={"activity_type": body_type, "idempotency_key": "replay-key-000001"},
        )
        assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_conflict", (body_type, lid(loan))
    assert state() == before
    # a replay is not an access bypass: the permission is checked again first
    hdr, uid = mkuser(client, sink, adm, tenant_a, "w@x.com", [CREATE])
    mine = act(client, hdr, w, "email", k="replay-key-000002")
    revoke(uid, CREATE)
    act(client, hdr, w, "email", expect=403, k="replay-key-000002")
    assert detail(client, adm, w, mine["activity_id"])["activity_type"] == "email"


def test_concurrent_requests_with_the_same_key_create_a_single_activity(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    barrier = threading.Barrier(3)

    def same():
        barrier.wait(10)
        return client.post(
            f"{L}/{lid(w)}/{ENDPOINT}",
            headers=adm,
            json={"activity_type": "whatsapp", "idempotency_key": "same-key-0000001"},
        )

    a, b = in_thread(same), in_thread(same)
    barrier.wait(10)
    a[0].join(60), b[0].join(60)
    assert [o["resp"].status_code for o in (a[1], b[1])] == [200, 200]  # no 500
    assert sorted(o["resp"].json()["replayed"] for o in (a[1], b[1])) == [False, True]
    assert len(arows(lid(w))) == 1 and len(audit("loan.collection_activity")) == 1


# ================================ authorization =======================================================
def test_write_needs_actions_create_on_the_managing_branch_and_nothing_else_substitutes_it(
    client, sink, tenant_a, tenant_b
):
    adm = admin_headers(client, tenant_a)
    b_mgr, b_other = mk_branch(client, adm, "MGR"), mk_branch(client, adm, "OTHER")
    w = world10(client, adm, tenant_a, managing=b_mgr)
    n = mkloan(client, adm, tenant_a, "Nulo", base=w)  # no managing branch
    at_mgr, _ = mkuser(client, sink, adm, tenant_a, "m@x.com", [CREATE], scope="branch", branch_id=b_mgr["id"])
    wrong, _ = mkuser(client, sink, adm, tenant_a, "w@x.com", [CREATE], scope="branch", branch_id=b_other["id"])
    origin, _ = mkuser(client, sink, adm, tenant_a, "o@x.com", [CREATE], scope="branch", branch_id=w.b["id"])
    tenant_wide, _ = mkuser(client, sink, adm, tenant_a, "t@x.com", [CREATE])
    assign_only, _ = mkuser(client, sink, adm, tenant_a, "as@x.com", ["collections.assign"])
    read_only, _ = mkuser(client, sink, adm, tenant_a, "ro@x.com", ["collections.read"])
    loans_only, _ = mkuser(client, sink, adm, tenant_a, "lo@x.com", ["loans.read", "loans.delinquency.assess"])
    legacy, legacy_id = mkuser(client, sink, adm, tenant_a, "lg@x.com", ["users.read"])
    with SessionLocal() as db:
        db.execute(text("UPDATE users SET role = 'collector' WHERE id = :i"), {"i": legacy_id})
        db.commit()
    assert act(client, at_mgr, w)["managing_branch_id"] == b_mgr["id"]  # the snapshot of the loan's managing branch
    act(client, tenant_wide, w)
    act(client, tenant_wide, n)  # NULL managing branch: tenant-level create
    for denied in (wrong, origin, assign_only, read_only, loans_only, legacy):
        act(client, denied, w, expect=403)
    act(client, at_mgr, n, expect=403)  # a branch-level create is never enough for a loan without managing branch
    adm_b = admin_headers(client, tenant_b)
    act(client, adm_b, w, expect=404)  # another tenant's loan is a safe 404
    r = client.post(f"{L}/999999/{ENDPOINT}", headers=adm, json={"activity_type": "other", "idempotency_key": key()})
    assert r.status_code == 404
    # writing needs no collections.read, and writing grants no read
    assert len(arows(lid(w))) == 2
    lst(client, tenant_wide, w, expect=403)
    # the permission is sensitive and reaches the tenant admin, not the legacy collector role
    with SessionLocal() as db:
        sensitive = db.execute(text("SELECT is_sensitive FROM permissions WHERE code = :c"), {"c": CREATE}).scalar()
        legacy_has = db.execute(
            text(
                "SELECT count(*) FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id "
                "JOIN roles r ON r.id = rp.role_id WHERE p.code = :c AND r.name ILIKE '%collector%'"
            ),
            {"c": CREATE},
        ).scalar()
    assert sensitive is True and legacy_has == 0


def test_read_needs_collections_read_and_neither_the_recorder_nor_the_assignee_gets_it(
    client, sink, tenant_a, tenant_b
):
    adm = admin_headers(client, tenant_a)
    b_mgr, b_other = mk_branch(client, adm, "MGR"), mk_branch(client, adm, "OTHER")
    w = world10(client, adm, tenant_a, managing=b_mgr)
    n = mkloan(client, adm, tenant_a, "Nulo", base=w)
    rec, rec_id = mkuser(client, sink, adm, tenant_a, "rec@x.com", [CREATE, "collections.read"])
    mine = act(client, rec, w)["activity_id"]
    n_id = act(client, adm, n)["activity_id"]
    at_mgr, _ = mkuser(client, sink, adm, tenant_a, "m@x.com", ["collections.read"], scope="branch", branch_id=b_mgr["id"])
    wrong, _ = mkuser(client, sink, adm, tenant_a, "w@x.com", ["collections.read"], scope="branch", branch_id=b_other["id"])
    assert [i["activity_id"] for i in lst(client, at_mgr, w)["items"]] == [mine]
    assert detail(client, at_mgr, w, mine)["recorded_by"] == rec_id
    lst(client, wrong, w, expect=403)
    detail(client, wrong, w, mine, expect=403)
    lst(client, at_mgr, n, expect=403)  # NULL managing branch: tenant-level read only
    assert detail(client, adm, n, n_id)["managing_branch_id"] is None
    # recording grants no reading; being the snapshot assignee neither
    only_create, uid = mkuser(client, sink, adm, tenant_a, "oc@x.com", [CREATE])
    act(client, only_create, w)
    lst(client, only_create, w, expect=403)
    detail(client, only_create, w, mine, expect=403)
    assert assign(client, adm, w, rec_id)["assignee_user_id"] == rec_id
    act(client, adm, w)
    revoke(rec_id, "collections.read")
    lst(client, rec, w, expect=403)
    # other tenant / other loan / missing: the same safe 404
    adm_b = admin_headers(client, tenant_b)
    lst(client, adm_b, w, expect=404)
    detail(client, adm_b, w, mine, expect=404)
    detail(client, adm, n, mine, expect=404)  # an activity of another loan
    detail(client, adm, w, 999999, expect=404)
    assert client.get(f"{L}/{lid(w)}/{ENDPOINT}").status_code == 401


# ================================ assignment snapshot =================================================
def test_the_assignment_is_a_stable_snapshot_that_neither_authorizes_nor_is_required(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    _h1, u1 = mkuser(client, sink, adm, tenant_a, "u1@x.com", ["collections.read"])
    _h2, u2 = mkuser(client, sink, adm, tenant_a, "u2@x.com", ["collections.read"])
    writer, _ = mkuser(client, sink, adm, tenant_a, "wr@x.com", [CREATE])  # not the assignee, still allowed
    first = act(client, writer, w)
    assert first["assignment_id"] is None  # no open assignment -> NULL (never rejected)
    a1 = assign(client, adm, w, u1)["assignment_id"]
    nobody, _ = mkuser(client, sink, adm, tenant_a, "nb@x.com", ["collections.read"])
    act(client, nobody, w, expect=403)  # an assigned loan opens no door: the permission still decides
    act(client, _h1, w, expect=403)  # not even for the assignee itself (it holds only collections.read)
    second = act(client, writer, w)
    assert second["assignment_id"] == a1  # the exact open assignment
    with SessionLocal() as db:  # a stale assignee (disabled) is still the open assignment: it is snapshotted
        db.execute(text("UPDATE users SET status = 'disabled' WHERE id = :u"), {"u": u1})
        db.commit()
    assert act(client, writer, w)["assignment_id"] == a1
    a2 = assign(client, adm, w, u2)["assignment_id"]  # reassign: the old snapshots do not move
    assert [r[4] for r in arows(lid(w))] == [None, a1, a1]
    assert act(client, writer, w)["assignment_id"] == a2
    end(client, adm, w)
    assert [r[4] for r in arows(lid(w))] == [None, a1, a1, a2]  # end does not touch them either
    assert act(client, writer, w)["assignment_id"] is None
    assert len(rows(lid(w))) == 2 and all(r[6] is not None for r in rows(lid(w)))  # activities never write assignments


def test_an_activity_racing_a_reassignment_snapshots_an_assignment_that_was_open_at_that_instant(
    client, sink, tenant_a
):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    _h1, u1 = mkuser(client, sink, adm, tenant_a, "u1@x.com", ["collections.read"])
    _h2, u2 = mkuser(client, sink, adm, tenant_a, "u2@x.com", ["collections.read"])
    old = assign(client, adm, w, u1)["assignment_id"]
    lock = tracked_connect()
    lock.execute(text("SELECT id FROM credit_loans WHERE id = :i FOR UPDATE"), {"i": lid(w)})
    t_act = in_thread(lambda: client.post(f"{L}/{lid(w)}/{ENDPOINT}", headers=adm, json={"activity_type": "other", "idempotency_key": key()}))
    t_re = in_thread(
        lambda: client.post(f"{V2}/loans/{lid(w)}/collection-assignment", headers=adm, json={"assignee_user_id": u2, "idempotency_key": key()})
    )
    wait_blocked_n(LOAN_LOCK, 2)  # both queue behind the loan row: loan first for activity and assignment alike
    lock.rollback()
    lock.close()
    for t, _ in (t_act, t_re):
        t.join(60)
    assert t_act[1]["resp"].status_code == 200 and t_re[1]["resp"].status_code == 200
    (activity,) = arows(lid(w))
    snapshot, created_at = activity[4], activity[6]
    by_id = {r[0]: r for r in rows(lid(w))}
    new = next(i for i in by_id if i != old)
    assert snapshot in (old, new)  # either serial order is valid, never anything else
    with SessionLocal() as db:
        a = db.execute(
            text("SELECT assigned_at, ended_at FROM credit_collection_assignments WHERE id = :i"), {"i": snapshot}
        ).one()
    assert a.assigned_at <= created_at and (a.ended_at is None or created_at <= a.ended_at)


# ================================ database guards =====================================================
def test_the_database_enforces_append_only_tenant_safety_and_the_snapshots(client, sink, tenant_a, tenant_b):
    adm = admin_headers(client, tenant_a)
    b_mgr = mk_branch(client, adm, "MGR")
    w = world10(client, adm, tenant_a, managing=b_mgr)
    _h, u1 = mkuser(client, sink, adm, tenant_a, "u1@x.com", ["collections.read"])
    admin_b = admin_headers(client, tenant_b)
    activity = act(client, adm, w)
    assert activity["managing_branch_id"] == b_mgr["id"]
    assign(client, adm, w, u1)
    insert = (
        "INSERT INTO credit_collection_activities (tenant_id, loan_id, managing_branch_id, recorded_by, assignment_id, "
        "activity_type, created_at, idempotency_key, request_digest) VALUES (:t, :l, :m, :u, :a, 'other', now(), :k, 'd')"
    )
    open_assignment = [r for r in rows(lid(w)) if r[6] is None][0][0]
    ok = {"t": tenant_a["tenant_id"], "l": lid(w), "m": b_mgr["id"], "u": tenant_a["admin_id"], "a": open_assignment}

    def attempt(sql, params, match):
        with SessionLocal() as db:
            with pytest.raises(DBAPIError, match=match):
                db.execute(text(sql), params)
                db.commit()
            db.rollback()

    attempt(insert, ok | {"m": None, "k": "db-key-00000001"}, "managing branch")  # wrong branch snapshot
    attempt(insert, ok | {"a": None, "k": "db-key-00000002"}, "assignment snapshot")  # the open one is missing
    attempt(insert, ok | {"a": open_assignment + 999, "k": "db-key-00000003"}, "assignment snapshot")  # wrong one
    attempt(insert, ok | {"u": tenant_b["admin_id"], "k": "db-key-00000004"}, "fk_credit_collection_activities_actor")
    attempt(
        insert, ok | {"t": tenant_b["tenant_id"], "m": None, "a": None, "k": "db-key-00000005"}, "fk_credit_collection_activities_loan"
    )  # (the loan pair does not exist: the trigger sees NULL = NULL, the tenant-safe FK rejects it)
    attempt(insert, ok | {"k": activity_key(activity)}, "uq_credit_collection_activities_tenant_key")
    for column, value in (
        ("activity_type", "'sms'"),
        ("assignment_id", "NULL"),
        ("managing_branch_id", "NULL"),
        ("recorded_by", str(u1)),
        ("created_at", "now()"),
        ("idempotency_key", "'changed-key-0001'"),
        ("request_digest", "'changed'"),
        ("loan_id", "loan_id"),
    ):
        attempt(
            f"UPDATE credit_collection_activities SET {column} = {value} WHERE id = :i",
            {"i": activity["activity_id"]},
            "append-only",
        )
    attempt("DELETE FROM credit_collection_activities WHERE id = :i", {"i": activity["activity_id"]}, "append-only")
    attempt("UPDATE credit_collection_activities SET id = id", {}, "append-only")
    # the tenant-safe constraints exist (branch and assignment snapshots are also covered by the insert trigger)
    with SessionLocal() as db:
        fks = {
            r[0]: r[1]
            for r in db.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'credit_collection_activities'::regclass AND contype = 'f'"
                )
            )
        }
    assert "(tenant_id, managing_branch_id)" in fks["fk_credit_collection_activities_branch"]
    assert "(tenant_id, assignment_id, loan_id)" in fks["fk_credit_collection_activities_assignment"]
    assert admin_b  # (tenant B exists: the foreign ids above are real rows)
    assert len(arows()) == 1


def activity_key(activity):
    with SessionLocal() as db:
        return db.execute(
            text("SELECT idempotency_key FROM credit_collection_activities WHERE id = :i"), {"i": activity["activity_id"]}
        ).scalar()


# ================================ reads: order, keyset, purity ========================================
def test_list_is_newest_first_keyset_paginated_and_every_read_is_pure(client, sink, tenant_a, tenant_b):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    made = [act(client, adm, w, TYPES[i % len(TYPES)])["activity_id"] for i in range(7)]
    other = mkloan(client, adm, tenant_a, "Otro", base=w)
    act(client, adm, other)  # another loan's activity never shows up
    full = lst(client, adm, w, limit=100)
    assert [i["activity_id"] for i in full["items"]] == sorted(made, reverse=True) and full["next_cursor"] is None
    assert full["limit"] == 100 and lst(client, adm, w)["limit"] == 50
    seen, cursor = [], None
    while True:
        page = lst(client, adm, w, limit=3, **({"cursor": cursor} if cursor else {}))
        seen += [i["activity_id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == sorted(made, reverse=True) and len(set(seen)) == 7  # no duplicate, no omission
    for bad in (
        "nope",
        base64.urlsafe_b64encode(b"[1]").decode(),
        base64.urlsafe_b64encode(b'{"i":"7"}').decode(),
        base64.urlsafe_b64encode(b'{"i":-1}').decode(),
        base64.urlsafe_b64encode(b'{"i":true}').decode(),
        base64.urlsafe_b64encode(b'{"i":1,"x":2}').decode(),
        base64.urlsafe_b64encode(b'{"x":2}').decode(),
    ):
        r = client.get(f"{L}/{lid(w)}/{ENDPOINT}", headers=adm, params={"cursor": bad})
        assert r.status_code == 422, bad
    assert lst(client, adm, w, expect=422, limit=0) is not None and lst(client, adm, w, expect=422, limit=101) is not None
    # there is no public OFFSET: the parameter is ignored
    assert lst(client, adm, w, offset=3, limit=100) == full
    # a cursor never widens the scope: another tenant is a 404 whatever it carries
    lst(client, admin_headers(client, tenant_b), w, expect=404, limit=3, cursor=page_cursor(client, adm, w))
    # pure reads: 0 writes, 0 audit, no PII, no key, no digest
    before = state()
    statements, stop = write_listener()
    try:
        lst(client, adm, w, limit=2)
        detail(client, adm, w, made[0])
    finally:
        stop()
    assert statements == [] and state() == before
    raw = json.dumps([full, detail(client, adm, w, made[0])], default=str)
    for leak in ("idempotency", "request_digest", "sha256", "Juan", "Perez", "@", "phone_number", "address", "-key-"):
        assert leak not in raw, leak
    assert set(full["items"][0]) == OUT_KEYS


def page_cursor(client, adm, w):
    return lst(client, adm, w, limit=3)["next_cursor"]


# ================================ lifecycle, audit, no side effects ===================================
def test_any_loan_status_accepts_an_activity_and_it_changes_nothing_else(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    first = act(client, adm, w)  # active and not overdue: no overdue requirement
    _h, u1 = mkuser(client, sink, adm, tenant_a, "u1@x.com", ["collections.read"])
    assign(client, adm, w, u1)
    clock(monkeypatch, local(first_due(client, adm, w) + timedelta(days=5)))
    worklist_before = wl(client, adm, limit=100)
    balances_before = client.get(f"{L}/{lid(w)}/balances", headers=adm).json()
    stored = _status(lid(w))
    legacy_before, money_before, contract_before = {t: count(t) for t in LEGACY}, money_counts(), contractual_rows()
    assignments_before, loan_rows = rows(), _loan_rows()
    for status in ("past_due", "paid", "active", "restructured", "refinanced"):
        set_loan_status(lid(w), status)
        out = act(client, adm, w, "in_person_visit")
        assert out["loan_id"] == lid(w), status
        assert _status(lid(w)) == status  # the activity never writes the loan status
    set_loan_status(lid(w), stored)
    assert len(arows()) == 6 and first["activity_id"] == arows()[0][0]
    assert wl(client, adm, limit=100) == worklist_before  # T-013 / T-011 untouched (no activity field, no filter)
    assert client.get(f"{L}/{lid(w)}/balances", headers=adm).json() == balances_before
    assert rows() == assignments_before and _loan_rows() == loan_rows
    assert {t: count(t) for t in LEGACY} == legacy_before and money_counts() == money_before
    assert contractual_rows() == contract_before
    assert schedule(client, adm, lid(w))  # the schedule is still there and unchanged in count


def _loan_rows():
    with SessionLocal() as db:
        return [tuple(r) for r in db.execute(text("SELECT to_jsonb(l)::text FROM credit_loans l ORDER BY id"))]


def test_audit_carries_ids_and_the_type_only_and_replays_or_failures_add_none(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    out = act(client, adm, w, "sms", k="audit-key-0000001")
    act(client, adm, w, "sms", k="audit-key-0000001")  # replay
    act(client, adm, w, "sms", k="audit-key-0000001", expect=200)
    act(client, adm, w, "email", k="audit-key-0000001", expect=409)  # conflict
    act(client, adm, w, "other", expect=422, outcome="x")  # invalid
    events = audit("loan.collection_activity")
    assert len(events) == 1 and events[0].event_type == "loan.collection_activity_created"
    d = events[0].details
    assert d["activity_id"] == out["activity_id"] and d["loan_id"] == lid(w) and d["activity_type"] == "sms"
    assert d["recorded_by"] == tenant_a["admin_id"] and d["assignment_id"] is None and d["managing_branch_id"] is None
    raw = json.dumps(d)
    for leak in ("audit-key", "sha256", "digest", "Juan", "Perez", "@", "phone"):
        assert leak not in raw, leak
    lst(client, adm, w)
    detail(client, adm, w, out["activity_id"])
    assert len(audit("loan.collection_activity")) == 1  # reads are not audited


# ================================ code / legacy isolation =============================================
def test_the_activity_code_has_no_legacy_money_outcome_text_gps_or_promise():
    src = (ROOT / "app/modules/loans/activities.py").read_text(encoding="utf-8")
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
        "location_ping",
        "outcome",
        "observation",
        "notes",
        "promise",
        "promised_amount",
        "latitude",
    ):
        assert banned not in names, banned
    assert "collections.assign" not in src and "branch_id" not in src.replace("managing_branch_id", "")
    model = (ROOT / "app/modules/loans/models.py").read_text(encoding="utf-8")
    block = model[model.index("class CreditCollectionActivity") : model.index("ACTIVITY_GUARD_FN")]
    columns = set(re.findall(r"^    (\w+): Mapped", block, re.M))
    assert columns == {
        "id",
        "tenant_id",
        "loan_id",
        "managing_branch_id",
        "recorded_by",
        "assignment_id",
        "activity_type",
        "created_at",
        "idempotency_key",
        "request_digest",
    }  # exactly these: no outcome, text, status, update/delete marker, customer, assignee, contact, promise or GPS
    for module in ("payments.py", "reversals.py", "overdue.py", "worklist.py"):
        module_src = (ROOT / "app/modules/loans" / module).read_text(encoding="utf-8")
        assert "collection_activit" not in module_src and "CreditCollectionActivity" not in module_src, module
    assert activity_service.CREATE == CREATE


# ================================ migration 0016 ======================================================
def test_migration_0016_empty_downgrade_reupgrade_and_alembic_check(scratch_db):
    from sqlalchemy import create_engine

    mig = (ROOT / "alembic/versions/0016_credit_collection_activity.py").read_text(encoding="utf-8")
    ops = re.findall(r"op\.(\w+)\(", mig)
    assert set(ops) <= {
        "create_unique_constraint",
        "create_table",
        "create_index",
        "execute",
        "bulk_insert",
        "get_bind",
        "drop_index",
        "drop_table",
        "drop_constraint",
        "f",
    }
    assert _alembic(scratch_db, "upgrade", "0015").returncode == 0
    eng = create_engine(scratch_db)
    try:
        up = _alembic(scratch_db, "upgrade", "head")
        assert up.returncode == 0, up.stderr
        assert _alembic(scratch_db, "check").returncode == 0
        with eng.connect() as c:
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0016"
            assert c.execute(text("SELECT count(*) FROM permissions WHERE code = 'collections.actions.create'")).scalar() == 1
            assert (
                c.execute(
                    text("SELECT count(*) FROM pg_constraint WHERE conname = 'uq_credit_collection_assignments_tenant_id_loan'")
                ).scalar()
                == 1
            )
            idx = {r[0] for r in c.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'credit_collection_activities'"))}
            assert idx == {
                "pk_credit_collection_activities",
                "ix_credit_collection_activities_loan_id_id",
                "uq_credit_collection_activities_tenant_key",
            }  # only the index the list query needs
        down = _alembic(scratch_db, "downgrade", "0015")
        assert down.returncode == 0, down.stderr
        with eng.connect() as c:
            assert c.execute(text("SELECT to_regclass('credit_collection_activities')")).scalar() is None
            assert c.execute(text("SELECT count(*) FROM permissions WHERE code = 'collections.actions.create'")).scalar() == 0
            assert (
                c.execute(
                    text("SELECT count(*) FROM pg_constraint WHERE conname = 'uq_credit_collection_assignments_tenant_id_loan'")
                ).scalar()
                == 0
            )
            assert c.execute(text("SELECT count(*) FROM pg_proc WHERE proname LIKE 'credit_collection_activities%'")).scalar() == 0
        again = _alembic(scratch_db, "upgrade", "head")
        assert again.returncode == 0, again.stderr
        assert _alembic(scratch_db, "check").returncode == 0
    finally:
        eng.dispose()


def test_downgrade_0016_is_refused_before_any_ddl_when_activity_history_exists(scratch_db):
    from sqlalchemy import create_engine

    assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    eng = create_engine(scratch_db)
    try:
        _seed(eng)
        with eng.begin() as c:
            c.execute(text("SET session_replication_role = replica"))  # the point is the guard, not a whole loan chain
            c.execute(
                text(
                    "INSERT INTO credit_collection_activities (tenant_id, loan_id, managing_branch_id, recorded_by, "
                    "assignment_id, activity_type, created_at, idempotency_key, request_digest) "
                    "SELECT id, 1, NULL, 1, NULL, 'other', now(), 'downgrade-key-0001', 'd' FROM companies"
                )
            )
        refused = _alembic(scratch_db, "downgrade", "0015")
        assert refused.returncode != 0 and "Cannot downgrade 0016" in refused.stderr
        with eng.connect() as c:  # nothing was dropped, nothing was erased
            assert c.execute(text("SELECT count(*) FROM credit_collection_activities")).scalar() == 1
            assert c.execute(text("SELECT count(*) FROM permissions WHERE code = 'collections.actions.create'")).scalar() == 1
            assert (
                c.execute(
                    text("SELECT count(*) FROM pg_constraint WHERE conname = 'uq_credit_collection_assignments_tenant_id_loan'")
                ).scalar()
                == 1
            )
            assert c.execute(text("SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg_credit_collection_activities%'")).scalar() == 2
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0016"
    finally:
        eng.dispose()


def test_every_activity_query_is_tenant_scoped_in_the_sql_itself(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    stmts: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        if "FROM credit_collection_activities" in statement:
            stmts.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        made = act(client, adm, w, k="sql-key-00000001")
        act(client, adm, w, k="sql-key-00000001")  # the replay lookup
        lst(client, adm, w)
        detail(client, adm, w, made["activity_id"])
    finally:
        event.remove(engine, "before_cursor_execute", before)
    assert len(stmts) >= 4
    # defence in depth: each lookup carries its own tenant condition, not only the loan-id one
    assert all("credit_collection_activities.tenant_id =" in s.split("WHERE", 1)[1] for s in stmts), stmts
