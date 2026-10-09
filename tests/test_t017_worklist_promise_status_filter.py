"""T-017 Collection worklist ``promise_status`` filter tests (T017-*). PostgreSQL only.

ONE new membership filter: ``promise_status=open|fulfilled|broken|none`` over the CURRENT promise (T-015: ``closed_at IS NULL``),
decided before the ledger, the sort, the cursor and the page, with the exact T-015 projection (shared helpers), each loan's own
business date, the filter result reused as ``current_promise``, and a conditional ``p`` in the cursor fingerprint. No Activity
filter, sort, permission, migration, index or write.
"""

import base64
import hashlib
import inspect
import json
import random
import re
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo, available_timezones

from sqlalchemy import event, text

from app.core.db import SessionLocal, engine
from app.modules.loans import overdue as overdue_service
from app.modules.loans import payments as pay_service
from app.modules.loans import promises as promise_service
from app.modules.loans import reversals as rev_service
from app.modules.loans import service as loan_service
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
from tests.test_t005_credit_products import flow
from tests.test_t005_engine import rules
from tests.test_t006_origination import (  # noqa: F401
    _release_tracked_connections,
    count,
)
from tests.test_t008_payments import LEGACY, pay, schedule
from tests.test_t009_payment_reversal import audit, rev
from tests.test_t010_overdue_projection import contractual_rows, money_counts, world10, write_listener
from tests.test_t011_collection_worklist import W, first_due, ids, local, mkloan, wl
from tests.test_t012_collection_assignment import _fresh_pool, assign, mkuser, rows  # noqa: F401
from tests.test_t015_collection_promise import (
    CREATE as PROMISE_CREATE,
)
from tests.test_t015_collection_promise import (
    at,
    cancel,
    detail,
    pay_field,
    promise,
    replace,
    world,
)
from tests.test_t016_worklist_promise_activity_enrichment import PROMISE_KEYS, by_loan, lid

ROOT = Path(__file__).resolve().parent.parent
STATUSES = ("open", "fulfilled", "broken", "none")


def lids(loans):
    return {lid(w) for w in loans}


def member_ids(client, hdr, status=None, **params):
    return set(ids(wl(client, hdr, limit=100, **({"promise_status": status} if status else {}), **params)))


def buckets(client, hdr, **params):
    return {s: member_ids(client, hdr, s, **params) for s in STATUSES}


def clock_at(monkeypatch, instant: datetime):
    for mod in (pay_service, loan_service, rev_service, overdue_service, worklist_service, promise_service):
        monkeypatch.setattr(mod, "now_utc", lambda instant=instant: instant)


def sha16(parts: dict) -> str:  # an INDEPENDENT recomputation of the fingerprint
    canon = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def raw_cursor(cursor: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))


# ================================ exact semantics =====================================================
def test_each_status_is_the_exact_t015_projection_of_the_current_promise_and_none_means_no_current(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=7)
    never, op, ful, brk, cancelled, superseded, history, untouched = loans
    half = (due1 / 2).quantize(Decimal("0.01"))
    deadline = d1 + timedelta(days=7)
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    promise(client, adm, op, half, deadline)
    promise(client, adm, ful, half, deadline)
    promise(client, adm, brk, half, deadline)
    cancel(client, adm, cancelled, promise(client, adm, cancelled, half, deadline)["promise_id"])
    promise(client, adm, superseded, half, deadline)
    cancel(client, adm, superseded, replace(client, adm, superseded, half, deadline)["promise_id"])
    promise(client, adm, history, half, deadline)
    replace(client, adm, history, half, deadline)
    current_history = replace(client, adm, history, half, deadline)["promise_id"]  # history of three, one current (open)
    at(monkeypatch, d1 + timedelta(days=5), hour=10)
    pay_field(client, adm, ful, half)  # EXACTLY the promised amount: fulfilled uses >=
    at(monkeypatch, d1 + timedelta(days=6), hour=12)
    got = buckets(client, adm)
    assert got["none"] == lids([never, cancelled, superseded, untouched])  # never had one, cancelled-only, superseded-only
    assert got["open"] == lids([op, brk, history]) and got["fulfilled"] == lids([ful]) and got["broken"] == set()
    assert set().union(*got.values()) == lids(loans) and sum(len(v) for v in got.values()) == len(loans)  # a partition
    at(monkeypatch, d1 + timedelta(days=8), hour=12)  # the deadline has passed
    got = buckets(client, adm)
    assert got["broken"] == lids([op, brk, history]) and got["fulfilled"] == lids([ful]) and got["open"] == set()
    assert got["none"] == lids([never, cancelled, superseded, untouched])
    # each filtered row carries the very status it was filtered by (the projection is reused, never recomputed apart)
    for status, members in got.items():
        body = wl(client, adm, limit=100, promise_status=status)
        for item in body["items"]:
            if status == "none":
                assert item["current_promise"] is None
            else:
                cp = item["current_promise"]
                assert set(cp) == PROMISE_KEYS and cp["projected_status"] == status
                owner = next(w for w in loans if lid(w) == item["loan_id"])
                d = detail(client, adm, owner, cp["promise_id"])  # and it is the T-015 detail, field by field
                assert (d["projected_status"], d["qualifying_paid_amount"]) == (status, cp["qualifying_paid_amount"])
        assert {i["loan_id"] for i in body["items"]} == members
    assert by_loan(wl(client, adm, limit=100))[lid(history)]["current_promise"]["promise_id"] == current_history
    # the filtered rows equal the unfiltered rows of the same loans (nothing else moves)
    plain = {i["loan_id"]: i for i in wl(client, adm, limit=100)["items"]}
    for i in wl(client, adm, limit=100, promise_status="broken")["items"]:
        assert i == plain[i["loan_id"]]


def test_the_qualifying_payment_rules_decide_membership_exactly_as_t015_does(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=6)
    rv, early, late, cumulative, counter, fld, reversed_partial = loans
    half = (due1 / 2).quantize(Decimal("0.01"))
    quarter = (half / 2).quantize(Decimal("0.01"))
    deadline = d1 + timedelta(days=7)
    at(monkeypatch, d1 + timedelta(days=5), hour=8)
    pay_field(client, adm, early, half)  # received BEFORE the promise: never counts
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    for w, amount in ((rv, half), (early, half), (late, half), (cumulative, half), (counter, half), (fld, half), (reversed_partial, half)):
        promise(client, adm, w, amount, deadline)
    at(monkeypatch, d1 + timedelta(days=5), hour=10)
    pay_rv = pay_field(client, adm, rv, half)
    pay_field(client, adm, cumulative, quarter)
    pay_field(client, adm, cumulative, half - quarter)  # cumulative: two payments add up
    pay(client, adm, counter, f"{half:.2f}")  # counter counts
    pay_field(client, adm, fld, half)  # field counts
    p1 = pay_field(client, adm, reversed_partial, quarter)
    pay_field(client, adm, reversed_partial, quarter)
    at(monkeypatch, d1 + timedelta(days=6), hour=12)
    got = buckets(client, adm)
    assert got["fulfilled"] == lids([rv, cumulative, counter, fld])
    assert got["open"] == lids([early, late, reversed_partial])  # earlier payment excluded; partial: not enough yet
    rev(client, adm, p1["id"], reversed_partial, session=None)  # a reversed payment contributes 0
    assert lid(reversed_partial) in member_ids(client, adm, "open")
    # reversal BEFORE the deadline: fulfilled -> open on the very next read
    rev(client, adm, pay_rv["id"], rv, session=None)
    got = buckets(client, adm)
    assert lid(rv) in got["open"] and lid(rv) not in got["fulfilled"]
    # past the deadline: the late payment never counts nor repairs, and a reversal turns fulfilled into broken
    at(monkeypatch, deadline + timedelta(days=1), hour=9)
    pay_field(client, adm, late, half)
    got = buckets(client, adm)
    assert lid(late) in got["broken"] and lid(rv) in got["broken"] and lid(reversed_partial) in got["broken"]
    assert got["fulfilled"] == lids([cumulative, counter, fld])
    assert got["open"] == set()
    # equivalence with T-015, loan by loan
    for status, members in got.items():
        for w in loans:
            if lid(w) in members and status != "none":
                cp = by_loan(wl(client, adm, limit=100, promise_status=status))[lid(w)]["current_promise"]
                assert detail(client, adm, w, cp["promise_id"])["projected_status"] == status


def test_the_business_date_is_each_loans_own_and_the_sql_date_prefilters_never_drop_a_valid_loan(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    base = world10(client, adm, tenant_a)  # America/Santo_Domingo (UTC-4)

    def product(code, tz):
        cal = {
            "source": "product",
            "timezone": tz,
            "non_working_weekdays": [5, 6],
            "holidays": [],
            "adjustment": "keep_original",
            "delinquency_start_basis": "effective_due_date",
            "accrual_basis": "contractual_dates",
        }
        pr = SimpleNamespace(p=None, v=None)
        pr.p, pr.v = flow(client, adm, code, raw=rules(calendar=cal))
        return pr

    east = mkloan(client, adm, tenant_a, "Este", base=base, product=product("PRD-KI", "Pacific/Kiritimati"))  # UTC+14
    west = mkloan(client, adm, tenant_a, "Oeste", base=base, product=product("PRD-PP", "Pacific/Pago_Pago"))  # UTC-11
    loans = {lid(base): (base, "America/Santo_Domingo"), lid(east): (east, "Pacific/Kiritimati"), lid(west): (west, "Pacific/Pago_Pago")}
    info = {}
    for loan_id, (w, tz) in loans.items():
        d = first_due(client, adm, w)
        info[loan_id] = (tz, d, d + timedelta(days=2))
    anchor = min(d for _tz, d, _p in info.values()) - timedelta(days=3)
    at(monkeypatch, anchor, hour=12)
    for loan_id, (w, _tz) in loans.items():
        promise(client, adm, w, Decimal("10.00"), info[loan_id][2])
    lo, hi = min(p for *_x, p in info.values()) - timedelta(days=2), max(p for *_x, p in info.values()) + timedelta(days=2)
    instants, t = [], datetime(lo.year, lo.month, lo.day, tzinfo=UTC)
    while t.date() <= hi:
        instants.append(t)
        t += timedelta(hours=3)
    for _tz, d, pd in info.values():  # the exact local midnights of EACH timezone around the promise date
        for tzname in {x[0] for x in info.values()}:
            midnight = datetime(pd.year, pd.month, pd.day, tzinfo=ZoneInfo(tzname)) + timedelta(days=1)
            instants += [midnight.astimezone(UTC) - timedelta(minutes=1), midnight.astimezone(UTC)]
    seen = {"open": 0, "broken": 0}
    for now in instants:
        clock_at(monkeypatch, now)
        expected = {s: set() for s in STATUSES}
        for loan_id, (tz, due, pd) in info.items():
            today = now.astimezone(ZoneInfo(tz)).date()  # independent
            if today > due:  # overdue (the worklist stays an overdue list)
                expected["broken" if today > pd else "open"].add(loan_id)
        for status in ("open", "broken"):
            assert member_ids(client, adm, status) == expected[status], (status, now)
            seen[status] += len(expected[status])
        assert member_ids(client, adm, "fulfilled") == set() and member_ids(client, adm, "none") == set()
    assert seen["open"] and seen["broken"]


def test_the_date_prefilters_are_a_safe_superset_in_every_timezone_and_instant():
    rnd = random.Random(17)
    zones = sorted(available_timezones())
    checked = 0
    for _ in range(4000):
        tz = ZoneInfo(rnd.choice(zones))
        now = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=rnd.randrange(0, 60 * 24 * 400))
        today, utc_date = now.astimezone(tz).date(), now.date()
        promise_date = utc_date + timedelta(days=rnd.randrange(-4, 5))
        if today > promise_date:  # broken: the prefilter promise_date <= UTC date must keep it
            assert promise_date <= utc_date
        else:  # open: the prefilter promise_date >= UTC date - 1 must keep it
            assert promise_date >= utc_date - timedelta(days=1)
        checked += 1
    assert checked == 4000


# ================================ intersection, scope, overdue stays primary ==========================
def test_the_promise_filter_intersects_with_every_other_filter_and_never_widens_the_scope(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    base = world10(client, adm, tenant_a)
    b_a, b_b = mk_branch(client, adm, "PS-A"), mk_branch(client, adm, "PS-B")
    n = base
    a1 = mkloan(client, adm, tenant_a, "Alfa", base=base, managing=b_a)
    a2 = mkloan(client, adm, tenant_a, "Beta", base=base, managing=b_a)
    b1 = mkloan(client, adm, tenant_a, "Gama", base=base, managing=b_b)
    _h1, u1 = mkuser(client, sink, adm, tenant_a, "u1@x.com", ["collections.read"])
    _h2, u2 = mkuser(client, sink, adm, tenant_a, "u2@x.com", ["collections.read"])
    due1 = Decimal(str(schedule(client, adm, lid(base))[0]["total_due"]))
    d1 = first_due(client, adm, base)
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    assign(client, adm, a1, u1)
    assign(client, adm, a2, u1)
    assign(client, adm, b1, u2)
    half = (due1 / 2).quantize(Decimal("0.01"))
    for w in (n, a1, b1):
        promise(client, adm, w, half, d1 + timedelta(days=5))  # date = today: open now, broken tomorrow
    promise(client, adm, a2, half, d1 + timedelta(days=7))
    at(monkeypatch, d1 + timedelta(days=5), hour=10)
    pay_field(client, adm, b1, half)  # fulfilled
    at(monkeypatch, d1 + timedelta(days=6), hour=12)  # n and a1 are broken (date passed), a2 open, b1 fulfilled
    everything = lids([n, a1, a2, b1])
    brk = member_ids(client, adm, "broken")
    assert brk == lids([n, a1]) and member_ids(client, adm, "open") == lids([a2]) and member_ids(client, adm, "fulfilled") == lids([b1])
    for params in (
        {"branch_id": b_a["id"]},
        {"currency": "DOP"},
        {"currency": "USD"},
        {"min_days_overdue": 1},
        {"min_days_overdue": 10_000},
        {"assignment": "mine"},
        {"assignment": "assigned"},
        {"assignment": "unassigned"},
        {"assignee_id": u1},
        {"assignee_id": u2},
    ):
        other = member_ids(client, adm, None, **params)
        for status in STATUSES:
            assert member_ids(client, adm, status, **params) == other & buckets(client, adm)[status], (status, params)
    assert everything == set().union(*buckets(client, adm).values())
    # the scope is never widened: a branch-A reader sees only its loans, with or without the filter
    at_a, _ = mkuser(client, sink, adm, tenant_a, "ra@x.com", ["collections.read"], scope="branch", branch_id=b_a["id"])
    assert member_ids(client, at_a, "broken") == lids([a1])  # n (no managing branch) and b1 stay hidden
    assert member_ids(client, at_a, "none") == set() and member_ids(client, at_a, None) == lids([a1, a2])
    tenant_wide, _ = mkuser(client, sink, adm, tenant_a, "tw@x.com", ["collections.read"])
    assert member_ids(client, tenant_wide, "broken") == brk  # tenant-level read also reaches the NULL-branch loan
    writer, _ = mkuser(client, sink, adm, tenant_a, "wr@x.com", [PROMISE_CREATE, "collections.actions.create", "collections.assign"])
    wl(client, writer, expect=403, promise_status="broken")  # holding promise permissions is not read access


def test_the_worklist_stays_an_overdue_list_whatever_the_promise_says(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=1)
    paid_late, overdue_fulfilled = loans
    half = (due1 / 2).quantize(Decimal("0.01"))
    at(monkeypatch, d1 - timedelta(days=5), hour=9)
    promise(client, adm, paid_late, due1, d1 + timedelta(days=1))
    promise(client, adm, overdue_fulfilled, half, d1 + timedelta(days=7))
    at(monkeypatch, d1 + timedelta(days=3), hour=9)  # after the promise date of the first one
    pay_field(client, adm, paid_late, due1)  # late and complete: the loan is settled, the promise stays broken
    pay_field(client, adm, overdue_fulfilled, half)  # meets the promise; the loan still owes the rest: overdue
    at(monkeypatch, d1 + timedelta(days=4), hour=9)
    d = detail(client, adm, paid_late, promise_service_current(client, adm, paid_late))
    assert d["projected_status"] == "broken"  # the promise is broken (T-015)...
    assert lid(paid_late) not in member_ids(client, adm, "broken") | member_ids(client, adm, None)  # ...but no overdue debt
    assert member_ids(client, adm, "fulfilled") == lids([overdue_fulfilled])  # fulfilled, and the loan is still listed
    assert member_ids(client, adm, "broken") == set()


def promise_service_current(client, adm, w):
    return client.get(f"/api/v2/loans/{lid(w)}/collection-promises/current", headers=adm).json()["promise"]["promise_id"]


# ================================ the filter decides membership BEFORE the ledger, the sort and the page ========
def test_the_filter_is_applied_before_the_ledger_and_the_page_and_reuses_one_projection(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=11)  # 12 candidates
    half = (due1 / 2).quantize(Decimal("0.01"))
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    brokenish = loans[:5]
    for w in brokenish:
        promise(client, adm, w, half, d1 + timedelta(days=5))  # date = today: broken from tomorrow
    for w in loans[5:8]:
        promise(client, adm, w, half, d1 + timedelta(days=9))  # open
    at(monkeypatch, d1 + timedelta(days=6), hour=12)
    calls: dict[str, list] = {"ledger": [], "paid": [], "page_lookup": []}
    real_views, real_paid, real_page = worklist_service.ledger.views_many, promise_service.qualifying_paid_amounts, promise_service.current_mini_views

    def spy_views(db, loan_ids):
        calls["ledger"].append(sorted(loan_ids))
        return real_views(db, loan_ids)

    def spy_paid(db, tenant_id, promise_ids):
        calls["paid"].append(list(promise_ids))
        return real_paid(db, tenant_id, promise_ids)

    def spy_page(db, tenant_id, dates):
        calls["page_lookup"].append(sorted(dates))
        return real_page(db, tenant_id, dates)

    monkeypatch.setattr(worklist_service.ledger, "views_many", spy_views)
    monkeypatch.setattr(promise_service, "qualifying_paid_amounts", spy_paid)
    monkeypatch.setattr(worklist_service.promises, "current_mini_views", spy_page)
    for status, expected in (("broken", lids(brokenish)), ("open", lids(loans[5:8]))):
        for key in calls:
            calls[key].clear()
        body = wl(client, adm, limit=100, promise_status=status)
        assert set(ids(body)) == expected
        assert calls["ledger"] == [sorted(expected)]  # the ledger only sees the survivors of the promise filter
        assert len(calls["paid"]) == 1 and len(calls["paid"][0]) <= 8  # ONE aggregation, over the current promises only
        assert calls["page_lookup"] == []  # the page enrichment REUSES the filter's projection: no second lookup
    for key in calls:
        calls[key].clear()
    none_body = wl(client, adm, limit=100, promise_status="none")
    assert set(ids(none_body)) == lids(loans[8:]) and calls["ledger"] == [sorted(lids(loans[8:]))]
    assert calls["paid"] == [] and calls["page_lookup"] == []  # none needs neither payments nor promises
    assert all(i["current_promise"] is None for i in none_body["items"])
    for key in calls:
        calls[key].clear()
    wl(client, adm, limit=100)  # no filter: the T-016 page enrichment, unchanged
    assert calls["ledger"] == [sorted(lids(loans))] and len(calls["page_lookup"]) == 1
    # the filter is applied BEFORE the page: pages are full, complete, without duplicates or holes
    seen, cursor, sizes = [], None, []
    while True:
        page = wl(client, adm, limit=2, promise_status="broken", **({"cursor": cursor} if cursor else {}))
        sizes.append(len(page["items"]))
        seen += ids(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert sizes == [2, 2, 1] and set(seen) == lids(brokenish) and len(set(seen)) == 5
    assert seen == ids(wl(client, adm, limit=100, promise_status="broken"))  # same order as the unpaged list


def test_the_query_count_is_constant_whatever_the_page_length(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=7)
    half = (due1 / 2).quantize(Decimal("0.01"))
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    for w in loans[:2]:
        promise(client, adm, w, half, d1 + timedelta(days=5))  # broken from tomorrow
    for w in loans[2:4]:
        promise(client, adm, w, half, d1 + timedelta(days=9))  # open
    promise(client, adm, loans[4], half, d1 + timedelta(days=9))  # fulfilled below
    at(monkeypatch, d1 + timedelta(days=5), hour=10)
    pay_field(client, adm, loans[4], half)
    at(monkeypatch, d1 + timedelta(days=6), hour=12)
    assert all(member_ids(client, adm, s) for s in STATUSES)  # every filter has rows: the counts below are the full path

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
        return len(stmts), body

    counts = {}
    for status in (None, "none", "open", "fulfilled", "broken"):
        extra = {"promise_status": status} if status else {}
        one, _ = selects(limit=1, **extra)
        many, body = selects(limit=100, **extra)
        assert one == many, status  # nothing per row, nothing per page
        counts[status] = many
    print("QUERY_COUNTS", counts)
    assert max(counts.values()) <= 14
    assert counts["none"] <= counts[None]  # none needs no promise lookup at all


# ================================ cursor =============================================================
def test_the_fingerprint_gains_p_only_when_the_filter_is_requested_and_cursors_never_cross(
    client, sink, tenant_a, monkeypatch
):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=5)
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    for w in loans[:4]:
        promise(client, adm, w, Decimal("10.00"), d1 + timedelta(days=5))
    at(monkeypatch, d1 + timedelta(days=6), hour=12)
    # without the filter: byte-for-byte the T-013 / T-016 fingerprint (an old cursor stays valid)
    plain = wl(client, adm, limit=1)
    cur = raw_cursor(plain["next_cursor"])
    assert set(cur) == {"s", "o", "v", "i", "f"}
    assert cur["f"] == sha16({"b": None, "m": None, "c": None, "a": "none"})
    assert worklist_service.fingerprint(None, None, None, "none") == sha16({"b": None, "m": None, "c": None, "a": "none"})
    assert worklist_service.fingerprint(None, None, None, "none", None) == cur["f"]
    assert wl(client, adm, limit=100, cursor=plain["next_cursor"])["items"]
    old = worklist_service.encode_cursor("days_overdue", "desc", 5, 1, sha16({"b": None, "m": None, "c": None, "a": "none"}))
    wl(client, adm, limit=100, cursor=old)  # a cursor built exactly as before T-017
    for status in STATUSES:  # with the filter: `p` is part of f, the shape is unchanged
        body = wl(client, adm, limit=1, promise_status=status)
        if body["next_cursor"] is None:
            continue
        c = raw_cursor(body["next_cursor"])
        assert set(c) == {"s", "o", "v", "i", "f"}
        assert c["f"] == sha16({"b": None, "m": None, "c": None, "a": "none", "p": status}) != cur["f"]
    # filtered cursor reused elsewhere: invalid
    broken_cursor = wl(client, adm, limit=1, promise_status="broken")["next_cursor"]
    assert broken_cursor
    wl(client, adm, expect=422, limit=1, cursor=broken_cursor)  # without the filter
    for other in ("open", "fulfilled", "none"):
        wl(client, adm, expect=422, limit=1, cursor=broken_cursor, promise_status=other)
    wl(client, adm, expect=422, limit=1, cursor=plain["next_cursor"], promise_status="broken")  # an unfiltered one with the filter
    wl(client, adm, expect=422, limit=1, cursor=broken_cursor, promise_status="broken", min_days_overdue=1)
    assert wl(client, adm, limit=1, cursor=broken_cursor, promise_status="broken")["items"]  # the right one works


def test_the_parameter_is_one_exact_enum_value_and_adds_no_other_surface(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=3)
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    for w in loans[:2]:
        promise(client, adm, w, Decimal("10.00"), d1 + timedelta(days=5))  # broken from tomorrow
    promise(client, adm, loans[2], Decimal("10.00"), d1 + timedelta(days=9))  # still open
    at(monkeypatch, d1 + timedelta(days=6), hour=12)
    assert member_ids(client, adm, "open") and member_ids(client, adm, "broken")
    for bad in ("cancelled", "superseded", "Broken", "OPEN", "any", "", "open,broken", "open|broken", "broken,", "true"):
        r = client.get(W, headers=adm, params={"promise_status": bad})
        assert r.status_code == 422, bad
    r = client.get(W, headers=adm, params=[("promise_status", "open"), ("promise_status", "broken")])  # FastAPI convention
    assert r.status_code in (200, 422)
    if r.status_code == 200:  # never a union
        union = member_ids(client, adm, "open") | member_ids(client, adm, "broken")
        assert set(ids(r.json())) in (member_ids(client, adm, "broken"), member_ids(client, adm, "open")) and set(ids(r.json())) != union
    assert set(inspect.signature(overdue_route).parameters) == {
        "branch_id",
        "min_days_overdue",
        "currency",
        "assignment",
        "assignee_id",
        "promise_status",
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
    assert versions[-1].startswith("0020_") and not [v for v in versions if v.startswith("0021")]  # 0018 = T-019, 0019 = T-020, 0020 = T-021
    catalog = (ROOT / "app/modules/identity/catalog.py").read_text(encoding="utf-8")
    assert "collections.promise_status" not in catalog and catalog.count("collections.promises.create") == 1
    src = (ROOT / "app/modules/loans/worklist.py").read_text(encoding="utf-8")
    assert "activity_type" not in src  # T-018 adds only an existence filter, never an Activity type filter
    for formula in ("received_at", "promised_amount", "closed_kind", "is_broken", "broken_at", "fulfilled_at"):
        assert formula not in src, formula  # the T-015 rule lives only in promises.py; nothing persisted


def test_the_shared_helper_takes_one_array_parameter_so_large_candidate_sets_are_cheap(client, sink, tenant_a):
    admin_headers(client, tenant_a)
    with SessionLocal() as db:
        started = time.perf_counter()
        assert promise_service.qualifying_paid_amounts(db, tenant_a["tenant_id"], range(1, 60_001)) == {}
        assert promise_service.qualifying_paid_amounts(db, tenant_a["tenant_id"], []) == {}
        assert time.perf_counter() - started < 10
    src = inspect.getsource(promise_service.qualifying_paid_amounts)
    assert "any_(" in src and ".in_(" not in src  # one array bind, not tens of thousands of parameters


# ================================ pure, no PII ========================================================
def test_the_filtered_worklist_is_read_only_pii_free_and_adds_no_write_audit_or_state(client, sink, tenant_a, monkeypatch):
    adm = admin_headers(client, tenant_a)
    loans, d1, due1 = world(client, adm, tenant_a, extra=3)
    at(monkeypatch, d1 + timedelta(days=5), hour=9)
    for w in loans[:3]:
        promise(client, adm, w, Decimal("10.00"), d1 + timedelta(days=5))
    at(monkeypatch, d1 + timedelta(days=6), hour=12)
    with SessionLocal() as db:
        loans_before = [tuple(r) for r in db.execute(text("SELECT to_jsonb(l)::text FROM credit_loans l ORDER BY id"))]
    legacy_before, money_before, contract_before = {t: count(t) for t in LEGACY}, money_counts(), contractual_rows()
    tables = {t: count(t) for t in ("credit_collection_promises", "credit_collection_activities", "credit_collection_assignments")}
    events_before, audit_before = count("security_events"), len(audit("loan.collection_"))
    statements, stop = write_listener()
    try:
        for status in STATUSES:
            body = wl(client, adm, limit=100, promise_status=status)
            wl(client, adm, limit=1, promise_status=status)
    finally:
        stop()
    assert statements == []  # 0 INSERT / UPDATE / DELETE
    assert count("security_events") == events_before and len(audit("loan.collection_")) == audit_before
    assert tables == {t: count(t) for t in tables} and rows() == []
    with SessionLocal() as db:
        assert [tuple(r) for r in db.execute(text("SELECT to_jsonb(l)::text FROM credit_loans l ORDER BY id"))] == loans_before
    assert {t: count(t) for t in LEGACY} == legacy_before and money_counts() == money_before
    assert contractual_rows() == contract_before
    raw = json.dumps(wl(client, adm, limit=100, promise_status="broken"), default=str)
    for leak in ("idempotency", "request_digest", "sha256", "-key-", "Juan", "Perez", "NombreExtra", "@", "recorded_by", "created_by"):
        assert leak not in raw, leak
    assert body is not None and set(wl(client, adm, limit=100, promise_status="broken")["items"][0]) == {
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


def test_the_alembic_schema_is_unchanged_by_t017(scratch_db):
    from tests.test_t001_foundation import _alembic

    assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    assert _alembic(scratch_db, "check").returncode == 0
    assert "0020" in _alembic(scratch_db, "heads").stdout  # T-019 / T-020 / T-021 added 0018 / 0019 / 0020
