"""T-013 Collection worklist assignment filters tests (T013-*). PostgreSQL only.

``assignment=mine|unassigned|assigned`` and ``assignee_id`` are an extra RESTRICTION over the T-011 worklist (never access):
only the current/open row of ``credit_collection_assignments`` counts, stale assignments are read and never repaired, an
unknown / foreign assignee is an empty result (no user lookup), each row carries ``current_assignment`` (ids + timestamp),
and the cursor is bound to a fingerprint of every membership filter. Read-only: no write, no new permission, no index.
"""

import base64
import io
import json
import re
import tokenize
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, text

from app.core.db import SessionLocal, engine
from app.modules.loans import worklist as worklist_service
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
)
from tests.test_t008_payments import LEGACY, pay, schedule
from tests.test_t010_overdue_projection import contractual_rows, money_counts, world10, write_listener
from tests.test_t011_collection_worklist import clock, first_due, ids, local, mkloan, wl
from tests.test_t012_collection_assignment import (
    _fresh_pool,  # noqa: F401  (autouse: new pooled connections for every recreated schema)
    assign,
    end,
    mkuser,
    rows,
    set_loan_status,
)

ROOT = Path(__file__).resolve().parent.parent
ITEM_ASSIGNMENT_KEYS = {"assignment_id", "assignee_user_id", "assigned_at"}


# ================================ helpers ============================================================
def lid(w):
    return w.loan["id"]


def by_loan(body):
    return {i["loan_id"]: i for i in body["items"]}


def world(client, sink, adm, tenant, monkeypatch, *, branches=False):
    """Four overdue loans (base has NO managing branch) + two tenant-level readers that can be assignees."""
    base = world10(client, adm, tenant)
    b_a, b_b = (mk_branch(client, adm, "AS-A"), mk_branch(client, adm, "AS-B")) if branches else (None, None)
    loans = {
        "n": base,
        "a1": mkloan(client, adm, tenant, "Alfa", base=base, managing=b_a),
        "a2": mkloan(client, adm, tenant, "Beta", base=base, managing=b_a),
        "b1": mkloan(client, adm, tenant, "Gama", base=base, managing=b_b),
    }
    _h1, u1 = mkuser(client, sink, adm, tenant, "u1@x.com", ["collections.read"])
    _h2, u2 = mkuser(client, sink, adm, tenant, "u2@x.com", ["collections.read"])
    clock(monkeypatch, local(first_due(client, adm, base) + timedelta(days=5)))
    return loans, (b_a, b_b), (u1, u2)


# ================================ current assignment only =============================================
def test_rows_carry_the_open_assignment_and_filters_use_only_current_rows(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, _b, (u1, u2) = world(client, sink, adm, tenant_a, monkeypatch)
    n, a1, a2, b1 = (lid(loans[k]) for k in ("n", "a1", "a2", "b1"))
    everything = {n, a1, a2, b1}
    base = wl(client, adm, limit=100)
    assert set(ids(base)) == everything
    assert all(i["current_assignment"] is None for i in base["items"])  # unassigned row -> null, still listed
    assign(client, adm, loans["a1"], u1)
    assign(client, adm, loans["a2"], u1)
    assign(client, adm, loans["b1"], u2)
    after = wl(client, adm, limit=100)
    assert set(ids(after)) == everything  # no filter: the SAME population as T-011
    for i in after["items"]:  # only the T-011 keys plus the new one
        assert set(i) - {"current_assignment"} == set(base["items"][0]) - {"current_assignment"}
    cur = by_loan(after)[a1]["current_assignment"]
    assert set(cur) == ITEM_ASSIGNMENT_KEYS and cur["assignee_user_id"] == u1
    assert cur["assignment_id"] == [r[0] for r in rows(a1)][0] and by_loan(after)[n]["current_assignment"] is None
    # filters
    assert set(ids(wl(client, adm, assignment="assigned", limit=100))) == {a1, a2, b1}
    assert ids(wl(client, adm, assignment="unassigned")) == [n]
    assert set(ids(wl(client, adm, assignee_id=u1, limit=100))) == {a1, a2}
    assert ids(wl(client, adm, assignee_id=u2)) == [b1]
    # reassignment: the old assignee no longer matches, the new one does; the closed row is history, not current
    assign(client, adm, loans["a2"], u2)
    assert set(ids(wl(client, adm, assignee_id=u1, limit=100))) == {a1}
    assert set(ids(wl(client, adm, assignee_id=u2, limit=100))) == {a2, b1}
    assert by_loan(wl(client, adm, limit=100))[a2]["current_assignment"]["assignee_user_id"] == u2
    # ended history only -> unassigned (several closed rows), and it is no longer "assigned"
    end(client, adm, loans["a2"])
    assign(client, adm, loans["a2"], u1)
    end(client, adm, loans["a2"])
    assert len(rows(a2)) == 3 and all(r[6] is not None for r in rows(a2))
    assert set(ids(wl(client, adm, assignment="unassigned", limit=100))) == {n, a2}
    assert a2 not in ids(wl(client, adm, assignment="assigned", limit=100))
    assert a2 not in ids(wl(client, adm, assignee_id=u1, limit=100)) + ids(wl(client, adm, assignee_id=u2, limit=100))
    assert by_loan(wl(client, adm, limit=100))[a2]["current_assignment"] is None
    # assigned + unassigned partition the base population
    both = set(ids(wl(client, adm, assignment="assigned", limit=100))) | set(
        ids(wl(client, adm, assignment="unassigned", limit=100))
    )
    assert both == everything


def test_mine_resolves_the_actor_and_is_equivalent_to_the_explicit_self_id(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, _b, (u1, u2) = world(client, sink, adm, tenant_a, monkeypatch)
    h1, _ = mkuser(client, sink, adm, tenant_a, "me@x.com", ["collections.read"])
    me = _id("me@x.com")
    assign(client, adm, loans["a1"], me)
    assign(client, adm, loans["b1"], u1)
    assert ids(wl(client, h1, assignment="mine")) == [lid(loans["a1"])]
    assert ids(wl(client, h1, assignee_id=me)) == ids(wl(client, h1, assignment="mine"))
    assert ids(wl(client, adm, assignment="mine")) == []  # the admin is the assignee of nothing
    # another reader sees the SAME rows of someone else with assignee_id (collections.read is enough: no assign needed)
    assert ids(wl(client, h1, assignee_id=u1)) == [lid(loans["b1"])]


def _id(email):
    with SessionLocal() as db:
        return db.execute(text("SELECT id FROM users WHERE email = :e"), {"e": email}).scalar()


# ================================ assignment never widens access ======================================
def test_assignment_never_grants_access_and_intersects_with_the_scope(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, (b_a, b_b), (u1, _u2) = world(client, sink, adm, tenant_a, monkeypatch, branches=True)
    n, a1, a2, b1 = (lid(loans[k]) for k in ("n", "a1", "a2", "b1"))
    at_a, id_a = mkuser(client, sink, adm, tenant_a, "ra@x.com", ["collections.read"])  # eligible: tenant-level for now
    at_b, id_b = mkuser(
        client, sink, adm, tenant_a, "rb@x.com", ["collections.read"], scope="branch", branch_id=b_b["id"]
    )
    for k in ("n", "a1", "b1"):
        assign(client, adm, loans[k], id_a)
    with SessionLocal() as db:  # the assignee is later narrowed to branch A (an open assignment is never repaired)
        db.execute(
            text("UPDATE user_role_assignments SET scope_kind = 'branch', branch_id = :b WHERE user_id = :u"),
            {"b": b_a["id"], "u": id_a},
        )
        db.commit()
    assert ids(wl(client, at_a, assignment="mine", limit=100)) == [
        a1
    ]  # NULL-branch + other-branch loans stay invisible
    assert ids(wl(client, at_a, assignment="assigned", limit=100)) == [a1]  # a2 is unassigned, n / b1 are out of scope
    # assignee_id: branch A reader filtering by a user that also holds loans in B / NULL: only branch A comes back
    assert ids(wl(client, at_a, assignee_id=id_a)) == [a1]
    assert ids(wl(client, at_b, assignee_id=id_a)) == [b1]
    assert set(ids(wl(client, adm, assignee_id=id_a, limit=100))) == {n, a1, b1}  # tenant scope sees all of them
    # the branch filter intersects the assignment filter
    assert ids(wl(client, adm, assignee_id=id_a, branch_id=b_b["id"])) == [b1]
    assert ids(wl(client, adm, assignment="unassigned", branch_id=b_a["id"])) == [a2]
    wl(client, at_a, expect=403, assignee_id=id_a, branch_id=b_b["id"])
    # a user WITHOUT collections.read gets nothing from being assigned
    nobody, _ = mkuser(client, sink, adm, tenant_a, "nb@x.com", ["loans.read"])
    wl(client, nobody, expect=403, assignment="mine")
    wl(client, nobody, expect=403, assignee_id=u1)


def test_a_stale_assignee_is_still_filterable_and_never_repaired_but_rbac_decides_what_it_sees(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    loans, _b, _u = world(client, sink, adm, tenant_a, monkeypatch)
    h3, u3 = mkuser(client, sink, adm, tenant_a, "u3@x.com", ["collections.read"])
    assign(client, adm, loans["a1"], u3)
    assert ids(wl(client, h3, assignment="mine")) == [lid(loans["a1"])]
    rows_before = rows()
    with SessionLocal() as db:  # u3 loses collections.read; later it is also disabled
        db.execute(
            text(
                "DELETE FROM role_permissions WHERE role_id IN (SELECT role_id FROM user_role_assignments "
                "WHERE user_id = :u) AND permission_id = (SELECT id FROM permissions WHERE code = 'collections.read')"
            ),
            {"u": u3},
        )
        db.commit()
    wl(client, h3, expect=403, assignment="mine")  # RBAC decides: being the assignee is not access
    assert ids(wl(client, adm, assignee_id=u3)) == [lid(loans["a1"])]  # still the CURRENT assignee for any other reader
    with SessionLocal() as db:
        db.execute(text("UPDATE users SET status = 'disabled' WHERE id = :u"), {"u": u3})
        db.commit()
    assert ids(wl(client, adm, assignee_id=u3)) == [lid(loans["a1"])]  # an inactive assignee still filters
    assert rows() == rows_before  # nothing was closed, repaired or rewritten by reading


def test_unknown_foreign_or_unassigned_assignee_ids_are_an_indistinguishable_empty_result(
    client, sink, tenant_a, tenant_b, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    loans, _b, (u1, _u2) = world(client, sink, adm, tenant_a, monkeypatch)
    adm_b = admin_headers(client, tenant_b)
    _hb, foreign = mkuser(client, sink, adm_b, tenant_b, "fb@x.com", ["collections.read"])
    assign(client, adm, loans["a1"], u1)
    quiet, _ = mkuser(client, sink, adm, tenant_a, "quiet@x.com", ["collections.read"])
    quiet_id = _id("quiet@x.com")
    answers = [wl(client, adm, assignee_id=x) for x in (foreign, 999999, quiet_id)]
    assert all(a["items"] == [] and a["next_cursor"] is None for a in answers)
    assert answers[0] == answers[1] == answers[2]  # no existence oracle: same body for every kind of id
    # a foreign user assigned in ITS tenant never shows up in this tenant's worklist either
    assert wl(client, adm_b, assignee_id=u1)["items"] == []


# ================================ validation ==========================================================
def test_the_filters_are_validated_and_mutually_exclusive(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    _loans, _b, (u1, _u2) = world(client, sink, adm, tenant_a, monkeypatch)
    for bad in (
        {"assignment": "everyone"},
        {"assignment": "Mine"},
        {"assignment": "true"},
        {"assignee_id": 0},
        {"assignee_id": -3},
        {"assignee_id": "abc"},
        {"assignment": "mine", "assignee_id": u1},
        {"assignment": "unassigned", "assignee_id": u1},
        {"assignment": "assigned", "assignee_id": u1},
    ):
        wl(client, adm, expect=422, **bad)
    r = client.get(
        worklist_url(), headers=adm, params={"assignment": "mine", "assignee_id": u1}
    )  # a precise error, no silent precedence
    assert r.json()["error"]["code"] == "conflicting_assignment_filters"
    for ignored in ({"assigned_to_me": "true"}, {"unassigned": "true"}):  # the booleans are NOT part of the API
        assert len(wl(client, adm, limit=100, **ignored)["items"]) == 4  # unknown parameters never filter
    assert wl(client, adm, assignment="assigned", sort="oldest_overdue_date", order="asc", limit=5)["sort"] == (
        "oldest_overdue_date"
    )


def worklist_url():
    from tests.test_t011_collection_worklist import W

    return W


# ================================ not overdue / not listed ============================================
def test_an_assignment_never_forces_a_loan_into_the_worklist(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    base = world10(client, adm, tenant_a)
    other = mkloan(client, adm, tenant_a, "Otro", base=base)
    third = mkloan(client, adm, tenant_a, "Tercero", base=base)
    _h, u1 = mkuser(client, sink, adm, tenant_a, "u1@x.com", ["collections.read"])
    for w in (base, other, third):
        assign(client, adm, w, u1)  # an `active` loan that is not overdue yet: assignable, but not listed
    assert wl(client, adm, assignment="assigned")["items"] == []
    assert wl(client, adm, assignee_id=u1)["items"] == []
    d1 = first_due(client, adm, base)
    clock(monkeypatch, local(d1 + timedelta(days=5)))
    assert set(ids(wl(client, adm, assignment="mine", limit=100))) == set()  # the admin is not the assignee
    assert set(ids(wl(client, adm, assignee_id=u1, limit=100))) == {lid(base), lid(other), lid(third)}
    rows_o = schedule(client, adm, lid(other))
    pay(
        client, adm, other, rows_o[0]["total_due"], origin="field"
    )  # no longer overdue: the open row stays, the loan leaves
    set_loan_status(lid(third), "paid")  # a STORED status is never the truth (T-011): the net ledger still lists it
    assert set(ids(wl(client, adm, assignee_id=u1, limit=100))) == {lid(base), lid(third)}
    assert len(rows(lid(other))) == 1 and rows(lid(other))[0][6] is None  # still open: nothing auto-closed
    assert wl(client, adm, assignment="unassigned")["items"] == []


def test_legacy_pointers_and_the_collector_role_are_ignored(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, _b, (u1, _u2) = world(client, sink, adm, tenant_a, monkeypatch)
    with SessionLocal() as db:
        db.execute(text("UPDATE users SET role = 'collector' WHERE id = :u"), {"u": u1})
        db.execute(
            text(
                'INSERT INTO customers (id, full_name, phone, address, "references", version, company_id, created_by_id, '
                "created_at, assigned_collector_id) VALUES (:c, 'Legacy', '', '', CAST('[]' AS json), 1, :t, :u, now(), :a)"
            ),
            {"c": loans["n"].c["id"], "t": tenant_a["tenant_id"], "u": tenant_a["admin_id"], "a": u1},
        )
        db.commit()
    assert wl(client, adm, assignee_id=u1)["items"] == []  # neither pointer nor role is an assignment
    assert wl(client, adm, assignment="assigned")["items"] == []
    assert len(wl(client, adm, assignment="unassigned", limit=100)["items"]) == 4
    assert all(i["current_assignment"] is None for i in wl(client, adm, limit=100)["items"])


# ================================ cursor ==============================================================
def test_the_cursor_is_bound_to_every_membership_filter_and_never_to_the_scope(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, (b_a, _bb), (u1, u2) = world(client, sink, adm, tenant_a, monkeypatch, branches=True)
    for k in ("n", "a1", "a2", "b1"):
        assign(client, adm, loans[k], u1)
    hu1, _ = mkuser(client, sink, adm, tenant_a, "me1@x.com", ["collections.read"])
    hu2, _ = mkuser(client, sink, adm, tenant_a, "me2@x.com", ["collections.read"])
    id1, id2 = _id("me1@x.com"), _id("me2@x.com")
    for k in ("a1", "a2"):
        assign(client, adm, loans[k], id1, k=f"re-{k}-{id1}-0000001")
    assign(client, adm, loans["b1"], id2, k=f"re-b1-{id2}-00000001")

    def raw(cursor):
        return json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))

    p1 = wl(client, adm, assignee_id=u1, limit=1)
    assert p1["next_cursor"] is None or set(raw(p1["next_cursor"])) == {"s", "o", "v", "i", "f"}
    first = wl(client, adm, limit=1)
    cur = first["next_cursor"]
    assert set(raw(cur)) == {"s", "o", "v", "i", "f"} and re.fullmatch(r"[0-9a-f]{16}", raw(cur)["f"])
    assert wl(client, adm, limit=50, cursor=cur)["items"]  # the same filters + another page size is accepted
    # every membership filter, sort and order mismatch is an invalid cursor
    for other in (
        {"branch_id": b_a["id"]},
        {"min_days_overdue": 1},
        {"currency": "DOP"},
        {"assignment": "assigned"},
        {"assignment": "unassigned"},
        {"assignee_id": u1},
        {"sort": "overdue_outstanding"},
        {"order": "asc"},
    ):
        r = client.get(worklist_url(), headers=adm, params={"limit": 1, "cursor": cur} | other)
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_cursor", other
    # a cursor of one assignee is not valid for another
    c_u1 = wl(client, adm, assignee_id=id1, limit=1)["next_cursor"]
    assert c_u1 is None or wl(client, adm, expect=422, assignee_id=id2, limit=1, cursor=c_u1)
    # `mine` of user A reused as `mine` by user B: 422 (the RESOLVED assignee is in the fingerprint)
    for k in ("n",):
        assign(client, adm, loans[k], id1, k=f"re-{k}-{id1}-0000001")
    mine1 = wl(client, hu1, assignment="mine", limit=1)
    assert mine1["next_cursor"] is not None
    wl(client, hu2, expect=422, assignment="mine", limit=1, cursor=mine1["next_cursor"])
    # canonical equivalence: `mine` of A == assignee_id=A, so the cursor is interchangeable for the SAME user
    assert wl(client, hu1, assignee_id=id1, limit=1, cursor=mine1["next_cursor"])["items"]
    # an old T-011 cursor (no "f") and tampered ones are invalid
    old = base64.urlsafe_b64encode(json.dumps({"s": "days_overdue", "o": "desc", "v": "5", "i": 1}).encode()).decode()
    wl(client, adm, expect=422, cursor=old)
    forged = {**raw(cur), "f": "0" * 16}
    wl(client, adm, expect=422, cursor=base64.urlsafe_b64encode(json.dumps(forged).encode()).decode())
    # a cursor never widens the scope: a branch reader paging with a cursor from the tenant view still sees its scope only
    at_a, _ = mkuser(client, sink, adm, tenant_a, "ra@x.com", ["collections.read"], scope="branch", branch_id=b_a["id"])
    page = wl(client, at_a, assignee_id=id1, limit=1)
    assert set(ids(page)) <= {lid(loans["a1"]), lid(loans["a2"])}


def test_pagination_with_an_assignment_filter_is_complete_and_follows_live_semantics(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    loans, _b, (u1, u2) = world(client, sink, adm, tenant_a, monkeypatch)
    for k in ("n", "a1", "a2"):
        assign(client, adm, loans[k], u1)
    assign(client, adm, loans["b1"], u2)
    whole = ids(wl(client, adm, assignee_id=u1, limit=100))
    seen, cursor = [], None
    while True:
        page = wl(client, adm, assignee_id=u1, limit=1, cursor=cursor)
        seen += ids(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == whole and len(set(seen)) == len(seen) == 3  # no duplicate, no omission (static dataset)
    # live: the membership may change between pages; no snapshot, no crash, no widening
    first = wl(client, adm, assignee_id=u1, limit=1)
    moved = [x for x in whole if x not in ids(first)][0]
    mover = next(w for w in loans.values() if lid(w) == moved)
    assign(client, adm, mover, u2)  # reassigned between pages: leaves the u1 set
    rest, cursor = [], first["next_cursor"]
    while cursor:
        page = wl(client, adm, assignee_id=u1, limit=1, cursor=cursor)
        rest += ids(page)
        cursor = page["next_cursor"]
    assert moved not in rest and set(rest) <= set(whole)
    end(client, adm, next(w for w in loans.values() if lid(w) == ids(first)[0]))  # an end between pages: no error
    assert wl(client, adm, assignment="unassigned")["items"] != []


# ================================ read-only, cheap, no PII ============================================
def test_the_filtered_worklist_is_read_only_pii_free_and_has_no_n_plus_one(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, _b, (u1, u2) = world(client, sink, adm, tenant_a, monkeypatch)
    for k, u in (("n", u1), ("a1", u1), ("a2", u2), ("b1", u1)):
        assign(client, adm, loans[k], u)
    end(client, adm, loans["a2"])
    assign(client, adm, loans["a2"], u2)

    def selects(**params):
        stmts: list[str] = []

        def before(conn, cursor, statement, parameters, context, executemany):
            if re.match(r"\s*SELECT", statement, re.I):
                stmts.append(statement[:80])

        event.listen(engine, "before_cursor_execute", before)
        try:
            body = wl(client, adm, **params)
        finally:
            event.remove(engine, "before_cursor_execute", before)
        return len(stmts), body, stmts

    one_n, _, _ = selects(limit=1)
    four_n, body, stmts = selects(limit=100)
    assert len(body["items"]) == 4 and four_n == one_n  # the assignment read is ONE batched query, not one per row
    assert four_n <= 13
    assert len([s for s in stmts if "credit_collection_assignments" in s]) <= 1
    for params in (
        {"assignment": "mine"},
        {"assignment": "assigned"},
        {"assignment": "unassigned"},
        {"assignee_id": u1},
    ):
        n1, _, _ = selects(limit=1, **params)
        n4, _, _ = selects(limit=100, **params)
        assert n1 <= n4 <= n1 + 1 and n4 <= 13, params  # constant: the filter adds no per-row query
    raw = json.dumps(body, default=str)
    for pii in (
        "Juan",
        "Perez",
        "NombreAlfa",
        "ApellidoBeta",
        "@",
        "phone",
        "address",
        "request_digest",
        "idempotency",
        "sha256",
    ):
        assert pii not in raw, pii
    assert all(set(i["current_assignment"]) == ITEM_ASSIGNMENT_KEYS for i in body["items"])
    before_rows, legacy_before, money_before, contract_before = (
        rows(),
        {t: count(t) for t in LEGACY},
        money_counts(),
        contractual_rows(),
    )
    events_before = count("security_events")
    stored = _stored_statuses()
    statements, stop = write_listener()
    try:
        for params in (
            {},
            {"assignment": "mine"},
            {"assignment": "assigned"},
            {"assignment": "unassigned"},
            {"assignee_id": u1},
            {"assignee_id": 424242},
            {"assignee_id": u1, "limit": 1},
        ):
            wl(client, adm, **params)
    finally:
        stop()
    assert statements == []  # 0 INSERT / UPDATE / DELETE
    assert rows() == before_rows and count("security_events") == events_before
    assert _stored_statuses() == stored
    assert {t: count(t) for t in LEGACY} == legacy_before and money_counts() == money_before
    assert contractual_rows() == contract_before


def _stored_statuses():
    with SessionLocal() as db:
        return db.execute(text("SELECT id, status FROM credit_loans ORDER BY id")).all()


def test_every_assignment_read_is_tenant_scoped_in_the_sql_itself(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, _b, (u1, _u2) = world(client, sink, adm, tenant_a, monkeypatch)
    assign(client, adm, loans["a1"], u1)
    stmts: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        if "credit_collection_assignments" in statement:
            stmts.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        for params in ({}, {"assignment": "assigned"}, {"assignment": "unassigned"}, {"assignee_id": u1}):
            wl(client, adm, **params)
    finally:
        event.remove(engine, "before_cursor_execute", before)
    assert len(stmts) >= 4
    # defence in depth: the assignment lookups carry their own tenant condition, not only the loan-id join
    assert all("credit_collection_assignments.tenant_id" in s for s in stmts), stmts


def test_the_overdue_result_does_not_depend_on_the_assignment(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, _b, (u1, _u2) = world(client, sink, adm, tenant_a, monkeypatch)
    plain = wl(client, adm, limit=100)["items"]
    for w in loans.values():
        assign(client, adm, w, u1)
    assigned = wl(client, adm, limit=100)["items"]
    strip = lambda items: [{k: v for k, v in i.items() if k != "current_assignment"} for i in items]  # noqa: E731
    assert strip(plain) == strip(assigned)  # same rows, same order, same overdue facts
    filtered = wl(client, adm, assignment="assigned", limit=100)["items"]
    assert strip(plain) == strip(filtered)  # a filtered read gives the very same facts (no assignment-driven number)
    assert strip(plain) == strip(wl(client, adm, assignee_id=u1, limit=100)["items"])


# ================================ code / migration ====================================================
def test_the_code_reads_only_the_open_modern_assignment_and_adds_no_permission_index_or_migration():
    src = (ROOT / "app/modules/loans/worklist.py").read_text(encoding="utf-8")
    names = {t.string for t in tokenize.generate_tokens(io.StringIO(src).readline) if t.type == tokenize.NAME}
    for banned in (
        "assigned_collector",
        "assigned_collector_id",
        "route_id",
        "UserAccount",  # no user lookup: the assignee id is never validated against users
        "UserRole",
        "role",
        "collections_assign",
        "refresh_loan_state",
        "hash",  # Python hash() is not stable across processes
    ):
        assert banned not in names, banned
    assert "ASSIGN" not in names and "collections.assign" not in src  # collections.assign is a WRITE permission only
    assert "ended_at" in src  # the open row only
    versions = sorted(p.name for p in (ROOT / "alembic/versions").glob("0*.py"))
    assert any(v.startswith("0015_") for v in versions)  # the index is justified by the EXPLAIN evidence documented for T-013
    catalog = (ROOT / "app/modules/identity/catalog.py").read_text(encoding="utf-8")
    assert "collections.work" not in catalog


def test_migration_0015_only_adds_the_partial_index_and_is_reversible_even_with_history(scratch_db):
    from sqlalchemy import create_engine

    from tests.test_t012_collection_assignment import _seed

    mig = (ROOT / "alembic/versions/0015_credit_collection_worklist_assignment_index.py").read_text(encoding="utf-8")
    ops = re.findall(r"op\.(\w+)\(", mig)
    assert sorted(ops) == ["create_index", "drop_index"]  # no table, column, permission, constraint or data change
    assert _alembic(scratch_db, "upgrade", "0014").returncode == 0
    eng = create_engine(scratch_db)
    try:
        _seed(eng)
        with eng.begin() as c:
            c.execute(text("SET session_replication_role = replica"))  # the point is the downgrade, not a loan chain
            c.execute(
                text(
                    "INSERT INTO credit_collection_assignments (tenant_id, loan_id, assignee_user_id, managing_branch_id, "
                    "assigned_by, assigned_at, idempotency_key, request_digest) SELECT id, 1, 1, NULL, 1, now(), "
                    "'t013-key-00000001', 'd' FROM companies"
                )
            )
        up = _alembic(scratch_db, "upgrade", "0015")
        assert up.returncode == 0, up.stderr
        # (alembic check compares against the models at head, which also hold T-014: it runs after the final re-upgrade)
        with eng.connect() as c:
            d = c.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_credit_collection_assignments_open_assignee'")
            ).scalar()
            assert d and "(tenant_id, assignee_user_id, loan_id)" in d and "WHERE (ended_at IS NULL)" in d
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0015"
        down = _alembic(scratch_db, "downgrade", "0014")  # only an index: the history is untouched, no guard needed
        assert down.returncode == 0, down.stderr
        with eng.connect() as c:
            assert c.execute(text("SELECT count(*) FROM credit_collection_assignments")).scalar() == 1
            assert c.execute(text("SELECT to_regclass('ix_credit_collection_assignments_open_assignee')")).scalar() is None
        again = _alembic(scratch_db, "upgrade", "head")
        assert again.returncode == 0, again.stderr
        assert _alembic(scratch_db, "check").returncode == 0
    finally:
        eng.dispose()


@pytest.fixture(autouse=True)
def _pool_guard():
    yield


def test_a_really_paid_loan_keeps_its_open_assignment_but_is_not_listed(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    w = world10(client, adm, tenant_a)
    _h, u1 = mkuser(client, sink, adm, tenant_a, "u1@x.com", ["collections.read"])
    assign(client, adm, w, u1)
    sched = schedule(client, adm, lid(w))
    last = max(date.fromisoformat(r["due_date"]) for r in sched)
    clock(monkeypatch, local(last + timedelta(days=1)))
    assert ids(wl(client, adm, assignee_id=u1)) == [lid(w)]  # overdue and assigned
    pay(client, adm, w, sum(r["total_due"] for r in sched), origin="field")
    bal = client.get(f"/api/v2/loans/{lid(w)}/balances", headers=adm).json()
    assert bal["projected_status"] == "paid"
    for params in ({"assignee_id": u1}, {"assignment": "assigned"}, {}):
        assert wl(client, adm, **params)["items"] == []  # paid: not overdue -> not listed, whatever the assignment
    assert len(rows(lid(w))) == 1 and rows(lid(w))[0][6] is None  # the open row survives (T-012: only `end` closes)


def test_the_assignment_filter_restricts_the_candidates_before_the_ledger_is_read(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    loans, _b, (u1, u2) = world(client, sink, adm, tenant_a, monkeypatch)
    for k in ("n", "a1", "a2"):
        assign(client, adm, loans[k], u1)
    assign(client, adm, loans["b1"], u2)
    seen: list[int] = []
    real = worklist_service.ledger.views_many

    def spy(db, loan_ids):
        seen.append(len(loan_ids))
        return real(db, loan_ids)

    monkeypatch.setattr(worklist_service.ledger, "views_many", spy)
    for params, expected in (
        ({"assignee_id": u1}, 3),
        ({"assignee_id": u2}, 1),
        ({"assignment": "assigned"}, 4),
        ({"assignment": "unassigned"}, 0),
        ({}, 4),
    ):
        seen.clear()
        wl(client, adm, **params)
        assert seen == [expected], params  # the ledger of the loans that are filtered OUT is never loaded
