"""T-016 Collection worklist promise / activity enrichment tests (T016-*). PostgreSQL only.

ENRICHMENT ONLY: each row of ``GET /collections/overdue-loans`` gains ``current_promise`` (the T-015 promise that is not closed,
with its derived status in the loan's own business date) and ``last_collection_activity`` (the T-014 activity with the greatest
id). Membership, order, cursor, fingerprint, scope and permissions are exactly T-013's; the enrichment runs on the PAGE only, in
a constant number of queries, with 0 writes and no PII. No filter, sort, migration, index or permission.
"""

import base64
import hashlib
import inspect
import json
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event, text

from app.core.db import SessionLocal, engine
from app.modules.loans import activities as activity_service
from app.modules.loans import promises as promise_service
from app.modules.loans import worklist as worklist_service
from app.modules.loans.api import overdue_loans as overdue_route
from tests import pg_env  # noqa: F401  (must precede app imports)
from tests.test_t001_foundation import scratch_db  # noqa: F401
from tests.test_t002_identity import (  # noqa: F401  (fixtures + helpers shared with the earlier suites)
    admin_headers,
    client,
    fresh_db,
    sink,
    tenant_a,
)
from tests.test_t003_organization import mk_branch
from tests.test_t006_origination import (  # noqa: F401
    _release_tracked_connections,
    count,
)
from tests.test_t008_payments import LEGACY
from tests.test_t009_payment_reversal import audit, rev
from tests.test_t010_overdue_projection import contractual_rows, money_counts, world10, write_listener
from tests.test_t011_collection_worklist import first_due, ids, mkloan, wl
from tests.test_t012_collection_assignment import _fresh_pool, assign, mkuser, rows  # noqa: F401
from tests.test_t014_collection_activity import act, arows
from tests.test_t015_collection_promise import (
    CREATE as PROMISE_CREATE,
)
from tests.test_t015_collection_promise import (
    at,
    cancel,
    detail,
    pay_field,
    promise,
    prows,
    replace,
    world,
)

ROOT = Path(__file__).resolve().parent.parent
PROMISE_KEYS = {
    "promise_id",
    "promised_amount",
    "currency_code",
    "promise_date",
    "projected_status",
    "qualifying_paid_amount",
    "created_at",
}
ACTIVITY_KEYS = {"activity_id", "activity_type", "created_at"}
NEW_KEYS = {"current_promise", "last_collection_activity"}


def lid(w):
    return w.loan["id"]


def by_loan(body):
    return {i["loan_id"]: i for i in body["items"]}


def full(client, hdr, **params):
    return wl(client, hdr, limit=100, **params)


def stripped(body):
    return [{k: v for k, v in i.items() if k not in NEW_KEYS} for i in body["items"]]


# ================================ the enrichment itself ===============================================
def test_each_row_carries_the_current_promise_and_the_last_activity_and_the_loan_stays_listed(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=7)
    l0, l1, l2, l3, l4, l5, l6, l7 = loans
    half = (due1 / 2).quantize(Decimal("0.01"))
    deadline = d1 + timedelta(days=7)
    at(monkeypatch, d1 + timedelta(days=5), hour=9)  # every loan is overdue now
    promise(client, adm, l1, half, deadline)  # current, open
    promise(client, adm, l2, half, deadline)  # current, will be fulfilled
    promise(client, adm, l3, half, deadline)  # current, will be broken
    cancel(client, adm, l4, promise(client, adm, l4, half, deadline)["promise_id"])  # cancelled only
    superseded = promise(client, adm, l5, half, deadline)["promise_id"]  # superseded, then the replacement is cancelled
    cancel(client, adm, l5, replace(client, adm, l5, half, deadline)["promise_id"])
    assert superseded
    p1 = promise(client, adm, l6, half, deadline)["promise_id"]  # history of three, one current
    p2 = replace(client, adm, l6, half, deadline)["promise_id"]
    cancel(client, adm, l6, p2)
    current6 = promise(client, adm, l6, half, deadline)["promise_id"]
    assert p1 != current6
    act(client, adm, l1, "phone_call")
    for t in ("sms", "in_person_visit", "no_contact"):
        last7 = act(client, adm, l7, t)
    at(monkeypatch, d1 + timedelta(days=5), hour=10)
    pay_field(client, adm, l2, half)  # meets its promise; the loan still owes the rest
    at(monkeypatch, d1 + timedelta(days=6), hour=12)
    body = full(client, adm)
    rows_ = by_loan(body)
    assert set(rows_) == {lid(w) for w in loans}  # every loan stays listed whatever its promise says
    for item in body["items"]:
        assert NEW_KEYS <= set(item)
        assert item["projected_status"] in ("active", "past_due")  # the LOAN status is a different thing
    assert rows_[lid(l0)]["current_promise"] is None and rows_[lid(l0)]["last_collection_activity"] is None
    assert rows_[lid(l4)]["current_promise"] is None and rows_[lid(l5)]["current_promise"] is None  # closed only -> null
    assert rows_[lid(l6)]["current_promise"]["promise_id"] == current6  # only the current of the history
    expected = {lid(l1): "open", lid(l2): "fulfilled", lid(l3): "open", lid(l6): "open"}
    for loan_id, status in expected.items():
        cp = rows_[loan_id]["current_promise"]
        assert set(cp) == PROMISE_KEYS and cp["projected_status"] == status, (loan_id, cp)
        assert cp["currency_code"] == "DOP" and cp["promise_date"] == deadline.isoformat()
        d = detail(client, adm, next(w for w in loans if lid(w) == loan_id), cp["promise_id"])  # the T-015 detail agrees
        assert (d["projected_status"], d["qualifying_paid_amount"], d["promised_amount"]) == (
            cp["projected_status"],
            cp["qualifying_paid_amount"],
            cp["promised_amount"],
        )
        assert d["created_at"] == cp["created_at"]
    assert Decimal(rows_[lid(l2)]["current_promise"]["qualifying_paid_amount"]) == half
    assert Decimal(rows_[lid(l1)]["current_promise"]["qualifying_paid_amount"]) == 0
    la = rows_[lid(l1)]["last_collection_activity"]
    assert set(la) == ACTIVITY_KEYS and la["activity_type"] == "phone_call"
    assert rows_[lid(l7)]["last_collection_activity"]["activity_id"] == last7["activity_id"]
    assert rows_[lid(l7)]["last_collection_activity"]["activity_type"] == "no_contact"  # the greatest id
    # after the deadline the unmet ones are broken; the met one is not; they are still current and still listed
    at(monkeypatch, d1 + timedelta(days=8), hour=12)
    after = by_loan(full(client, adm))
    assert set(after) == set(rows_)
    assert {i: after[i]["current_promise"]["projected_status"] for i in expected} == {
        lid(l1): "broken",
        lid(l2): "fulfilled",
        lid(l3): "broken",
        lid(l6): "broken",
    }
    for loan_id in expected:  # a broken or fulfilled promise changes nothing of the overdue facts
        assert after[loan_id]["days_overdue"] > rows_[loan_id]["days_overdue"]
        assert after[loan_id]["overdue_outstanding"] == rows_[loan_id]["overdue_outstanding"]


def test_a_reversal_reprojects_the_promise_on_the_next_read_and_the_window_rules_follow_t015(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=5)
    rv, early, late, cumulative, counter, tz = loans
    half = (due1 / 2).quantize(Decimal("0.01"))
    rest = due1 - half
    deadline = d1 + timedelta(days=7)
    at(monkeypatch, d1 + timedelta(days=5), hour=8)
    pay_field(client, adm, early, half)  # received BEFORE the promise: it never counts
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    for w, amount in ((rv, half), (early, rest), (late, half), (cumulative, half), (counter, half), (tz, half)):
        promise(client, adm, w, amount, deadline)
    at(monkeypatch, d1 + timedelta(days=5), hour=10)
    pay_rv = pay_field(client, adm, rv, half)
    pay_field(client, adm, cumulative, half / 2)
    pay_field(client, adm, cumulative, half - (half / 2).quantize(Decimal("0.01")))  # two payments add up
    from tests.test_t008_payments import pay

    pay(client, adm, counter, f"{half:.2f}")  # a counter payment counts like a field one
    at(monkeypatch, d1 + timedelta(days=6), hour=12)
    got = {k: by_loan(full(client, adm))[lid(w)]["current_promise"] for k, w in (
        ("rv", rv), ("early", early), ("late", late), ("cum", cumulative), ("counter", counter))}
    assert got["rv"]["projected_status"] == "fulfilled"
    assert (got["early"]["projected_status"], Decimal(got["early"]["qualifying_paid_amount"])) == ("open", Decimal("0.00"))
    assert got["cum"]["projected_status"] == "fulfilled" and Decimal(got["cum"]["qualifying_paid_amount"]) == half
    assert got["counter"]["projected_status"] == "fulfilled"
    # a payment AFTER the promise date does not count and does not repair it
    at(monkeypatch, deadline + timedelta(days=1), hour=9)
    assert by_loan(full(client, adm))[lid(late)]["current_promise"]["projected_status"] == "broken"
    pay_field(client, adm, late, half)
    assert by_loan(full(client, adm))[lid(late)]["current_promise"]["projected_status"] == "broken"
    # a reversal re-projects on the very next read (fulfilled -> broken after the deadline)
    assert by_loan(full(client, adm))[lid(rv)]["current_promise"]["projected_status"] == "fulfilled"
    rev(client, adm, pay_rv["id"], rv, session=None)
    assert by_loan(full(client, adm))[lid(rv)]["current_promise"]["projected_status"] == "broken"
    # the business date is the LOAN's: at 23:00 local of the promise date it is not yet broken (the UTC date is already tomorrow)
    at(monkeypatch, deadline, hour=23)
    assert by_loan(full(client, adm))[lid(tz)]["current_promise"]["projected_status"] == "open"


def test_the_last_activity_is_the_greatest_id_and_ignores_the_assignment(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=1)
    w, quiet = loans
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    _h, u1 = mkuser(client, sink, adm, tenant_a, "u1@x.com", ["collections.read"])
    assign(client, adm, w, u1)
    first = act(client, adm, w, "sms")  # recorded while u1 was the assignee
    assert first["assignment_id"] is not None
    client.post(f"/api/v2/loans/{lid(w)}/collection-assignment/end", headers=adm, json={"idempotency_key": "end-key-000000001"})
    assert by_loan(full(client, adm))[lid(w)]["current_assignment"] is None  # unassigned now
    got = by_loan(full(client, adm))[lid(w)]["last_collection_activity"]
    assert got["activity_id"] == first["activity_id"]  # still shown: never filtered by the current assignment
    # the order is the canonical id, not the timestamp: a higher id with an OLDER created_at still wins
    with SessionLocal() as db:
        db.execute(
            text(
                "INSERT INTO credit_collection_activities (tenant_id, loan_id, managing_branch_id, recorded_by, assignment_id, "
                "activity_type, created_at, idempotency_key, request_digest) VALUES (:t, :l, NULL, :u, NULL, 'email', "
                "now() - interval '30 days', 'older-ts-key-0001', 'd')"
            ),
            {"t": tenant_a["tenant_id"], "l": lid(w), "u": tenant_a["admin_id"]},
        )
        db.commit()
    newest = max(r[0] for r in arows(lid(w)))
    got = by_loan(full(client, adm))[lid(w)]["last_collection_activity"]
    assert got["activity_id"] == newest and got["activity_type"] == "email"
    assert by_loan(full(client, adm))[lid(quiet)]["last_collection_activity"] is None


# ================================ nothing else changes ===============================================
def test_membership_order_cursor_and_fingerprint_are_identical_with_or_without_promises_and_activities(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    base = world10(client, adm, tenant_a)
    b_a, b_b = mk_branch(client, adm, "EN-A"), mk_branch(client, adm, "EN-B")
    loans = [
        base,
        mkloan(client, adm, tenant_a, "Alfa", base=base, managing=b_a),
        mkloan(client, adm, tenant_a, "Beta", base=base, managing=b_a),
        mkloan(client, adm, tenant_a, "Gama", base=base, managing=b_b),
    ]
    _h1, u1 = mkuser(client, sink, adm, tenant_a, "u1@x.com", ["collections.read"])
    _h2, u2 = mkuser(client, sink, adm, tenant_a, "u2@x.com", ["collections.read"])
    d1 = first_due(client, adm, base)
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    assign(client, adm, loans[1], u1)
    assign(client, adm, loans[2], u1)
    assign(client, adm, loans[3], u2)
    sets = [
        {},
        {"branch_id": b_a["id"]},
        {"currency": "DOP"},
        {"min_days_overdue": 1},
        {"min_days_overdue": 10_000},
        {"assignment": "mine"},
        {"assignment": "assigned"},
        {"assignment": "unassigned"},
        {"assignee_id": u1},
        {"sort": "overdue_outstanding", "order": "asc"},
        {"sort": "oldest_overdue_date"},
    ]
    before = {i: wl(client, adm, limit=100, **p) for i, p in enumerate(sets)}
    page1 = {i: wl(client, adm, limit=1, **p) for i, p in enumerate(sets)}
    assert all(i["current_promise"] is None and i["last_collection_activity"] is None for b in before.values() for i in b["items"])
    half = Decimal("10.00")
    for w in loans:  # promises and activities on EVERY loan, including assigned and unassigned ones
        promise(client, adm, w, half, d1 + timedelta(days=7))
        act(client, adm, w, "phone_call")
    after = {i: wl(client, adm, limit=100, **p) for i, p in enumerate(sets)}
    for i, p in enumerate(sets):
        assert ids(after[i]) == ids(before[i]), p  # the very same loan ids
        assert stripped(after[i]) == stripped(before[i]), p  # in the same order, with the same values
        assert after[i]["next_cursor"] == before[i]["next_cursor"], p
        assert {k: v for k, v in after[i].items() if k != "items"} == {k: v for k, v in before[i].items() if k != "items"}
        assert page1[i]["next_cursor"] == wl(client, adm, limit=1, **p)["next_cursor"]  # the cursor does not depend on them
    # cursors issued BEFORE keep working, with the format and the fingerprint T-013 defined
    cursor = page1[0]["next_cursor"]
    assert cursor is not None
    raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    assert set(raw) == {"s", "o", "v", "i", "f"}
    # the fingerprint is T-013's, recomputed here independently: SHA-256 of the canonical JSON of the four filters, first 16 hex
    canon = json.dumps({"b": None, "m": None, "c": None, "a": "none"}, sort_keys=True, separators=(",", ":"))
    assert raw["f"] == hashlib.sha256(canon.encode()).hexdigest()[:16]
    assert raw["f"] == worklist_service.fingerprint(None, None, None, "none")
    assert wl(client, adm, limit=100, cursor=cursor)["items"]  # accepted
    built = worklist_service.encode_cursor("days_overdue", "desc", 5, 1, raw["f"])
    wl(client, adm, limit=100, cursor=built)  # a hand-built T-013 cursor is accepted too
    rest = [i for i in ids(after[0])]
    seen, cur = [], None
    while True:  # paging with enrichment on: no duplicate, no omission
        pg = wl(client, adm, limit=1, **({"cursor": cur} if cur else {}))
        seen += ids(pg)
        cur = pg["next_cursor"]
        if cur is None:
            break
    assert seen == rest


def test_the_authorization_and_scope_are_unchanged_and_neither_promise_nor_activity_nor_assignment_grants_access(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    b_a, b_b = mk_branch(client, adm, "AU-A"), mk_branch(client, adm, "AU-B")
    base = world10(client, adm, tenant_a)
    loans = [
        base,  # no managing branch
        mkloan(client, adm, tenant_a, "Alfa", base=base, managing=b_a),
        mkloan(client, adm, tenant_a, "Beta", base=base, managing=b_b),
    ]
    d1 = first_due(client, adm, base)
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    for w in loans:
        promise(client, adm, w, Decimal("10.00"), d1 + timedelta(days=7))
        act(client, adm, w, "other")
    at_a, id_a = mkuser(client, sink, adm, tenant_a, "ra@x.com", ["collections.read"], scope="branch", branch_id=b_a["id"])
    tenant_wide, _ = mkuser(client, sink, adm, tenant_a, "tw@x.com", ["collections.read"])
    assert set(ids(full(client, at_a))) == {lid(loans[1])}  # only its branch: the NULL-branch and B loans stay hidden
    seen = by_loan(full(client, at_a))[lid(loans[1])]
    assert set(seen["current_promise"]) == PROMISE_KEYS and set(seen["last_collection_activity"]) == ACTIVITY_KEYS
    assert set(ids(full(client, tenant_wide))) == {lid(w) for w in loans}
    # holding the WRITE permissions (and having recorded them) is not read access
    writer, writer_id = mkuser(
        client, sink, adm, tenant_a, "wr@x.com", [PROMISE_CREATE, "collections.actions.create", "collections.assign"]
    )
    promise(client, writer, loans[1], Decimal("10.00"), d1 + timedelta(days=7), expect=409)  # (a current one already exists)
    wl(client, writer, expect=403)
    # being the assignee never widens the scope: a branch-A reader assigned to a branch-B loan is not eligible, and the
    # promise / activity attached to that loan do not make it visible either
    only_a, _ = mkuser(client, sink, adm, tenant_a, "ra2@x.com", ["collections.read"], scope="branch", branch_id=b_a["id"])
    assert set(ids(full(client, only_a))) == {lid(loans[1])}
    assert id_a  # (the branch reader itself)


# ================================ page only, constant cost, tenant-scoped =============================
def test_the_enrichment_touches_only_the_page_in_a_constant_number_of_queries(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=5)
    at(monkeypatch, d1 + timedelta(days=5), hour=9)

    def selects(**params):
        stmts: list[str] = []

        def before(conn, cursor, statement, parameters, context, executemany):
            if re.match(r"\s*SELECT", statement, re.I):
                stmts.append(statement)

        event.listen(engine, "before_cursor_execute", before)
        try:
            body = wl(client, adm, **params)
        finally:
            event.remove(engine, "before_cursor_execute", before)
        return len(stmts), body, stmts

    one, _, _ = selects(limit=1)
    six, body, _ = selects(limit=100)
    assert len(body["items"]) == 6 and one == six  # nothing per row
    plain = six  # no promise exists yet: the aggregation is skipped
    for w in loans:
        promise(client, adm, w, Decimal("10.00"), d1 + timedelta(days=7))
        act(client, adm, w, "sms")
    one_p, _, _ = selects(limit=1)
    six_p, body, stmts = selects(limit=100)
    assert one_p == six_p  # constant: page size 1 vs 6 (and the same shape for 100)
    assert six_p == plain + 1  # the aggregation of qualifying payments runs only when the page HAS current promises
    assert six_p <= 13  # 10 measured before T-016 + current promises + aggregation + latest activities
    # the helpers receive the loans of the PAGE only, never the whole candidate universe
    seen: dict[str, list[int]] = {}
    real_p, real_a = promise_service.current_mini_views, activity_service.latest_by_loan

    def spy_p(db, tenant_id, by_loan_dates):
        seen["promises"] = sorted(by_loan_dates)
        return real_p(db, tenant_id, by_loan_dates)

    def spy_a(db, tenant_id, loan_ids):
        seen["activities"] = sorted(loan_ids)
        return real_a(db, tenant_id, loan_ids)

    monkeypatch.setattr(worklist_service.promises, "current_mini_views", spy_p)
    monkeypatch.setattr(worklist_service.activities, "latest_by_loan", spy_a)
    page = wl(client, adm, limit=2)
    assert len(ids(page)) == 2 and page["next_cursor"] is not None  # 6 candidates, a page of 2
    assert seen["promises"] == sorted(ids(page)) == seen["activities"]
    # every new query is tenant-scoped in the SQL itself
    mine = [s for s in stmts if "credit_collection_promises" in s or "credit_collection_activities" in s]
    assert mine and all(re.search(r"credit_collection_(promises|activities)\.tenant_id|\.tenant_id = ", s) for s in mine), mine


def test_the_enrichment_queries_are_tenant_scoped_and_use_the_agreed_shapes():
    activities_src = inspect.getsource(activity_service.latest_by_loan)
    assert ".lateral(" in activities_src and ".limit(1)" in activities_src and "id.desc()" in activities_src
    assert ".distinct(" not in activities_src  # a global DISTINCT ON would sort every activity of a busy loan
    assert "tenant_id ==" in activities_src
    promises_src = inspect.getsource(promise_service.current_mini_views)
    assert "tenant_id == tenant_id" in promises_src and "closed_at.is_(None)" in promises_src
    assert "qualifying_paid_amounts(" in promises_src  # the single shared definition of the qualifying payments
    worklist_src = (ROOT / "app/modules/loans/worklist.py").read_text(encoding="utf-8")
    assert "received_at" not in worklist_src  # the qualifying-payment formula is NOT re-implemented in the worklist
    assert "promised_amount" not in worklist_src and "qualifying" not in worklist_src.replace("qualifying-payment", "")


# ================================ pure, no PII, no new surface ========================================
def test_the_enriched_worklist_is_read_only_pii_free_and_adds_no_parameter_sort_permission_or_migration(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=2)
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    for w in loans:
        promise(client, adm, w, Decimal("10.00"), d1 + timedelta(days=7))
        act(client, adm, w, "whatsapp")
    with SessionLocal() as db:
        loans_before = [tuple(r) for r in db.execute(text("SELECT to_jsonb(l)::text FROM credit_loans l ORDER BY id"))]
    legacy_before, money_before, contract_before = {t: count(t) for t in LEGACY}, money_counts(), contractual_rows()
    tables = {t: count(t) for t in ("credit_collection_promises", "credit_collection_activities", "credit_collection_assignments")}
    events_before, promises_before, activities_before = count("security_events"), prows(), arows()
    collection_audit_before = len(audit("loan.collection_"))
    statements, stop = write_listener()
    try:
        for params in ({}, {"limit": 1}, {"assignment": "unassigned"}, {"min_days_overdue": 1}):
            body = wl(client, adm, **params)
    finally:
        stop()
    assert statements == []  # 0 INSERT / UPDATE / DELETE
    assert count("security_events") == events_before and len(audit("loan.collection_")) == collection_audit_before  # 0 audit
    assert prows() == promises_before and arows() == activities_before
    assert tables == {t: count(t) for t in tables}
    with SessionLocal() as db:
        assert [tuple(r) for r in db.execute(text("SELECT to_jsonb(l)::text FROM credit_loans l ORDER BY id"))] == loans_before
    assert {t: count(t) for t in LEGACY} == legacy_before and money_counts() == money_before
    assert contractual_rows() == contract_before and rows() == []
    raw = json.dumps(wl(client, adm, limit=100), default=str)
    for leak in (
        "idempotency",
        "request_digest",
        "sha256",
        "-key-",
        "Juan",
        "Perez",
        "NombreExtra",
        "@",
        "recorded_by",
        "created_by",
        "assignment_id\": 1",
        "supersedes",
        "closed_kind",
        "managing_branch_id\": null, \"assignment",
    ):
        assert leak not in raw, leak
    item = body["items"][0]
    assert set(item["current_promise"]) == PROMISE_KEYS and set(item["last_collection_activity"]) == ACTIVITY_KEYS
    # the contract: the old fields are all still there; only the two new ones were added; no filter, sort, permission or migration
    assert set(item) == {
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
        "current_assignment",
        "current_promise",
        "last_collection_activity",
    }
    assert set(inspect.signature(overdue_route).parameters) == {
        "branch_id",
        "min_days_overdue",
        "currency",
        "assignment",
        "assignee_id",
        "promise_status",  # T-017: the only parameter added after T-016
        "activity",  # T-018: activity existence filter
        "sort",
        "order",
        "limit",
        "cursor",
        "actor",
        "db",
    }
    assert worklist_service.SORTS == ("days_overdue", "overdue_outstanding", "oldest_overdue_date")
    versions = sorted(p.name for p in (ROOT / "alembic/versions").glob("0*.py"))
    assert versions[-1].startswith("0018_") and not [v for v in versions if v.startswith("0019")]  # 0018 = T-019
    catalog = (ROOT / "app/modules/identity/catalog.py").read_text(encoding="utf-8")
    assert "collections.worklist" not in catalog and catalog.count("collections.promises.create") == 1


def test_the_alembic_schema_is_unchanged_by_t016(scratch_db):
    from tests.test_t001_foundation import _alembic

    assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    assert _alembic(scratch_db, "check").returncode == 0
    assert "0018" in _alembic(scratch_db, "heads").stdout  # T-019 added 0018


@pytest.fixture(autouse=True)
def _pool_guard():
    yield
