"""T-005 fix 02: real PostgreSQL concurrency between publication and direct changes to a draft.

Ordering is controlled deterministically (open transactions, an event hook inside publish, and polling
``pg_stat_activity`` until the contender is *actually blocked on a lock*) — never with a sleep as the only sync.
"""

import threading
import time
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.db import SessionLocal, engine
from app.modules.credit import service
from app.modules.credit.models import CreditProductVersion
from app.modules.credit.rules import _canon, compute_rules_hash, parse_rules
from tests import pg_env  # noqa: F401  (must precede app imports)
from tests.test_t002_identity import (  # noqa: F401  (fixtures + helpers shared with the T-002 suite)
    admin_headers,
    client,
    fresh_db,
    sink,
    tenant_a,
)
from tests.test_t005_credit_products import SIM, P, mk_product, mk_version, today
from tests.test_t005_engine import rules

HOLIDAYS = ["2026-03-20", "2026-04-01"]
BLOCKED_PUBLISH = "%FROM credit_product_versions%FOR UPDATE%"  # publish waiting for the version row lock
BLOCKED_CURRENCY = "%UPDATE credit_product_currencies%"  # currency write waiting inside the guard trigger
BLOCKED_TENANT_CURRENCY_LOCK = "%FROM tenant_currencies%FOR SHARE%"  # publish waiting for the currency row
BLOCKED_DISABLE = "%UPDATE tenant_currencies%"  # a disable waiting for publish
BLOCKED_RULES = "%UPDATE credit_product_versions SET rules%"  # direct rules write waiting for the row lock


def wait_blocked(pattern: str, timeout: float = 20.0) -> None:
    """Poll until some backend with a matching statement is waiting on a LOCK (condition-based, bounded)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as c:
            n = c.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND query ILIKE :p"
                ),
                {"p": pattern},
            ).scalar()
        if n:
            return
        time.sleep(0.02)
    raise AssertionError(f"nobody became blocked on {pattern}")


def draft(client, adm, code, raw=None, validate=True, cur=None):
    p = mk_product(client, adm, code)
    default = rules(calendar=rules()["calendar"] | {"holidays": HOLIDAYS})
    v = mk_version(client, adm, p["id"], raw=raw or default, cur=cur)
    if validate:
        assert client.post(f"{P}/{p['id']}/versions/{v['id']}/validate", headers=adm).json()["valid"] is True
    return p, v


def publish_in_thread(client, adm, p, v, out: dict):
    def run():
        row = client.get(f"{P}/{p['id']}/versions/{v['id']}", headers=adm).json()["row_version"]
        out["resp"] = client.post(
            f"{P}/{p['id']}/versions/{v['id']}/publish",
            headers=adm,
            json={"row_version": row, "effective_from": str(today())},
        )

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def raw_write_in_thread(sql: str, params: dict, out: dict):
    def run():
        with engine.connect() as c:
            try:
                c.execute(text(sql), params)
                c.commit()
                out["result"] = "ok"
            except DBAPIError as exc:
                c.rollback()
                out["result"] = str(exc.orig)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


_TRACKED: list = []


def tracked_connect():
    """A raw connection that is ALWAYS rolled back and closed at teardown, even when an assertion fails while it
    holds a row lock (otherwise the schema drop of the next fixture would wait on it forever)."""
    conn = engine.connect()
    _TRACKED.append(conn)
    return conn


@pytest.fixture(autouse=True)
def _release_tracked_connections(client):
    # depends on `client` so it is torn down BEFORE it: the TestClient waits for in-flight request threads,
    # which may be blocked behind the very lock this releases
    yield
    for conn in _TRACKED:
        try:
            conn.rollback()
            conn.close()
        except Exception:  # already closed by the test
            pass
    _TRACKED.clear()


@pytest.fixture()
def hold_publish(monkeypatch):
    """Pause the publish transaction right after it took its locks and before it reads rules/currencies' validation."""
    locked, release, armed = threading.Event(), threading.Event(), threading.Event()
    original = service._run_validation

    def hooked(*args, **kwargs):
        if armed.is_set():  # only the publish under test pauses; earlier validate calls pass through
            locked.set()
            assert release.wait(30), "test never released publish"
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_run_validation", hooked)
    return locked, release, armed


def assert_published_coherent(client, adm, p, v, *, expected_published=1):
    """Hash/snapshot/rules/currencies of a published version describe exactly one and the same contract."""
    base = f"{P}/{p['id']}/versions/{v['id']}"
    snap = client.get(f"{base}/snapshot", headers=adm).json()
    assert snap["hash_verified"] is True
    with SessionLocal() as db:
        currencies = service._currency_rows(db, v["id"])
    sim = client.post(f"{base}/simulate", headers=adm, json=SIM | {"currency": currencies[0]["code"]})
    assert sim.status_code == 200, sim.text  # simulated in the version's own currency
    with SessionLocal() as db:
        row = db.get(CreditProductVersion, v["id"])
        currencies = service._currency_rows(db, v["id"])
        assert row.status == "published" and row.rules_hash == snap["rules_hash"]
        assert compute_rules_hash(parse_rules(row.rules), currencies) == row.rules_hash  # stored columns
        assert compute_rules_hash(row.snapshot["rules"], row.snapshot["currencies"]) == row.rules_hash  # snapshot
        assert row.snapshot["rules"] == _canon(parse_rules(row.rules))  # same rules
        assert row.snapshot["currencies"] == currencies  # same currencies
        published = db.query(CreditProductVersion).filter_by(product_id=p["id"], status="published").count()
    assert published == expected_published
    return snap, currencies


def revalidate_and_publish(client, adm, p, v):
    assert client.post(f"{P}/{p['id']}/versions/{v['id']}/validate", headers=adm).json()["valid"] is True
    row = client.get(f"{P}/{p['id']}/versions/{v['id']}", headers=adm).json()["row_version"]
    r = client.post(
        f"{P}/{p['id']}/versions/{v['id']}/publish",
        headers=adm,
        json={"row_version": row, "effective_from": str(today())},
    )
    assert r.status_code == 200, r.text


# ============================== TEST 1 - currency mutation vs publish ==================================
def test_currency_change_wins_the_lock_first_publish_waits_and_uses_the_final_content(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    p, v = draft(client, adm, "RACE-C1")
    a = tracked_connect()
    a.execute(text("UPDATE credit_product_currencies SET max_amount = 400000 WHERE version_id = :v"), {"v": v["id"]})
    out: dict = {}
    t = publish_in_thread(client, adm, p, v, out)
    wait_blocked(BLOCKED_PUBLISH)  # publish is parked on the version row lock held through the currency guard
    assert "resp" not in out  # it did not slip through while the change was uncommitted
    a.commit()
    a.close()
    t.join(30)
    assert out["resp"].status_code == 422  # it READ the new content: the earlier validation no longer matches it
    assert client.get(f"{P}/{p['id']}/versions/{v['id']}", headers=adm).json()["status"] == "draft"
    revalidate_and_publish(client, adm, p, v)
    snap, currencies = assert_published_coherent(client, adm, p, v)
    assert [Decimal(c["max_amount"]) for c in snap["snapshot"]["currencies"]] == [Decimal(400000)]  # final content
    assert Decimal(currencies[0]["max_amount"]) == Decimal(400000)


def test_publish_wins_the_lock_first_currency_change_waits_and_is_then_rejected(client, tenant_a, hold_publish):
    locked, release, armed = hold_publish
    adm = admin_headers(client, tenant_a)
    p, v = draft(client, adm, "RACE-C2")
    out_pub: dict = {}
    armed.set()
    t_pub = publish_in_thread(client, adm, p, v, out_pub)
    assert locked.wait(30)  # publish owns the version row now
    out_a: dict = {}
    t_a = raw_write_in_thread(
        "UPDATE credit_product_currencies SET max_amount = 400000 WHERE version_id = :v", {"v": v["id"]}, out_a
    )
    wait_blocked(BLOCKED_CURRENCY)  # the guard trigger is waiting for publish, not reading a stale 'draft'
    assert "result" not in out_a
    release.set()
    t_pub.join(30)
    t_a.join(30)
    assert out_pub["resp"].status_code == 200, out_pub["resp"].text
    assert "immutable" in out_a["result"]
    snap, currencies = assert_published_coherent(client, adm, p, v)
    assert Decimal(currencies[0]["max_amount"]) == Decimal(500000)  # the late write changed nothing


# ============================== TEST 2 - rules mutation vs publish =====================================
def test_rules_change_wins_first_hash_neutral_change_is_published_as_final_content(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    p, v = draft(client, adm, "RACE-R1")
    reordered = '["2026-04-01", "2026-03-20"]'  # same logical rules, different stored array order
    a = tracked_connect()
    a.execute(
        text(
            "UPDATE credit_product_versions SET rules = jsonb_set(rules, '{calendar,holidays}', CAST(:h AS jsonb)) WHERE id = :v"
        ),
        {"h": reordered, "v": v["id"]},
    )
    out: dict = {}
    t = publish_in_thread(client, adm, p, v, out)
    wait_blocked(BLOCKED_PUBLISH)
    a.commit()
    a.close()
    t.join(30)
    assert out["resp"].status_code == 200, out["resp"].text  # same hash: the validation still applies
    with SessionLocal() as db:
        stored = db.get(CreditProductVersion, v["id"]).rules["calendar"]["holidays"]
    assert stored == ["2026-04-01", "2026-03-20"]  # publish saw and froze the FINAL stored content
    assert_published_coherent(client, adm, p, v)


def test_rules_change_wins_first_content_change_forces_revalidation(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    p, v = draft(client, adm, "RACE-R2")
    a = tracked_connect()
    a.execute(
        text(
            "UPDATE credit_product_versions SET rules = jsonb_set(rules, '{method,rate,value}', '\"24\"') WHERE id = :v"
        ),
        {"v": v["id"]},
    )
    out: dict = {}
    t = publish_in_thread(client, adm, p, v, out)
    wait_blocked(BLOCKED_PUBLISH)
    a.commit()
    a.close()
    t.join(30)
    assert out["resp"].status_code == 422  # publish read the new rate; the old validation is void
    revalidate_and_publish(client, adm, p, v)
    snap, _ = assert_published_coherent(client, adm, p, v)
    assert snap["snapshot"]["rules"]["method"]["rate"]["value"] == "24"


def test_publish_wins_first_later_rules_update_waits_and_is_rejected(client, tenant_a, hold_publish):
    locked, release, armed = hold_publish
    adm = admin_headers(client, tenant_a)
    p, v = draft(client, adm, "RACE-R3")
    out_pub: dict = {}
    armed.set()
    t_pub = publish_in_thread(client, adm, p, v, out_pub)
    assert locked.wait(30)
    out_a: dict = {}
    t_a = raw_write_in_thread(
        "UPDATE credit_product_versions SET rules = jsonb_set(rules, '{method,rate,value}', '\"99\"') WHERE id = :v",
        {"v": v["id"]},
        out_a,
    )
    wait_blocked(BLOCKED_RULES)
    release.set()
    t_pub.join(30)
    t_a.join(30)
    assert out_pub["resp"].status_code == 200, out_pub["resp"].text
    assert "immutable" in out_a["result"]
    snap, _ = assert_published_coherent(client, adm, p, v)
    assert snap["snapshot"]["rules"]["method"]["rate"]["value"] == "12"  # the late rate never landed


# ============================== TEST 4 - no deadlocks ==================================================
def test_opposite_direction_currency_moves_between_drafts_never_deadlock(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    assert client.post("/api/v2/tenant/currencies", headers=adm, json={"code": "USD"}).status_code == 201
    assert client.post("/api/v2/tenant/currencies", headers=adm, json={"code": "EUR"}).status_code == 201
    p = mk_product(client, adm, "RACE-D")
    lim = lambda code: {"code": code, "min_amount": "10", "max_amount": "99"}  # noqa: E731
    v1 = mk_version(client, adm, p["id"], cur=[lim("USD")])
    v2 = mk_version(client, adm, p["id"], cur=[lim("EUR")])
    ids = {"v1": v1["id"], "v2": v2["id"]}
    for i in range(25):
        barrier = threading.Barrier(2)
        out_a, out_b = {}, {}

        def mover(sql, out, barrier=barrier):
            def run():
                with engine.connect() as c:
                    barrier.wait(10)
                    try:
                        c.execute(text(sql), ids)
                        c.commit()
                        out["result"] = "ok"
                    except DBAPIError as exc:
                        c.rollback()
                        out["result"] = str(exc.orig)

            return threading.Thread(target=run, daemon=True)

        ta = mover(
            "UPDATE credit_product_currencies SET version_id = :v2 WHERE version_id = :v1 AND currency_code = 'USD'",
            out_a,
        )
        tb = mover(
            "UPDATE credit_product_currencies SET version_id = :v1 WHERE version_id = :v2 AND currency_code = 'EUR'",
            out_b,
        )
        ta.start(), tb.start()
        ta.join(30), tb.join(30)
        assert (out_a["result"], out_b["result"]) == ("ok", "ok"), (i, out_a, out_b)  # no deadlock, no failure
        with engine.begin() as c:  # swap back for the next round
            c.execute(text("UPDATE credit_product_currencies SET version_id = :v1 WHERE currency_code = 'USD'"), ids)
            c.execute(text("UPDATE credit_product_currencies SET version_id = :v2 WHERE currency_code = 'EUR'"), ids)


def test_repeated_publish_vs_currency_writes_is_never_corrupt_and_never_deadlocks(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    outcomes = {"published": 0, "draft": 0}
    for i in range(10):
        p, v = draft(client, adm, f"RACE-S{i}")
        stop = threading.Event()
        errors: list[str] = []

        def hammer(stop=stop, errors=errors, version_id=v["id"]):
            limit = 450000
            while not stop.is_set():
                with engine.connect() as c:
                    try:
                        limit += 1
                        c.execute(
                            text("UPDATE credit_product_currencies SET max_amount = :m WHERE version_id = :v"),
                            {"m": limit, "v": version_id},
                        )
                        c.commit()
                    except DBAPIError as exc:
                        c.rollback()
                        if "immutable" not in str(exc.orig):
                            errors.append(str(exc.orig))  # anything but the guard (e.g. a deadlock) is a failure
                        return

        t = threading.Thread(target=hammer, daemon=True)
        t.start()
        out: dict = {}
        publish_in_thread(client, adm, p, v, out).join(30)
        stop.set()
        t.join(30)
        assert not errors, errors
        status = out["resp"].status_code
        assert status in (200, 422), out["resp"].text  # published, or refused as stale - never a 500/deadlock
        with SessionLocal() as db:
            state = db.get(CreditProductVersion, v["id"]).status
        if state == "published":
            assert status == 200
            assert_published_coherent(client, adm, p, v)
            outcomes["published"] += 1
        else:
            assert status == 422
            outcomes["draft"] += 1
    assert sum(outcomes.values()) == 10


# ============================== fix 03 - publish vs tenant currency enable/disable =====================
USD = [{"code": "USD", "min_amount": "10", "max_amount": "99999"}]
DISABLE_USD = "UPDATE tenant_currencies SET disabled_at = now() WHERE tenant_id = :t AND currency_code = 'USD'"


def usd_draft(client, adm, code):
    assert client.post("/api/v2/tenant/currencies", headers=adm, json={"code": "USD"}).status_code == 201
    return draft(client, adm, code, cur=USD)


def test_disable_wins_the_currency_lock_first_publish_waits_then_is_refused(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    p, v = usd_draft(client, adm, "RACE-T1")  # validated while USD is enabled
    a = tracked_connect()
    a.execute(text(DISABLE_USD), {"t": tenant_a["tenant_id"]})  # open, uncommitted
    out: dict = {}
    t = publish_in_thread(client, adm, p, v, out)
    wait_blocked(BLOCKED_TENANT_CURRENCY_LOCK)  # publish is parked on the tenant_currencies row
    assert "resp" not in out
    a.commit()
    a.close()
    t.join(30)
    resp = out["resp"]
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "product_validation_failed"
    assert any(i["code"] == "currency_not_enabled" for i in resp.json()["error"]["details"])
    got = client.get(f"{P}/{p['id']}/versions/{v['id']}", headers=adm).json()
    assert got["status"] == "draft" and got["rules_hash"] is None and got["effective_from"] is None
    assert client.get(f"{P}/{p['id']}/versions/{v['id']}/snapshot", headers=adm).status_code == 409  # no snapshot
    with SessionLocal() as db:
        row = db.get(CreditProductVersion, v["id"])
        assert row.status == "draft" and row.snapshot is None and row.rules_hash is None


def test_publish_wins_the_currency_lock_first_disable_waits_and_history_is_unchanged(client, tenant_a, hold_publish):
    locked, release, armed = hold_publish
    adm = admin_headers(client, tenant_a)
    p, v = usd_draft(client, adm, "RACE-T2")
    out_pub: dict = {}
    armed.set()
    t_pub = publish_in_thread(client, adm, p, v, out_pub)
    assert locked.wait(30)  # publish owns product, version and the USD enablement row
    out_a: dict = {}
    t_a = raw_write_in_thread(DISABLE_USD, {"t": tenant_a["tenant_id"]}, out_a)
    wait_blocked(BLOCKED_DISABLE)
    assert "result" not in out_a  # the disable waits for the publication
    release.set()
    t_pub.join(30)
    t_a.join(30)
    assert out_pub["resp"].status_code == 200, out_pub["resp"].text
    assert out_a["result"] == "ok"  # it completes afterwards: that is legitimate
    base = f"{P}/{p['id']}/versions/{v['id']}"
    before = client.get(f"{base}/snapshot", headers=adm).json()
    with SessionLocal() as db:
        disabled = db.execute(text("SELECT disabled_at IS NOT NULL FROM tenant_currencies WHERE currency_code = 'USD'"))
        assert disabled.scalar()
    snap, currencies = assert_published_coherent(client, adm, p, v)  # hash verified, simulation still works
    assert snap == before and currencies[0]["code"] == "USD"
    got = client.get(base, headers=adm).json()  # the historical version stays queryable, rules and hash untouched
    assert got["status"] == "published" and got["rules_hash"] == before["rules_hash"]
    assert client.get(f"{P}/{p['id']}/effective", headers=adm).json()["id"] == v["id"]


def test_sequential_disable_after_validate_still_blocks_publish(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    p, v = usd_draft(client, adm, "RACE-T3")
    assert client.delete("/api/v2/tenant/currencies/USD", headers=adm).status_code == 204
    row = client.get(f"{P}/{p['id']}/versions/{v['id']}", headers=adm).json()["row_version"]
    r = client.post(
        f"{P}/{p['id']}/versions/{v['id']}/publish",
        headers=adm,
        json={"row_version": row, "effective_from": str(today())},
    )
    assert r.status_code == 422 and any(i["code"] == "currency_not_enabled" for i in r.json()["error"]["details"])


def test_publish_vs_disable_stress_never_500_deadlock_or_stale_publication(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    assert client.post("/api/v2/tenant/currencies", headers=adm, json={"code": "USD"}).status_code == 201
    wins = {"publish_first": 0, "disable_first": 0}
    for i in range(12):
        p, v = draft(client, adm, f"RACE-X{i}", cur=USD)
        barrier = threading.Barrier(2)
        out_a: dict = {}
        out_b: dict = {}

        def disable(out=out_a, barrier=barrier):
            with engine.connect() as c:
                barrier.wait(10)
                try:
                    c.execute(text(DISABLE_USD), {"t": tenant_a["tenant_id"]})
                    c.commit()
                    out["result"] = "ok"
                except DBAPIError as exc:
                    c.rollback()
                    out["result"] = str(exc.orig)

        def publish(out=out_b, barrier=barrier, p=p, v=v):
            barrier.wait(10)
            row = client.get(f"{P}/{p['id']}/versions/{v['id']}", headers=adm).json()["row_version"]
            out["resp"] = client.post(
                f"{P}/{p['id']}/versions/{v['id']}/publish",
                headers=adm,
                json={"row_version": row, "effective_from": str(today())},
            )

        ts = [threading.Thread(target=disable, daemon=True), threading.Thread(target=publish, daemon=True)]
        [t.start() for t in ts]
        [t.join(60) for t in ts]
        assert out_a["result"] == "ok", out_a  # never a deadlock / serialization error
        status = out_b["resp"].status_code
        assert status in (200, 422), out_b["resp"].text  # never a 500
        with SessionLocal() as db:
            row = db.get(CreditProductVersion, v["id"])
            state, snapshot, rules_hash = row.status, row.snapshot, row.rules_hash
        if status == 200:
            assert state == "published"
            wins["publish_first"] += 1
            assert_published_coherent(client, adm, p, v, expected_published=1)
        else:
            assert state == "draft" and snapshot is None and rules_hash is None
            assert any(d["code"] == "currency_not_enabled" for d in out_b["resp"].json()["error"]["details"])
            wins["disable_first"] += 1
        with engine.begin() as c:  # re-enable for the next round
            c.execute(text("UPDATE tenant_currencies SET disabled_at = NULL WHERE currency_code = 'USD'"))
    assert sum(wins.values()) == 12
