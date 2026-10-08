"""T-018 Collection worklist ``activity`` existence filter tests (T018-*). PostgreSQL only.

ONE new membership filter: ``activity=has_activity|no_activity`` = at least one / no T-014 activity of the loan, ever (any type,
recorder, date or assignment snapshot), decided with an exact ``EXISTS`` / ``NOT EXISTS`` in the candidate SQL before the promise
stage, the ledger, the sort, the cursor and the page; a conditional ``ae`` in the cursor fingerprint; ``no_activity`` rows carry
``last_collection_activity`` null from the membership itself. No type, recency, sort, permission, migration, index or write.
"""

import base64
import hashlib
import inspect
import json
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import event, text

from app.core.db import SessionLocal, engine
from app.modules.loans import activities as activity_service
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
from tests.test_t009_payment_reversal import audit
from tests.test_t010_overdue_projection import contractual_rows, money_counts, world10, write_listener
from tests.test_t011_collection_worklist import W, first_due, ids, mkloan, wl
from tests.test_t012_collection_assignment import _fresh_pool, assign, end, mkuser, rows  # noqa: F401
from tests.test_t014_collection_activity import act, arows
from tests.test_t015_collection_promise import CREATE as PROMISE_CREATE
from tests.test_t015_collection_promise import at, pay_field, promise, world
from tests.test_t016_worklist_promise_activity_enrichment import ACTIVITY_KEYS, by_loan, lid

ROOT = Path(__file__).resolve().parent.parent
VALUES = ("has_activity", "no_activity")
PLAIN = {"b": None, "m": None, "c": None, "a": "none"}


def lids(loans):
    return {lid(w) for w in loans}


def member_ids(client, hdr, activity=None, **params):
    return set(ids(wl(client, hdr, limit=100, **({"activity": activity} if activity else {}), **params)))


def sha16(parts: dict) -> str:  # an INDEPENDENT recomputation of the fingerprint
    canon = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def raw_cursor(cursor: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))


def bulk_activities(tenant, loan_id, n, activity_type="phone_call"):
    """``n`` activities of one loan straight in SQL (a hot loan); the loan has no managing branch nor open assignment."""
    with SessionLocal() as db:
        db.execute(
            text(
                "INSERT INTO credit_collection_activities (tenant_id, loan_id, managing_branch_id, recorded_by, assignment_id, "
                "activity_type, created_at, idempotency_key, request_digest) SELECT :t, :l, NULL, :u, NULL, :ty, now(), "
                "'hot-key-' || :l || '-' || g, 'd' FROM generate_series(1, :n) g"
            ),
            {"t": tenant["tenant_id"], "l": loan_id, "u": tenant["admin_id"], "ty": activity_type, "n": n},
        )
        db.commit()


def insert_activity_now(tenant, loan_id, k):
    """A concurrent insert from ANOTHER transaction (committed), as a second user would do."""
    with SessionLocal() as db:
        db.execute(
            text(
                "INSERT INTO credit_collection_activities (tenant_id, loan_id, managing_branch_id, recorded_by, assignment_id, "
                "activity_type, created_at, idempotency_key, request_digest) VALUES (:t, :l, NULL, :u, NULL, 'sms', now(), :k, 'd')"
            ),
            {"t": tenant["tenant_id"], "l": loan_id, "u": tenant["admin_id"], "k": k},
        )
        db.commit()
        return db.execute(text("SELECT max(id) FROM credit_collection_activities WHERE loan_id = :l"), {"l": loan_id}).scalar()


# ================================ exact semantics =====================================================
def test_has_and_no_activity_are_exact_historical_existence_whatever_the_type_count_recorder_or_assignment(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    loans, d1, _due1 = world(client, adm, tenant_a, extra=8)
    never, one, many, hot, old_asg, moved_recorder, no_contact, other, other_types = loans
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    rec_h, rec = mkuser(client, sink, adm, tenant_a, "rec@x.com", ["collections.read", "collections.actions.create"])
    _h2, u2 = mkuser(client, sink, adm, tenant_a, "u2@x.com", ["collections.read"])
    act(client, adm, one, "email")  # A02 one activity
    for t in ("phone_call", "whatsapp", "sms", "in_person_visit", "office_visit"):  # A03 many
        act(client, adm, many, t)
    bulk_activities(tenant_a, lid(hot), 5_000)  # A04 a hot loan
    assign(client, adm, old_asg, rec)
    first = act(client, adm, old_asg, "sms")  # recorded under an assignment that is later closed
    assert first["assignment_id"] is not None
    end(client, adm, old_asg)
    assign(client, adm, moved_recorder, rec)
    act(client, rec_h, moved_recorder, "phone_call")  # the recorder is the assignee...
    end(client, adm, moved_recorder)
    assign(client, adm, moved_recorder, u2)  # ...and no longer is
    act(client, adm, no_contact, "no_contact")  # "no contact" is still an activity
    act(client, adm, other, "other")
    act(client, adm, other_types, "office_visit")
    at(monkeypatch, d1 + timedelta(days=6), hour=12)
    has, no = member_ids(client, adm, "has_activity"), member_ids(client, adm, "no_activity")
    assert no == lids([never])  # A01
    assert has == lids(loans) - lids([never])  # A02-A06, type irrelevant, no_contact / other count
    assert has | no == member_ids(client, adm) and not has & no  # a partition of the unfiltered list
    # 5,000 activities are ONE membership answer: one row, never multiplied
    body = wl(client, adm, limit=100, activity="has_activity")
    assert ids(body).count(lid(hot)) == 1 and len(ids(body)) == len(set(ids(body)))
    # has_activity rows carry their latest activity (greatest id), even one recorded under a closed assignment
    rows_ = by_loan(body)
    for w in loans[1:]:
        got = rows_[lid(w)]["last_collection_activity"]
        assert set(got) == ACTIVITY_KEYS and got["activity_id"] == max(r[0] for r in arows(lid(w)))
    assert rows_[lid(old_asg)]["last_collection_activity"]["activity_id"] == first["activity_id"]
    assert rows_[lid(no_contact)]["last_collection_activity"]["activity_type"] == "no_contact"
    # no_activity rows always carry null; the filtered rows equal the unfiltered rows of the same loans
    assert all(i["last_collection_activity"] is None for i in wl(client, adm, limit=100, activity="no_activity")["items"])
    plain = {i["loan_id"]: i for i in wl(client, adm, limit=100)["items"]}
    for value in VALUES:
        for i in wl(client, adm, limit=100, activity=value)["items"]:
            assert i == plain[i["loan_id"]]
    # no filter: exactly the pre-T-018 list (A22)
    assert set(plain) == lids(loans)


# ================================ intersection, scope, overdue stays primary ==========================
def test_the_activity_filter_intersects_with_every_other_filter_and_never_widens_the_scope(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    base = world10(client, adm, tenant_a)
    b_a, b_b = mk_branch(client, adm, "AE-A"), mk_branch(client, adm, "AE-B")
    a1 = mkloan(client, adm, tenant_a, "Alfa", base=base, managing=b_a)
    a2 = mkloan(client, adm, tenant_a, "Beta", base=base, managing=b_a)
    b1 = mkloan(client, adm, tenant_a, "Gama", base=base, managing=b_b)
    b2 = mkloan(client, adm, tenant_a, "Delta", base=base, managing=b_b)
    everything = [base, a1, a2, b1, b2]
    d1 = first_due(client, adm, base)
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    _h1, u1 = mkuser(client, sink, adm, tenant_a, "u1@x.com", ["collections.read"])
    _h2, u2 = mkuser(client, sink, adm, tenant_a, "u2@x.com", ["collections.read"])
    me, u3 = mkuser(client, sink, adm, tenant_a, "me@x.com", ["collections.read"])
    assign(client, adm, a1, u1)
    assign(client, adm, b1, u2)
    assign(client, adm, b2, u3)
    for w in (base, a1, b2):
        act(client, adm, w, "phone_call")
    at(monkeypatch, d1 + timedelta(days=6), hour=12)
    has, no = member_ids(client, adm, "has_activity"), member_ids(client, adm, "no_activity")
    assert has == lids([base, a1, b2]) and no == lids([a2, b1])
    for params in (
        {"branch_id": b_a["id"]},  # A07 / A08
        {"branch_id": b_b["id"]},
        {"currency": "DOP"},
        {"currency": "USD"},
        {"min_days_overdue": 1},
        {"min_days_overdue": 10_000},
        {"assignment": "assigned"},
        {"assignment": "unassigned"},  # A10
        {"assignee_id": u1},
        {"assignee_id": u2},
        {"assignee_id": u3},
    ):
        other = member_ids(client, adm, None, **params)
        assert member_ids(client, adm, "has_activity", **params) == other & has, params
        assert member_ids(client, adm, "no_activity", **params) == other & no, params
    assert member_ids(client, me, "has_activity", assignment="mine") == lids([b2])  # A09
    assert member_ids(client, me, "no_activity", assignment="mine") == set()
    assert member_ids(client, me, "no_activity", assignment="unassigned") == lids([a2])
    assert member_ids(client, adm, "no_activity", assignment="unassigned") == lids([a2])
    # the scope is never widened (A18): a branch-A reader sees only its loans, with or without the filter
    at_a, _ = mkuser(client, sink, adm, tenant_a, "ra@x.com", ["collections.read"], scope="branch", branch_id=b_a["id"])
    assert member_ids(client, at_a, "has_activity") == lids([a1]) and member_ids(client, at_a, "no_activity") == lids([a2])
    assert member_ids(client, at_a, None) == lids([a1, a2])  # base (NULL managing branch) stays hidden: A19
    tenant_wide, _ = mkuser(client, sink, adm, tenant_a, "tw@x.com", ["collections.read"])
    assert member_ids(client, tenant_wide, "has_activity") == has  # tenant-level read also reaches the NULL-branch loan
    # recording activities (or promises, or assigning) is never read access (A18, D14)
    writer, _ = mkuser(client, sink, adm, tenant_a, "wr@x.com", ["collections.actions.create", PROMISE_CREATE, "collections.assign"])
    for value in VALUES:
        wl(client, writer, expect=403, activity=value)
    assert lids(everything) == has | no


def test_the_activity_snapshots_never_decide_scope_or_membership(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    base = world10(client, adm, tenant_a)
    b_a, b_b = mk_branch(client, adm, "SN-A"), mk_branch(client, adm, "SN-B")
    w = mkloan(client, adm, tenant_a, "Snap", base=base, managing=b_a)
    d1 = first_due(client, adm, base)
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    _h, u1 = mkuser(client, sink, adm, tenant_a, "su1@x.com", ["collections.read"])
    assign(client, adm, w, u1)
    a = act(client, adm, w, "sms")  # snapshots: managing branch A, the open assignment of u1
    assert a["managing_branch_id"] == b_a["id"] and a["assignment_id"] is not None
    end(client, adm, w)  # the snapshot assignment is closed now
    at(monkeypatch, d1 + timedelta(days=6), hour=12)
    reader_a, _ = mkuser(client, sink, adm, tenant_a, "sa@x.com", ["collections.read"], scope="branch", branch_id=b_a["id"])
    reader_b, _ = mkuser(client, sink, adm, tenant_a, "sb@x.com", ["collections.read"], scope="branch", branch_id=b_b["id"])
    assert lid(w) in member_ids(client, reader_a, "has_activity")  # the closed snapshot assignment still counts (A05)
    assert lid(w) not in member_ids(client, reader_b, "has_activity") | member_ids(client, reader_b, "no_activity")
    assert lid(w) in member_ids(client, adm, "has_activity", assignment="unassigned")  # current assignment, not the snapshot
    assert lid(w) not in member_ids(client, adm, "has_activity", assignee_id=u1)
    stage = inspect.getsource(worklist_service._candidates).split("if activity is not None")[1].split("if promise_status")[0]
    assert "activities.restrict_candidates(" in stage  # the ONE shared T-014 definition, before the promise stage
    helper = inspect.getsource(activity_service.restrict_candidates).split("Pure read.")[1]
    for column in ("assignment_id", "managing_branch_id", "activity_type", "created_at", "recorded_by"):
        assert column not in helper, column  # A20: only tenant + loan decide existence
    assert "CreditCollectionActivity" not in inspect.getsource(worklist_service)  # the worklist never touches the model


def test_promise_and_activity_filters_intersect_and_the_worklist_stays_an_overdue_list(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=7)
    brk_act, brk_quiet, ful_act, ful_quiet, open_act, nopromise_act, nopromise_quiet, settled_brk = loans
    half = (due1 / 2).quantize(Decimal("0.01"))
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    for w in (brk_act, brk_quiet):
        promise(client, adm, w, half, d1 + timedelta(days=5))  # broken from tomorrow
    for w in (ful_act, ful_quiet, open_act):
        promise(client, adm, w, half, d1 + timedelta(days=9))
    promise(client, adm, settled_brk, half, d1 + timedelta(days=5))
    for w in (brk_act, ful_act, open_act, nopromise_act, settled_brk):
        act(client, adm, w, "phone_call")
    at(monkeypatch, d1 + timedelta(days=5), hour=10)
    pay_field(client, adm, ful_act, half)
    pay_field(client, adm, ful_quiet, half)
    pay_field(client, adm, settled_brk, due1)  # no overdue debt any more (A21)
    at(monkeypatch, d1 + timedelta(days=6), hour=12)
    combos = {
        ("broken", "has_activity"): lids([brk_act]),  # A11
        ("broken", "no_activity"): lids([brk_quiet]),  # A12
        ("fulfilled", "has_activity"): lids([ful_act]),
        ("fulfilled", "no_activity"): lids([ful_quiet]),
        ("open", "has_activity"): lids([open_act]),
        ("open", "no_activity"): set(),
        ("none", "has_activity"): lids([nopromise_act]),
        ("none", "no_activity"): lids([nopromise_quiet]),
    }
    for (status, value), expected in combos.items():
        got = member_ids(client, adm, value, promise_status=status)
        assert got == expected, (status, value)
        # AND, never OR and never one replacing the other
        assert got == member_ids(client, adm, value) & set(ids(wl(client, adm, limit=100, promise_status=status)))
    assert lid(settled_brk) not in member_ids(client, adm, "has_activity", promise_status="broken")  # overdue stays primary
    for item in wl(client, adm, limit=100, promise_status="broken", activity="has_activity")["items"]:
        assert item["current_promise"]["projected_status"] == "broken" and item["last_collection_activity"] is not None
    for item in wl(client, adm, limit=100, promise_status="broken", activity="no_activity")["items"]:
        assert item["current_promise"]["projected_status"] == "broken" and item["last_collection_activity"] is None


# ================================ before the ledger and the page; result reuse ========================
def test_the_filter_is_applied_before_the_ledger_and_the_page_and_no_activity_reuses_its_membership(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=11)  # 12 candidates
    half = (due1 / 2).quantize(Decimal("0.01"))
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    active = loans[:5]
    for w in active:
        act(client, adm, w, "whatsapp")
    for w in (loans[0], loans[1], loans[6]):
        promise(client, adm, w, half, d1 + timedelta(days=5))  # broken from tomorrow
    at(monkeypatch, d1 + timedelta(days=6), hour=12)
    calls: dict[str, list] = {"ledger": [], "latest": []}
    real_views, real_latest = worklist_service.ledger.views_many, activity_service.latest_by_loan

    def spy_views(db, loan_ids):
        calls["ledger"].append(sorted(loan_ids))
        return real_views(db, loan_ids)

    def spy_latest(db, tenant_id, loan_ids):
        calls["latest"].append(sorted(loan_ids))
        return real_latest(db, tenant_id, loan_ids)

    monkeypatch.setattr(worklist_service.ledger, "views_many", spy_views)
    monkeypatch.setattr(worklist_service.activities, "latest_by_loan", spy_latest)

    def run(**params):
        for k in calls:
            calls[k].clear()
        return wl(client, adm, limit=100, **params)

    body = run(activity="has_activity")
    assert set(ids(body)) == lids(active) and calls["ledger"] == [sorted(lids(active))]  # only the survivors
    assert calls["latest"] == [sorted(lids(active))]  # has_activity: the T-016 PAGE lookup, nothing candidate-wide
    body = run(activity="no_activity")
    quiet = lids(loans) - lids(active)
    assert set(ids(body)) == quiet and calls["ledger"] == [sorted(quiet)]
    assert calls["latest"] == []  # no_activity: the membership is reused, the lookup is skipped
    assert all(i["last_collection_activity"] is None for i in body["items"])
    body = run(activity="has_activity", promise_status="broken")
    assert set(ids(body)) == lids(loans[:2]) and calls["ledger"] == [sorted(lids(loans[:2]))]  # the intersection only
    body = run(activity="no_activity", promise_status="broken")
    assert set(ids(body)) == lids([loans[6]]) and calls["ledger"] == [[lid(loans[6])]] and calls["latest"] == []
    run()  # no filter: the T-016 behaviour, unchanged
    assert calls["ledger"] == [sorted(lids(loans))] and len(calls["latest"]) == 1
    # before the page: full pages, no holes, no duplicates, same order as the unpaged list
    for value, expected in (("has_activity", lids(active)), ("no_activity", quiet)):
        seen, cursor, sizes = [], None, []
        while True:
            page = wl(client, adm, limit=2, activity=value, **({"cursor": cursor} if cursor else {}))
            sizes.append(len(page["items"]))
            seen += ids(page)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert all(s == 2 for s in sizes[:-1]) and set(seen) == expected and len(seen) == len(expected)
        assert seen == ids(wl(client, adm, limit=100, activity=value))
    # the sort semantics are unchanged: same relative order as the unfiltered list
    plain = ids(wl(client, adm, limit=100, sort="oldest_overdue_date", order="asc"))
    assert ids(wl(client, adm, limit=100, sort="oldest_overdue_date", order="asc", activity="has_activity")) == [
        i for i in plain if i in lids(active)
    ]


def test_a_concurrent_activity_never_contradicts_the_response_and_shows_on_the_next_request(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    loans, d1, _due1 = world(client, adm, tenant_a, extra=2)
    quiet, busy, other = loans
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    old = act(client, adm, busy, "email")
    at(monkeypatch, d1 + timedelta(days=6), hour=12)
    real_candidates = worklist_service._candidates
    inserted: dict[str, int] = {}

    def racing(*args, **kwargs):  # another transaction commits an activity right after the membership was decided
        out = real_candidates(*args, **kwargs)
        target = quiet if args[8] == "no_activity" else busy  # _candidates(db, actor, scope, b, c, now, a, p, activity)
        inserted[lid(target)] = insert_activity_now(tenant_a, lid(target), f"race-key-{lid(target)}-{len(inserted)}")
        return out

    monkeypatch.setattr(worklist_service, "_candidates", racing)
    body = wl(client, adm, limit=100, activity="no_activity")  # A16
    assert lid(quiet) in ids(body) and by_loan(body)[lid(quiet)]["last_collection_activity"] is None
    body = wl(client, adm, limit=100, activity="has_activity")  # D16: a NEWER concurrent latest is acceptable
    got = by_loan(body)[lid(busy)]["last_collection_activity"]
    assert got is not None and got["activity_id"] in (old["activity_id"], inserted[lid(busy)])
    monkeypatch.setattr(worklist_service, "_candidates", real_candidates)
    assert lid(quiet) not in member_ids(client, adm, "no_activity")  # the next request sees it (A17: live, no snapshot)
    assert lid(quiet) in member_ids(client, adm, "has_activity") and lid(other) in member_ids(client, adm, "no_activity")


def test_the_query_count_is_constant_whatever_the_page_length(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=7)
    half = (due1 / 2).quantize(Decimal("0.01"))
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    for w in loans[:4]:
        promise(client, adm, w, half, d1 + timedelta(days=5))  # broken from tomorrow
    for w in (loans[0], loans[1], loans[5], loans[6]):
        act(client, adm, w, "sms")
    at(monkeypatch, d1 + timedelta(days=6), hour=12)

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
        assert body["items"]  # the full path
        return len(stmts)

    counts = {}
    for name, extra in (
        ("none", {}),
        ("has", {"activity": "has_activity"}),
        ("no", {"activity": "no_activity"}),
        ("broken+has", {"activity": "has_activity", "promise_status": "broken"}),
        ("broken+no", {"activity": "no_activity", "promise_status": "broken"}),
    ):
        one, many = selects(limit=1, **extra), selects(limit=100, **extra)
        assert one == many, name  # nothing per row, nothing per page
        counts[name] = many
    print("QUERY_COUNTS", counts)
    assert counts["has"] == counts["none"]  # the EXISTS lives inside the candidate SELECT
    assert counts["no"] == counts["none"] - 1  # no_activity skips the latest-activity SELECT
    assert counts["broken+no"] == counts["broken+has"] - 1


# ================================ cursor =============================================================
def test_the_fingerprint_gains_ae_only_when_the_filter_is_requested_and_cursors_never_cross(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    loans, d1, _due1 = world(client, adm, tenant_a, extra=7)
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    for w in loans[:4]:
        act(client, adm, w, "phone_call")
    for w in (loans[0], loans[1], loans[4], loans[5]):
        promise(client, adm, w, Decimal("10.00"), d1 + timedelta(days=5))
    at(monkeypatch, d1 + timedelta(days=6), hour=12)
    # without the filter: byte-for-byte the T-017 fingerprint; an old cursor stays valid
    plain = wl(client, adm, limit=1)
    cur = raw_cursor(plain["next_cursor"])
    assert set(cur) == {"s", "o", "v", "i", "f"} and cur["f"] == sha16(PLAIN)
    assert worklist_service.fingerprint(None, None, None, "none") == sha16(PLAIN)
    assert worklist_service.fingerprint(None, None, None, "none", None, None) == sha16(PLAIN)
    assert worklist_service.fingerprint(None, None, None, "none", "broken") == sha16(PLAIN | {"p": "broken"})
    old = worklist_service.encode_cursor("days_overdue", "desc", 5, 1, sha16(PLAIN))  # built exactly as before T-018
    assert wl(client, adm, limit=100, cursor=old) is not None
    assert wl(client, adm, limit=100, cursor=plain["next_cursor"])["items"]
    cursors = {}
    for value in VALUES:  # with the filter: `ae` is part of f, the shape is unchanged
        body = wl(client, adm, limit=1, activity=value)
        c = raw_cursor(body["next_cursor"])
        assert set(c) == {"s", "o", "v", "i", "f"}
        assert c["f"] == sha16(PLAIN | {"ae": value}) != cur["f"]
        cursors[value] = body["next_cursor"]
        assert wl(client, adm, limit=1, cursor=body["next_cursor"], activity=value)["items"]  # the right one works
    for value, cursor in cursors.items():
        wl(client, adm, expect=422, limit=1, cursor=cursor)  # without the filter
        wl(client, adm, expect=422, limit=1, cursor=cursor, activity=[v for v in VALUES if v != value][0])  # the other
        wl(client, adm, expect=422, limit=1, cursor=cursor, activity=value, min_days_overdue=1)
    for value in VALUES:
        wl(client, adm, expect=422, limit=1, cursor=plain["next_cursor"], activity=value)  # unfiltered one with the filter
    # p and ae bind together
    both = wl(client, adm, limit=1, activity="has_activity", promise_status="broken")
    c = raw_cursor(both["next_cursor"])
    assert c["f"] == sha16(PLAIN | {"p": "broken", "ae": "has_activity"})
    assert wl(client, adm, limit=1, cursor=both["next_cursor"], activity="has_activity", promise_status="broken")["items"]
    wl(client, adm, expect=422, limit=1, cursor=both["next_cursor"], activity="has_activity")  # p dropped
    wl(client, adm, expect=422, limit=1, cursor=both["next_cursor"], promise_status="broken")  # ae dropped
    wl(client, adm, expect=422, limit=1, cursor=both["next_cursor"], activity="no_activity", promise_status="broken")
    wl(client, adm, expect=422, limit=1, cursor=both["next_cursor"], activity="has_activity", promise_status="open")
    r = client.get(W, headers=adm, params={"cursor": both["next_cursor"], "activity": "no_activity", "promise_status": "broken"})
    assert r.status_code == 422 and "invalid_cursor" in r.text


def test_the_parameter_is_one_exact_enum_value_and_adds_no_other_surface(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, _due1 = world(client, adm, tenant_a, extra=2)
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    act(client, adm, loans[0], "sms")
    at(monkeypatch, d1 + timedelta(days=6), hour=12)
    assert member_ids(client, adm, "has_activity") and member_ids(client, adm, "no_activity")
    for bad in (
        "has",
        "none",
        "no",
        "phone_call",
        "HAS_ACTIVITY",
        "Has_Activity",
        "has_activity,no_activity",
        "has_activity|no_activity",
        "has_activity ",
        "",
        "true",
        "1",
    ):
        r = client.get(W, headers=adm, params={"activity": bad})
        assert r.status_code == 422, bad
    r = client.get(W, headers=adm, params=[("activity", "has_activity"), ("activity", "no_activity")])  # FastAPI convention
    assert r.status_code in (200, 422)
    if r.status_code == 200:  # never a union of both states
        assert set(ids(r.json())) in (member_ids(client, adm, "has_activity"), member_ids(client, adm, "no_activity"))
    for absent in ("activity_type", "last_activity_type", "no_activity_since", "inactive_days", "contacted_recently", "has_activity"):
        assert client.get(W, headers=adm, params={absent: "x"}).status_code == 200  # unknown: ignored, never a filter
        assert set(ids(wl(client, adm, limit=100, **{absent: "phone_call"}))) == member_ids(client, adm)
    for bad_sort in ("last_activity_at", "activity_type", "activity_count"):
        wl(client, adm, expect=422, sort=bad_sort)
    assert set(inspect.signature(overdue_route).parameters) == {
        "branch_id",
        "min_days_overdue",
        "currency",
        "assignment",
        "assignee_id",
        "promise_status",
        "activity",  # T-018: the only parameter added after T-017
        "sort",
        "order",
        "limit",
        "cursor",
        "actor",
        "db",
    }
    assert worklist_service.SORTS == ("days_overdue", "overdue_outstanding", "oldest_overdue_date")
    versions = sorted(p.name for p in (ROOT / "alembic/versions").glob("0*.py"))
    assert versions[-1].startswith("0019_") and not [v for v in versions if v.startswith("0020")]  # 0018 = T-019, 0019 = T-020
    catalog = (ROOT / "app/modules/identity/catalog.py").read_text(encoding="utf-8")
    assert "collections.activity" not in catalog.replace("collections.actions", "") and "has_activity" not in catalog
    models = (ROOT / "app/modules/loans/models.py").read_text(encoding="utf-8")
    for persisted in ("last_activity_id", "last_activity_at", "has_activity", "activity_count"):
        assert persisted not in models, persisted  # no persisted activity state
    assert models.count('Index("ix_credit_collection_activities_') == 1  # no new index


def test_the_existence_predicates_are_exact_tenant_scoped_exists_and_not_exists(client, sink, tenant_a):
    admin_headers(client, tenant_a)
    from datetime import UTC, datetime

    from sqlalchemy.dialects import postgresql

    from app.modules.identity.authorization import Grant, Principal

    actor = Principal(user_id=1, tenant_id=tenant_a["tenant_id"], person_id=None, session_id=1, grants=(Grant("collections.read", "tenant"),))
    captured: list[str] = []

    class Grab:
        def execute(self, stmt):
            captured.append(str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})))

            class R:
                @staticmethod
                def all():
                    return []

            return R()

    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    for value in (None, "has_activity", "no_activity"):
        worklist_service._candidates(Grab(), actor, None, None, None, now, "none", None, value)
    plain, has, no = captured
    assert "credit_collection_activities" not in plain  # no filter: the pre-T-018 statement
    tid = tenant_a["tenant_id"]
    for sql, neg in ((has, False), (no, True)):
        assert sql.count("credit_collection_activities") == 3  # ONE EXISTS subquery: FROM + the two predicates
        assert ("NOT (EXISTS (SELECT *" in sql) == neg and "EXISTS (SELECT *" in sql
        assert f"credit_collection_activities.tenant_id = {tid}" in sql
        assert "credit_collection_activities.loan_id = credit_loans.id" in sql
        for banned in ("count(", "GROUP BY", "DISTINCT", "ORDER BY", "row_number", "LATERAL", "activity_type", "assignment_id"):
            assert banned not in sql.split("credit_collection_activities", 1)[1], banned


# ================================ pure, no PII ========================================================
def test_the_filtered_worklist_is_read_only_pii_free_and_adds_no_write_audit_or_state(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, _due1 = world(client, adm, tenant_a, extra=3)
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    for w in loans[:2]:
        act(client, adm, w, "phone_call")
    promise(client, adm, loans[0], Decimal("10.00"), d1 + timedelta(days=5))
    at(monkeypatch, d1 + timedelta(days=6), hour=12)
    with SessionLocal() as db:
        loans_before = [tuple(r) for r in db.execute(text("SELECT to_jsonb(l)::text FROM credit_loans l ORDER BY id"))]
    legacy_before, money_before, contract_before = {t: count(t) for t in LEGACY}, money_counts(), contractual_rows()
    tables = {t: count(t) for t in ("credit_collection_promises", "credit_collection_activities", "credit_collection_assignments")}
    events_before, audit_before, activities_before = count("security_events"), len(audit("loan.collection_")), arows()
    statements, stop = write_listener()
    try:
        for value in VALUES:
            wl(client, adm, limit=100, activity=value)
            wl(client, adm, limit=1, activity=value, promise_status="broken")
    finally:
        stop()
    assert statements == []  # 0 INSERT / UPDATE / DELETE
    assert count("security_events") == events_before and len(audit("loan.collection_")) == audit_before  # 0 audit
    assert tables == {t: count(t) for t in tables} and rows() == [] and arows() == activities_before
    with SessionLocal() as db:
        assert [tuple(r) for r in db.execute(text("SELECT to_jsonb(l)::text FROM credit_loans l ORDER BY id"))] == loans_before
    assert {t: count(t) for t in LEGACY} == legacy_before and money_counts() == money_before
    assert contractual_rows() == contract_before
    raw = json.dumps(wl(client, adm, limit=100, activity="has_activity"), default=str)
    for leak in ("idempotency", "request_digest", "sha256", "-key-", "Juan", "Perez", "NombreExtra", "@", "recorded_by", "created_by"):
        assert leak not in raw, leak
    for value in VALUES:
        for item in wl(client, adm, limit=100, activity=value)["items"]:
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


def test_the_alembic_schema_is_unchanged_by_t018(scratch_db):
    from tests.test_t001_foundation import _alembic

    assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    assert _alembic(scratch_db, "check").returncode == 0
    assert "0019" in _alembic(scratch_db, "heads").stdout  # T-019 / T-020 added 0018 / 0019
