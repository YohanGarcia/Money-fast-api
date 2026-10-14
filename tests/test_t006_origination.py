"""T-006 Credit Origination tests (T006-*). PostgreSQL only.

application -> evaluation -> decision/approval -> formalization (READY_FOR_DISBURSEMENT). No money ever moves.
"""

import re
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from app.core.db import SessionLocal, engine
from app.modules.identity.models import SecurityEvent
from app.modules.origination.models import (
    CreditApplication,
    CreditApplicationSubmission,
    CreditDecision,
)
from tests import pg_env  # noqa: F401  (must precede app imports)
from tests.test_t001_foundation import _alembic, scratch_db  # noqa: F401
from tests.test_t002_identity import (  # noqa: F401  (fixtures + helpers shared with the T-002 suite)
    V2,
    activate_user,
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
from tests.test_t004_customers import mk as mk_customer
from tests.test_t005_credit_products import P as PRODUCTS
from tests.test_t005_credit_products import flow, today
from tests.test_t005_engine import rules

A = f"{V2}/credit-applications"
POLICY = f"{V2}/credit-approval-policies"
LIMITS = f"{V2}/credit-approval-limits"
OPEN_POLICY = dict(
    maker_checker_required=False, limits_enforced=False, approved_may_exceed_requested=False, evaluation_required=False
)
ALL_APPLICATION_PERMS = [
    "credit.applications.read",
    "credit.applications.create",
    "credit.applications.update_draft",
    "credit.applications.submit",
    "credit.applications.evaluate",
    "credit.applications.approve",
    "credit.applications.reject",
    "credit.applications.cancel",
    "credit.applications.formalize",
]


# ================================ helpers ============================================================
def world(client, adm, policy=OPEN_POLICY, code="PRD-1"):
    branch = mk_branch(client, adm, "B1")
    customer = mk_customer(client, adm)
    product, version = flow(client, adm, code)
    if policy is not None:
        r = client.put(POLICY, headers=adm, json=policy)
        assert r.status_code == 200, r.text
    return SimpleNamespace(b=branch, c=customer, p=product, v=version)


def new_app(client, hdr, w, amount="10000", term=12, expect=201, **extra):
    body = {
        "customer_id": w.c["id"],
        "product_id": w.p["id"],
        "requested_amount": amount,
        "currency_code": "DOP",
        "requested_term": term,
        "requested_frequency": "monthly",
        "origin_branch_id": w.b["id"],
    } | extra
    r = client.post(A, headers=hdr, json=body)
    assert r.status_code == expect, r.text
    return r.json()


def post(client, hdr, aid, action, expect=200, **body):
    r = client.post(f"{A}/{aid}/{action}", headers=hdr, json=body or None)
    assert r.status_code == expect, f"{action}: {r.status_code} {r.text}"
    return r.json()


def detail(client, hdr, aid):
    r = client.get(f"{A}/{aid}", headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()


def review(client, hdr, w, **kw):
    app = new_app(client, hdr, w, **kw)
    post(client, hdr, app["id"], "submit")
    post(client, hdr, app["id"], "start-review")
    return detail(client, hdr, app["id"])


def approve_body(d, amount="10000", term=12, **extra):
    return {
        "row_version": d["row_version"],
        "approved_amount": amount,
        "approved_term": term,
        "approved_frequency": "monthly",
    } | extra


def approve(client, hdr, d, expect=200, **kw):
    r = client.post(f"{A}/{d['id']}/approve", headers=hdr, json=approve_body(d, **kw))
    assert r.status_code == expect, f"approve: {r.status_code} {r.text}"
    return r.json()


def approved(client, hdr, w, **kw):
    d = review(client, hdr, w)
    approve(client, hdr, d, **kw)
    return detail(client, hdr, d["id"])


def user_hdr(client, sink, adm, tenant, email, perms, name=None, **assign):
    role = create_role(client, adm, name or f"Rol-{email}", perms)
    activate_user(client, sink, adm, email, roles=[role], **assign)
    return h(login(client, email, slug=tenant["slug"]))


def audit(prefix="credit_application."):
    with SessionLocal() as db:
        return [e for e in db.query(SecurityEvent).order_by(SecurityEvent.id) if e.event_type.startswith(prefix)]


def count(table, where="true", **params):
    with SessionLocal() as db:
        return db.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), params).scalar()


MONEY_TABLES = (
    "cash_movements",
    "cash_transfers",
    "cash_custody_transfers",
    "cash_deliveries",
    "payments",
    "loans",
    "loan_installments",
    "capital_movements",
)


def money_counts():
    return {t: count(t) for t in MONEY_TABLES}


def wait_blocked_n(pattern: str, n: int = 1, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as c:
            seen = c.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND query ILIKE :p"
                ),
                {"p": pattern},
            ).scalar()
        if seen >= n:
            return
        time.sleep(0.02)
    raise AssertionError(f"expected {n} backends blocked on {pattern}")


APP_LOCK = "%FROM credit_applications%FOR UPDATE%"


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


def in_thread(fn):
    out: dict = {}

    def run():
        out["resp"] = fn()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


# ================================ states (S01-S07) ===================================================
def test_s01_to_s07_state_machine(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    app = new_app(client, adm, w)
    assert (
        app["status"] == "draft"
        and app["application_number"] == "SOL-000001"
        and app["tenant_id"] == tenant_a["tenant_id"]
    )
    assert app["requested_amount"] == "10000.0000" and app["currency_code"] == "DOP"
    aid = app["id"]
    # invalid transitions out of draft
    for action, body in (
        ("start-review", {}),
        ("approve", approve_body(app)),
        ("reject", {"row_version": 1, "reason": "no"}),
        ("formalize", {}),
    ):
        r = client.post(f"{A}/{aid}/{action}", headers=adm, json=body)
        assert r.status_code in (409, 422) and r.status_code != 200, (action, r.text)
    assert post(client, adm, aid, "submit")["status"] == "submitted"  # S01
    assert post(client, adm, aid, "start-review")["status"] == "under_review"  # S02
    d = detail(client, adm, aid)
    assert approve(client, adm, d)["status"] == "approved"  # S03
    f = post(client, adm, aid, "formalize")  # S05
    assert f["status"] == "ready_for_disbursement" and detail(client, adm, aid)["status"] == "formalized"
    # S07 / S06: a formalized application can go nowhere
    for action, body in (
        ("reopen", {"reason": "volver"}),
        ("cancel", {"reason": "no vale"}),
        ("submit", {}),
        ("start-review", {}),
    ):
        r = client.post(f"{A}/{aid}/{action}", headers=adm, json=body)
        assert r.status_code == 409, (action, r.text)
    assert (
        client.patch(f"{A}/{aid}", headers=adm, json={"row_version": 99, "requested_amount": "500"}).status_code == 409
    )
    # S04: under_review -> rejected, and a rejected one is terminal
    d2 = review(client, adm, w)
    rej = post(client, adm, d2["id"], "reject", row_version=d2["row_version"], reason="capacidad insuficiente")
    assert rej["status"] == "rejected" and rej["decision"]["outcome"] == "rejected"
    for action, body in (("submit", {}), ("start-review", {}), ("formalize", {}), ("cancel", {"reason": "tarde"})):
        assert client.post(f"{A}/{d2['id']}/{action}", headers=adm, json=body).status_code == 409, action
    # a submitted application cannot be approved without review, nor rejected
    s = new_app(client, adm, w)
    post(client, adm, s["id"], "submit")
    assert (
        client.post(f"{A}/{s['id']}/approve", headers=adm, json=approve_body(detail(client, adm, s["id"]))).status_code
        == 409
    )
    assert (
        client.post(f"{A}/{s['id']}/reject", headers=adm, json={"row_version": 2, "reason": "x y z"}).status_code == 409
    )


def test_cancel_is_explicit_audited_and_keeps_history(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    for stage in ("draft", "submitted", "under_review", "approved"):
        app = new_app(client, adm, w)
        aid = app["id"]
        if stage != "draft":
            post(client, adm, aid, "submit")
        if stage in ("under_review", "approved"):
            post(client, adm, aid, "start-review")
        if stage == "approved":
            approve(client, adm, detail(client, adm, aid))
        r = post(client, adm, aid, "cancel", reason=f"cancelada desde {stage}")
        assert r["status"] == "cancelled" and r["cancellation"]["from_status"] == stage
        assert (
            r["cancellation"]["reason"] == f"cancelada desde {stage}"
            and r["cancellation"]["by"] == tenant_a["admin_id"]
        )
        assert post(client, adm, aid, "cancel", reason="otra vez")["replayed"] is True  # equivalent repeat
        assert client.post(f"{A}/{aid}/submit", headers=adm).status_code == 409
    assert count("credit_applications") == 4 and count("credit_decisions") == 1  # nothing was deleted
    assert [e.details["before"]["status"] for e in audit("credit_application.cancelled")] == [
        "draft",
        "submitted",
        "under_review",
        "approved",
    ]


def test_reopen_keeps_the_original_submitted_request_and_resubmission_is_a_new_submission(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    app = new_app(client, adm, w, amount="10000")
    post(client, adm, app["id"], "submit")
    d = detail(client, adm, app["id"])
    assert (
        client.patch(
            f"{A}/{app['id']}", headers=adm, json={"row_version": d["row_version"], "requested_amount": "20000"}
        ).status_code
        == 409
    )
    re = post(client, adm, app["id"], "reopen", reason="el cliente corrigio el monto")
    assert re["status"] == "draft"
    p = client.patch(
        f"{A}/{app['id']}",
        headers=adm,
        json={"row_version": re["row_version"], "requested_amount": "20000", "requested_term": 24},
    )
    assert p.status_code == 200 and p.json()["requested_amount"] == "20000.0000"
    post(client, adm, app["id"], "submit")
    final = detail(client, adm, app["id"])
    first, second = final["submissions"]
    assert (first["submission_number"], second["submission_number"]) == (1, 2)
    assert (
        first["request"]["requested_amount"] == "10000.0000" and first["request"]["requested_term"] == 12
    )  # untouched
    assert (
        first["reopen_reason"] == "el cliente corrigio el monto"
        and second["request"]["requested_amount"] == "20000.0000"
    )
    # under review it can no longer be reopened
    post(client, adm, app["id"], "start-review")
    assert client.post(f"{A}/{app['id']}/reopen", headers=adm, json={"reason": "ya en revision"}).status_code == 409
    with SessionLocal() as db:  # the submitted request is immutable in the database too
        sid = db.query(CreditApplicationSubmission).filter_by(application_id=app["id"], submission_number=1).one().id
    for sql in (
        "UPDATE credit_application_submissions SET request = '{}'::jsonb WHERE id = :i",
        "DELETE FROM credit_application_submissions WHERE id = :i",
    ):
        with SessionLocal() as db:
            with pytest.raises(DBAPIError, match="immutable|cannot be deleted"):
                db.execute(text(sql), {"i": sid})
                db.commit()


# ================================ amounts (A01-A04) ==================================================
def test_a01_a04_requested_and_approved_amounts_are_separate_and_explicit(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    d = review(client, adm, w, amount="10000")
    # A02: approved_amount is required: it is never inferred from the request
    body = approve_body(d)
    del body["approved_amount"]
    assert client.post(f"{A}/{d['id']}/approve", headers=adm, json=body).status_code == 422
    assert (
        client.post(
            f"{A}/{d['id']}/approve", headers=adm, json=approve_body(d) | {"approved_amount": 7000.5}
        ).status_code
        == 422
    )  # float
    assert (
        client.post(f"{A}/{d['id']}/approve", headers=adm, json=approve_body(d) | {"disbursed_amount": "1"}).status_code
        == 422
    )
    # A03: a smaller explicit amount is stored beside the request, which stays untouched
    out = approve(client, adm, d, amount="7000", term=6)
    assert out["requested_amount"] == "10000.0000" and out["requested_term"] == 12
    ap = out["decision"]["approval"]
    assert (ap["approved_amount"], ap["requested_amount"], ap["approved_term"]) == ("7000.0000", "10000.0000", 6)
    # A04: no disbursed amount exists anywhere
    with SessionLocal() as db:
        cols = {
            r[0]
            for r in db.execute(
                text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name LIKE 'credit_app%' "
                    "OR table_name LIKE 'credit_decision%' OR table_name LIKE 'credit_formal%'"
                )
            )
        }
    assert not [c for c in cols if "disburs" in c]
    f = post(client, adm, d["id"], "formalize")
    assert (
        f["approved_amount"] == "7000.0000" and "disbursed_amount" not in f and f["status"] == "ready_for_disbursement"
    )


def test_approved_above_requested_is_a_tenant_policy_choice_never_a_default(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)  # approved_may_exceed_requested = False
    d = review(client, adm, w, amount="10000")
    r = client.post(f"{A}/{d['id']}/approve", headers=adm, json=approve_body(d, amount="12000"))
    assert r.status_code == 422 and r.json()["error"]["details"][0]["code"] == "approved_exceeds_requested"
    assert (
        client.put(POLICY, headers=adm, json=OPEN_POLICY | {"approved_may_exceed_requested": True}).status_code == 200
    )
    out = approve(client, adm, d, amount="12000")
    assert out["decision"]["approval"]["approved_amount"] == "12000.0000" and out["requested_amount"] == "10000.0000"


def test_approval_is_blocked_until_the_tenant_configures_its_policy(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm, policy=None)
    d = review(client, adm, w)
    r = client.post(f"{A}/{d['id']}/approve", headers=adm, json=approve_body(d))
    assert r.status_code == 409 and r.json()["error"]["code"] == "approval_policy_not_configured"
    assert "BLOCKED_BY_SPEC" in r.json()["error"]["message"]
    assert count("credit_decisions") == 0 and detail(client, adm, d["id"])["status"] == "under_review"
    # reject needs no policy: it grants nothing
    assert (
        client.post(
            f"{A}/{d['id']}/reject", headers=adm, json={"row_version": d["row_version"], "reason": "sin capacidad"}
        ).status_code
        == 200
    )


def test_product_specific_policy_overrides_the_tenant_wide_one(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    assert (
        client.put(
            POLICY, headers=adm, json=OPEN_POLICY | {"product_id": w.p["id"], "evaluation_required": True}
        ).status_code
        == 200
    )
    d = review(client, adm, w)
    r = client.post(f"{A}/{d['id']}/approve", headers=adm, json=approve_body(d))
    assert r.status_code == 422 and "evaluacion" in r.json()["error"]["message"]
    ev = client.post(
        f"{A}/{d['id']}/evaluate", headers=adm, json={"notes": "visita realizada", "recommendation": "approve"}
    )
    assert ev.status_code == 201
    approve(client, adm, detail(client, adm, d["id"]))
    assert len(client.get(POLICY, headers=adm).json()) == 2


# ================================ maker-checker & limits (M01-M03) ===================================
def test_m01_m02_maker_checker_blocks_the_same_actor_and_allows_a_different_authorised_one(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm, policy=OPEN_POLICY | {"maker_checker_required": True})
    checker = user_hdr(
        client, sink, adm, tenant_a, "checker@x.com", ["credit.applications.read", "credit.applications.approve"]
    )
    d = review(client, adm, w)  # the admin prepared (created + submitted) it
    r = client.post(f"{A}/{d['id']}/approve", headers=adm, json=approve_body(d))
    assert r.status_code == 403 and r.json()["error"]["code"] == "maker_checker_violation"
    assert count("credit_decisions") == 0
    out = approve(client, checker, d)
    assert out["status"] == "approved"
    authority = out["decision"]["approval"]["authority"]["maker_checker"]
    assert (
        authority["checker"] != tenant_a["admin_id"] and tenant_a["admin_id"] in authority["makers"]
    )  # auditable actors
    assert out["decision"]["decided_by"] == authority["checker"]
    ev = audit("credit_application.approved")[0]
    assert ev.actor_id == authority["checker"] and ev.details["maker_checker_required"] is True
    # when the policy does not require separation, the same person may approve (no universal rule is imposed)
    client.put(POLICY, headers=adm, json=OPEN_POLICY)
    d2 = review(client, adm, w)
    assert approve(client, adm, d2)["status"] == "approved"


def test_m01b_both_creator_and_submitter_are_makers_a_third_user_is_the_checker(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm, policy=OPEN_POLICY | {"maker_checker_required": True})
    perms = ["credit.applications.read", "credit.applications.submit", "credit.applications.approve"]
    submitter = user_hdr(client, sink, adm, tenant_a, "sub@x.com", perms)
    third = user_hdr(
        client, sink, adm, tenant_a, "third@x.com", ["credit.applications.read", "credit.applications.approve"]
    )
    app = new_app(client, adm, w)  # created by the admin
    post(client, submitter, app["id"], "submit")  # submitted by someone else
    post(client, adm, app["id"], "start-review")
    d = detail(client, adm, app["id"])
    for who in (submitter, adm):  # the submitter and the creator are both makers
        r = client.post(f"{A}/{d['id']}/approve", headers=who, json=approve_body(d))
        assert r.status_code == 403 and r.json()["error"]["code"] == "maker_checker_violation"
    out = approve(client, third, d)
    assert sorted(out["decision"]["approval"]["authority"]["maker_checker"]["makers"]) == sorted(
        {tenant_a["admin_id"], detail(client, adm, app["id"])["submitted_by"]}
    )


def test_m03_approval_limits_are_enforced_server_side_per_user_role_currency_product_and_branch(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm, policy=OPEN_POLICY | {"limits_enforced": True})
    perms = ["credit.applications.read", "credit.applications.approve"]
    low = user_hdr(client, sink, adm, tenant_a, "low@x.com", perms)
    wide = user_hdr(client, sink, adm, tenant_a, "wide@x.com", perms)
    none = user_hdr(client, sink, adm, tenant_a, "none@x.com", perms)
    users = {u["email"]: u for u in client.get(f"{V2}/users", headers=adm).json()}
    mk_limit = lambda email, amount, **kw: client.post(  # noqa: E731
        LIMITS, headers=adm, json={"user_id": users[email]["id"], "currency_code": "DOP", "max_amount": amount} | kw
    )
    assert mk_limit("low@x.com", "5000").status_code == 201
    lim_wide = mk_limit("wide@x.com", "50000")
    assert lim_wide.status_code == 201 and lim_wide.json()["max_amount"] == "50000.0000"
    d = review(client, adm, w, amount="10000")
    over = client.post(f"{A}/{d['id']}/approve", headers=low, json=approve_body(d, amount="10000"))
    assert over.status_code == 403 and over.json()["error"]["code"] == "approval_limit_exceeded"
    no_limit = client.post(f"{A}/{d['id']}/approve", headers=none, json=approve_body(d))
    assert (
        no_limit.status_code == 403 and no_limit.json()["error"]["code"] == "approval_limit_exceeded"
    )  # deny by default
    assert (
        client.post(f"{A}/{d['id']}/approve", headers=adm, json=approve_body(d)).status_code == 403
    )  # admin has no limit row
    assert count("credit_decisions") == 0
    ok = approve(client, wide, d, amount="10000")
    assert ok["decision"]["approval"]["authority"]["limit"]["max_amount"] == "50000.0000"
    # within the limit of a smaller approver the same permission works
    d2 = review(client, adm, w, amount="4000")
    assert approve(client, low, d2, amount="4000")["status"] == "approved"
    # revoked limit stops working, history kept
    lid = lim_wide.json()["id"]
    assert client.post(f"{LIMITS}/{lid}/revoke", headers=adm).json()["revoked_at"] is not None
    d3 = review(client, adm, w, amount="4000")
    assert client.post(f"{A}/{d3['id']}/approve", headers=wide, json=approve_body(d3, amount="4000")).status_code == 403
    assert client.post(f"{LIMITS}/{lid}/revoke", headers=adm).status_code == 409
    assert count("credit_approval_limits") == 2


def test_limits_by_role_currency_product_and_branch_and_no_self_service(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm, policy=OPEN_POLICY | {"limits_enforced": True})
    other_branch = mk_branch(client, adm, "B2")
    role = create_role(client, adm, "Aprobador", ["credit.applications.read", "credit.applications.approve"])
    activate_user(client, sink, adm, "ro@x.com", roles=[role])
    ro = h(login(client, "ro@x.com", slug=tenant_a["slug"]))
    me = next(u for u in client.get(f"{V2}/users", headers=adm).json() if u["email"] == tenant_a["email"])
    assert (
        client.post(
            LIMITS, headers=adm, json={"user_id": me["id"], "currency_code": "DOP", "max_amount": "1"}
        ).status_code
        == 403
    )
    base = {"role_id": role["id"], "currency_code": "DOP", "max_amount": "20000"}
    # a limit for another currency / product / branch does not authorise this operation
    assert client.post(LIMITS, headers=adm, json=base | {"branch_id": other_branch["id"]}).status_code == 201
    d = review(client, adm, w, amount="10000")
    assert client.post(f"{A}/{d['id']}/approve", headers=ro, json=approve_body(d)).status_code == 403
    assert (
        client.post(LIMITS, headers=adm, json=base | {"product_id": w.p["id"], "branch_id": w.b["id"]}).status_code
        == 201
    )
    assert approve(client, ro, d)["status"] == "approved"  # a role limit matched product + branch
    # validation of the limit itself
    assert client.post(LIMITS, headers=adm, json={"currency_code": "DOP", "max_amount": "5"}).status_code == 422
    assert client.post(LIMITS, headers=adm, json=base | {"user_id": me["id"]}).status_code == 422
    assert client.post(LIMITS, headers=adm, json=base | {"max_amount": "-5"}).status_code == 422
    assert client.post(LIMITS, headers=adm, json=base | {"max_amount": "0"}).status_code == 422
    assert client.post(LIMITS, headers=adm, json=base | {"product_id": 9999}).status_code == 404
    assert client.post(LIMITS, headers=adm, json=base | {"currency_code": "USD"}).status_code == 422  # not enabled


# ================================ product version & snapshot (V01-V03) ==============================
def test_v01_v03_formalization_freezes_the_t005_snapshot_and_survives_later_product_changes(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    d = approved(client, adm, w)
    f = post(client, adm, d["id"], "formalize")
    snap = client.get(f"{PRODUCTS}/{w.p['id']}/versions/{w.v['id']}/snapshot", headers=adm).json()
    contract = f["contract_snapshot"]
    assert (
        contract["product"]["snapshot"] == snap["snapshot"] and contract["product"]["rules_hash"] == snap["rules_hash"]
    )
    assert f["rules_hash"] == snap["rules_hash"] == w.v["rules_hash"] and f["hash_verified"] is True
    assert (f["approved_amount"], f["currency_code"], f["term"], f["frequency"]) == ("10000.0000", "DOP", 12, "monthly")
    assert contract["approved"]["term_periods"] == 12 and contract["branches"]["origin_branch_id"] == w.b["id"]
    assert contract["tenant_id"] == tenant_a["tenant_id"] and contract["application"]["number"] == "SOL-000001"
    assert {
        "frequency",
        "rounding",
        "calendar",
        "grace",
        "delinquency",
        "allocation",
        "prepayment",
        "payoff",
        "fees",
    } <= set(
        contract["product"]["snapshot"]["rules"]
    )  # calendar, fees, allocation, delinquency, prepayment... all frozen
    assert f["reference"] == "FRM-000001" and f["contract_hash"].startswith("sha256:")
    # a NEW product version (different rate) is published afterwards: the formalization does not move
    v2 = client.post(f"{PRODUCTS}/{w.p['id']}/versions", headers=adm, json={"based_on_version_id": w.v["id"]}).json()
    r2 = rules()
    r2["method"]["rate"]["value"] = "36"
    assert (
        client.put(
            f"{PRODUCTS}/{w.p['id']}/versions/{v2['id']}",
            headers=adm,
            json={"row_version": v2["row_version"], "rules": r2},
        ).status_code
        == 200
    )
    from tests.test_t005_credit_products import publish

    publish(client, adm, w.p["id"], v2["id"], eff=today() + timedelta(days=400))
    again = client.get(f"{A}/{d['id']}/formalization", headers=adm).json()
    assert (
        again["contract_snapshot"] == contract
        and again["contract_hash"] == f["contract_hash"]
        and again["hash_verified"] is True
    )
    assert again["rules_hash"] == w.v["rules_hash"] != v2["rules_hash"]
    # the frozen contract is immutable in the database
    forbidden = [
        "UPDATE credit_formalizations SET contract_snapshot = '{}'::jsonb",
        "UPDATE credit_formalizations SET approved_amount = approved_amount + 1",
        "UPDATE credit_formalizations SET rules_hash = 'sha256:x'",
        "UPDATE credit_formalizations SET product_version_id = product_version_id + 1",
        "UPDATE credit_formalizations SET contract_hash = 'sha256:x'",
        "DELETE FROM credit_formalizations",
    ]
    for sql in forbidden:
        with SessionLocal() as db:
            with pytest.raises(DBAPIError, match="immutable"):
                db.execute(text(sql))
                db.commit()


def test_decisions_approvals_evaluations_and_condition_definitions_are_immutable_in_the_database(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    d = review(client, adm, w)
    assert client.post(f"{A}/{d['id']}/evaluate", headers=adm, json={"notes": "ok"}).status_code == 201
    approve(
        client,
        adm,
        detail(client, adm, d["id"]),
        conditions=[
            {"kind": "document_pending", "description": "Comprobante de ingresos", "blocks_formalization": True}
        ],
    )
    for sql in (
        "UPDATE credit_decisions SET reason = 'x'",
        "DELETE FROM credit_decisions",
        "UPDATE credit_approvals SET approved_amount = approved_amount + 1",
        "DELETE FROM credit_approvals",
        "UPDATE credit_application_evaluations SET data = '{}'::jsonb",
        "DELETE FROM credit_application_evaluations",
        "UPDATE credit_application_conditions SET description = 'otra'",
        "UPDATE credit_application_conditions SET blocks_formalization = false",
        "DELETE FROM credit_application_conditions",
    ):
        with SessionLocal() as db:
            with pytest.raises(DBAPIError, match="immutable|cannot be deleted"):
                db.execute(text(sql))
                db.commit()


def test_integrity_of_the_pinned_version_is_verified_before_approving_and_formalizing(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    d = approved(client, adm, w)
    d2 = review(client, adm, w)  # submitted while the version was still intact
    with engine.begin() as c:  # corrupt the published version (only possible with the guard trigger off)
        c.execute(text("ALTER TABLE credit_product_versions DISABLE TRIGGER trg_credit_product_versions_guard"))
        c.execute(
            text(
                "UPDATE credit_product_versions SET rules = jsonb_set(rules, '{method,rate,value}', '\"99\"') WHERE id = :i"
            ),
            {"i": w.v["id"]},
        )
        c.execute(text("ALTER TABLE credit_product_versions ENABLE TRIGGER trg_credit_product_versions_guard"))
    r = client.post(f"{A}/{d['id']}/formalize", headers=adm)
    assert r.status_code == 409 and r.json()["error"]["code"] == "rules_integrity_failed"  # rules_hash is checked
    assert count("credit_formalizations") == 0 and detail(client, adm, d["id"])["status"] == "approved"
    assert client.post(f"{A}/{d2['id']}/approve", headers=adm, json=approve_body(d2)).status_code == 409
    new = client.post(f"{A}/{d2['id']}/cancel", headers=adm, json={"reason": "limpieza"})
    assert new.status_code == 200


def test_withdrawn_product_or_version_blocks_but_a_superseded_version_stays_usable(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    d = review(client, adm, w)
    v2 = client.post(f"{PRODUCTS}/{w.p['id']}/versions", headers=adm, json={"based_on_version_id": w.v["id"]}).json()
    from tests.test_t005_credit_products import publish

    publish(client, adm, w.p["id"], v2["id"], eff=today() + timedelta(days=400))  # supersedes v1 (window closes)
    assert approve(client, adm, d)["status"] == "approved"  # v1 is superseded, NOT withdrawn: in-flight work completes
    assert post(client, adm, d["id"], "formalize")["product_version_id"] == w.v["id"]
    d2 = review(client, adm, w)
    assert (
        client.post(
            f"{PRODUCTS}/{w.p['id']}/versions/{w.v['id']}/retire", headers=adm, json={"reason": "obsoleta"}
        ).status_code
        == 200
    )
    r = client.post(f"{A}/{d2['id']}/approve", headers=adm, json=approve_body(d2))
    assert r.status_code == 409 and r.json()["error"]["code"] == "product_not_available"  # explicit withdrawal blocks
    new = client.post(
        A,
        headers=adm,
        json={
            "customer_id": w.c["id"],
            "product_id": w.p["id"],
            "requested_amount": "1000",
            "currency_code": "DOP",
            "requested_term": 3,
            "requested_frequency": "monthly",
            "origin_branch_id": w.b["id"],
        },
    )
    assert new.status_code == 409  # the retired v1 is no longer offered and v2 is not yet in force


# ================================ validation =========================================================
def test_application_validates_against_the_pinned_product_version(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)

    def attempt(**kw):
        body = {
            "customer_id": w.c["id"],
            "product_id": w.p["id"],
            "requested_amount": "10000",
            "currency_code": "DOP",
            "requested_term": 12,
            "requested_frequency": "monthly",
            "origin_branch_id": w.b["id"],
        } | kw
        return client.post(A, headers=adm, json=body)

    codes = lambda r: {i["code"] for i in r.json()["error"]["details"]}  # noqa: E731
    assert "amount_out_of_range" in codes(attempt(requested_amount="50"))  # below min (100)
    assert "amount_out_of_range" in codes(attempt(requested_amount="600000"))
    assert "too_many_decimals" in codes(attempt(requested_amount="1000.001"))
    assert "term_out_of_range" in codes(attempt(requested_term=61))
    assert "frequency_incompatible" in codes(attempt(requested_frequency="weekly"))
    assert "currency_not_allowed" in codes(attempt(currency_code="USD"))
    assert attempt(requested_amount=10000.5).status_code == 422  # float
    assert attempt(tenant_id=1).status_code == 422  # tenant is never taken from the payload
    assert attempt(requested_amount="0").status_code == 422
    assert attempt(origin_branch_id=99999).status_code == 404 and attempt(customer_id=99999).status_code == 404
    assert attempt(product_id=99999).status_code == 404
    assert count("credit_applications") == 0
    # inactive customer / inactive product are refused
    assert client.post(f"{V2}/customers/{w.c['id']}/activate", headers=adm).status_code == 200
    assert client.post(f"{V2}/customers/{w.c['id']}/deactivate", headers=adm).status_code == 200
    assert "customer_inactive" in codes(attempt())
    assert client.post(f"{V2}/customers/{w.c['id']}/activate", headers=adm).status_code == 200
    assert (
        client.post(f"{PRODUCTS}/{w.p['id']}/deactivate", headers=adm, json={"reason": "fuera de oferta"}).status_code
        == 200
    )
    assert attempt().status_code == 409


def test_draft_edit_repins_validates_and_uses_optimistic_versions(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    app = new_app(client, adm, w)
    ok = client.patch(
        f"{A}/{app['id']}",
        headers=adm,
        json={"row_version": app["row_version"], "requested_amount": "15000", "requested_term": 18},
    )
    assert ok.status_code == 200 and ok.json()["row_version"] == app["row_version"] + 1
    stale = client.patch(
        f"{A}/{app['id']}", headers=adm, json={"row_version": app["row_version"], "requested_amount": "16000"}
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_application_version"
    bad = client.patch(
        f"{A}/{app['id']}", headers=adm, json={"row_version": ok.json()["row_version"], "requested_term": 500}
    )
    assert bad.status_code == 422
    assert detail(client, adm, app["id"])["requested_term"] == 18  # a rejected edit changed nothing
    assert [e.details["changed_fields"] for e in audit("credit_application.updated")] == [
        ["requested_amount", "requested_term"]
    ]


# ================================ conditions, documents, evaluation ==================================
def test_conditions_block_formalization_only_when_the_approver_says_so(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    d = review(client, adm, w)
    conds = [
        {"kind": "document_pending", "description": "Comprobante de ingresos", "blocks_formalization": True},
        {"kind": "administrative", "description": "Firmar anexo", "blocks_formalization": False},
    ]
    out = approve(client, adm, d, conditions=conds)
    assert [c["blocks_formalization"] for c in out["conditions"]] == [True, False]
    blocked = client.post(f"{A}/{d['id']}/formalize", headers=adm)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "blocking_conditions_pending"
    assert [c["kind"] for c in blocked.json()["error"]["details"]] == ["document_pending"]
    assert count("credit_formalizations") == 0
    cid = out["conditions"][0]["id"]
    r = client.post(
        f"{A}/{d['id']}/conditions/{cid}/resolve", headers=adm, json={"status": "fulfilled", "note": "recibido"}
    )
    assert r.status_code == 200 and r.json()["status"] == "fulfilled"
    assert (
        client.post(f"{A}/{d['id']}/conditions/{cid}/resolve", headers=adm, json={"status": "waived"}).status_code
        == 409
    )
    f = post(client, adm, d["id"], "formalize")
    assert [c["status"] for c in f["contract_snapshot"]["conditions"]] == [
        "fulfilled",
        "pending",
    ]  # frozen as they were


def test_document_links_are_references_only_and_evaluation_content_is_protected(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    app = new_app(client, adm, w)
    link = client.post(
        f"{A}/{app['id']}/documents", headers=adm, json={"requirement": "Cedula", "reference": "doc-123"}
    )
    assert link.status_code == 201 and link.json()["status"] == "pending" and link.json()["reference"] == "doc-123"
    post(client, adm, app["id"], "submit")
    post(client, adm, app["id"], "start-review")
    st = client.post(f"{A}/{app['id']}/documents/{link.json()['id']}/status", headers=adm, json={"status": "verified"})
    assert st.status_code == 200 and st.json()["status"] == "verified"
    assert detail(client, adm, app["id"])["documents"][0]["status"] == "verified"
    with SessionLocal() as db:  # no binary storage: no content/bytes column
        cols = {
            r[0]
            for r in db.execute(
                text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'credit_application_document_links'"
                )
            )
        }
    assert not {"content", "data", "blob", "file"} & cols
    secret = "ingreso mensual confidencial 9876543"
    ev = client.post(
        f"{A}/{app['id']}/evaluate",
        headers=adm,
        json={
            "monthly_income": "50000",
            "monthly_expenses": "20000",
            "declared_payment_capacity": "8000",
            "currency_code": "DOP",
            "verifications": [{"kind": "employment", "result": "verified"}],
            "risks": ["alta rotacion"],
            "notes": secret,
            "recommendation": "approve",
        },
    )
    assert ev.status_code == 201 and "notes" not in ev.json()
    assert client.post(f"{A}/{app['id']}/evaluate", headers=adm, json={}).status_code == 422  # empty
    assert (
        client.post(f"{A}/{app['id']}/evaluate", headers=adm, json={"score": 700}).status_code == 422
    )  # no invented score
    assert client.post(f"{A}/{app['id']}/evaluate", headers=adm, json={"monthly_income": 5000.5}).status_code == 422
    listed = client.get(f"{A}/{app['id']}/evaluations", headers=adm).json()
    assert listed[0]["data"]["notes"] == secret and listed[0]["data"]["monthly_income"] == "50000"
    assert "evaluations" not in detail(client, adm, app["id"]) and secret not in str(detail(client, adm, app["id"]))
    # privacy: the audit trail names the evaluation, never its values
    evs = audit("credit_application.evaluated")
    assert len(evs) == 1 and secret not in str(evs[0].details) and "50000" not in str(evs[0].details)
    assert evs[0].details["sections"] == [
        "currency_code",
        "declared_payment_capacity",
        "monthly_expenses",
        "monthly_income",
        "notes",
        "recommendation",
        "risks",
        "verifications",
    ]
    # evaluation content needs the evaluate permission
    reader = user_hdr(client, sink, adm, tenant_a, "reader@x.com", ["credit.applications.read"])
    assert client.get(f"{A}/{app['id']}", headers=reader).status_code == 200
    assert client.get(f"{A}/{app['id']}/evaluations", headers=reader).status_code == 403
    done = new_app(client, adm, w)
    assert (
        client.post(f"{A}/{done['id']}/evaluate", headers=adm, json={"notes": "x"}).status_code == 409
    )  # not under review


# ================================ idempotency (I01-I04) ==============================================
def test_i01_i04_duplicate_commands_are_deterministic_and_create_nothing_new(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    app = new_app(client, adm, w)
    first = post(client, adm, app["id"], "submit")
    again = post(client, adm, app["id"], "submit")  # I01
    assert first["replayed"] is False and again["replayed"] is True and again["status"] == "submitted"
    assert count("credit_application_submissions", "application_id = :a", a=app["id"]) == 1
    post(client, adm, app["id"], "start-review")
    assert post(client, adm, app["id"], "start-review")["replayed"] is True
    d = detail(client, adm, app["id"])
    one = approve(client, adm, d)
    two = client.post(f"{A}/{d['id']}/approve", headers=adm, json=approve_body(d))  # I02: the same approval again
    assert two.status_code == 200 and two.json()["replayed"] is True and one["replayed"] is False
    assert two.json()["decision"]["id"] == one["decision"]["id"]
    assert count("credit_decisions") == 1 and count("credit_approvals") == 1
    conflict = client.post(f"{A}/{d['id']}/approve", headers=adm, json=approve_body(d, amount="9000"))
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "already_decided"  # a different one
    assert (
        client.post(
            f"{A}/{d['id']}/reject", headers=adm, json={"row_version": d["row_version"], "reason": "arrepentido"}
        ).status_code
        == 409
    )
    f1 = post(client, adm, d["id"], "formalize")
    f2 = post(client, adm, d["id"], "formalize")  # I03
    assert (
        f1["replayed"] is False
        and f2["replayed"] is True
        and f1["id"] == f2["id"]
        and f1["reference"] == f2["reference"]
    )
    assert count("credit_formalizations") == 1
    # reject: the same rejection is a replay, another reason a conflict
    d3 = review(client, adm, w)
    body = {"row_version": d3["row_version"], "reason": "capacidad insuficiente"}
    assert client.post(f"{A}/{d3['id']}/reject", headers=adm, json=body).json()["replayed"] is False
    assert client.post(f"{A}/{d3['id']}/reject", headers=adm, json=body).json()["replayed"] is True
    assert client.post(f"{A}/{d3['id']}/reject", headers=adm, json=body | {"reason": "otro motivo"}).status_code == 409
    assert client.post(f"{A}/{d3['id']}/approve", headers=adm, json=approve_body(d3)).status_code == 409
    assert count("credit_decisions", "application_id = :a", a=d3["id"]) == 1


def test_stale_review_content_cannot_be_approved(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    d = review(client, adm, w)
    stale = d["row_version"] - 1
    r = client.post(f"{A}/{d['id']}/approve", headers=adm, json=approve_body(d) | {"row_version": stale})
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_application_version"
    assert (
        client.post(f"{A}/{d['id']}/reject", headers=adm, json={"row_version": stale, "reason": "algo"}).status_code
        == 409
    )
    assert count("credit_decisions") == 0
    dec = approve(client, adm, d)["decision"]
    assert dec["application_row_version"] == d["row_version"]  # the approval is bound to the reviewed content


# ================================ concurrency (real PostgreSQL connections) ===========================
def test_i04_approve_vs_reject_race_yields_exactly_one_terminal_decision(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    d = review(client, adm, w)
    lock = tracked_connect()  # an in-flight transition holds the application row
    lock.execute(text("SELECT id FROM credit_applications WHERE id = :i FOR UPDATE"), {"i": d["id"]})
    t_a, out_a = in_thread(lambda: client.post(f"{A}/{d['id']}/approve", headers=adm, json=approve_body(d)))
    t_r, out_r = in_thread(
        lambda: client.post(
            f"{A}/{d['id']}/reject", headers=adm, json={"row_version": d["row_version"], "reason": "no cumple"}
        )
    )
    wait_blocked_n(APP_LOCK, 2)  # both are queued behind the same row lock: not an accident of timing
    lock.rollback()
    lock.close()
    t_a.join(30), t_r.join(30)
    codes = sorted([out_a["resp"].status_code, out_r["resp"].status_code])
    assert codes == [200, 409], codes
    assert count("credit_decisions", "application_id = :a", a=d["id"]) == 1
    with SessionLocal() as db:
        decision = db.query(CreditDecision).filter_by(application_id=d["id"]).one()
        status = db.get(CreditApplication, d["id"]).status
        approvals = db.execute(text("SELECT count(*) FROM credit_approvals")).scalar()
    assert (decision.outcome, status) in (("approved", "approved"), ("rejected", "rejected"))
    assert approvals == (1 if decision.outcome == "approved" else 0)  # no half-decision


def test_double_approve_and_double_formalize_races_create_exactly_one_row(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    d = review(client, adm, w)
    barrier = threading.Barrier(3)

    def go(action, body):
        def run():
            barrier.wait(10)
            return client.post(f"{A}/{d['id']}/{action}", headers=adm, json=body)

        return run

    ts = [in_thread(go("approve", approve_body(d))) for _ in range(2)]
    barrier.wait(10)
    for t, _ in ts:
        t.join(30)
    assert sorted(o["resp"].status_code for _, o in ts) == [200, 200]
    assert sorted(o["resp"].json()["replayed"] for _, o in ts) == [False, True]
    assert count("credit_decisions") == 1 and count("credit_approvals") == 1
    barrier2 = threading.Barrier(3)

    def formalize():
        barrier2.wait(10)
        return client.post(f"{A}/{d['id']}/formalize", headers=adm)

    fs = [in_thread(formalize) for _ in range(2)]
    barrier2.wait(10)
    for t, _ in fs:
        t.join(30)
    assert sorted(o["resp"].status_code for _, o in fs) == [200, 200]
    assert sorted(o["resp"].json()["replayed"] for _, o in fs) == [False, True]
    assert count("credit_formalizations") == 1
    assert (
        count("tenant_sequences", "name = 'credit_formalization' AND last_value = 1") == 1
    )  # the loser consumed no number


def test_formalize_race_blocked_on_the_same_row_lock_formalizes_once(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    d = approved(client, adm, w)
    lock = tracked_connect()
    lock.execute(text("SELECT id FROM credit_applications WHERE id = :i FOR UPDATE"), {"i": d["id"]})
    ts = [in_thread(lambda: client.post(f"{A}/{d['id']}/formalize", headers=adm)) for _ in range(3)]
    wait_blocked_n(APP_LOCK, 3)
    lock.rollback()
    lock.close()
    for t, _ in ts:
        t.join(30)
    assert [o["resp"].status_code for _, o in ts] == [200, 200, 200]
    assert sum(1 for _, o in ts if o["resp"].json()["replayed"] is False) == 1
    assert count("credit_formalizations") == 1


def test_submitted_application_edit_vs_submit_race_never_leaves_a_mismatched_snapshot(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    app = new_app(client, adm, w, amount="10000")
    lock = tracked_connect()
    lock.execute(text("SELECT id FROM credit_applications WHERE id = :i FOR UPDATE"), {"i": app["id"]})
    t_s, out_s = in_thread(lambda: client.post(f"{A}/{app['id']}/submit", headers=adm))
    t_p, out_p = in_thread(
        lambda: client.patch(
            f"{A}/{app['id']}", headers=adm, json={"row_version": app["row_version"], "requested_amount": "20000"}
        )
    )
    wait_blocked_n(APP_LOCK, 2)
    lock.rollback()
    lock.close()
    t_s.join(30), t_p.join(30)
    final = detail(client, adm, app["id"])
    assert out_s["resp"].status_code == 200 and final["status"] == "submitted" and len(final["submissions"]) == 1
    # whichever won, the frozen submitted request equals the application's content: no silent post-submit rewrite
    assert final["submissions"][0]["request"]["requested_amount"] == final["requested_amount"]
    if out_p["resp"].status_code == 200:
        assert final["requested_amount"] == "20000.0000"  # the edit landed BEFORE the submission and was frozen with it
    else:
        assert out_p["resp"].status_code == 409 and final["requested_amount"] == "10000.0000"


def test_formalize_waits_for_an_in_flight_version_withdrawal_and_then_refuses(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    d = approved(client, adm, w)
    a = tracked_connect()  # a version retirement in flight (it holds the version row)
    a.execute(
        text(
            "UPDATE credit_product_versions SET status = 'retired', retired_at = now(), effective_to = current_date WHERE id = :i"
        ),
        {"i": w.v["id"]},
    )
    t, out = in_thread(lambda: client.post(f"{A}/{d['id']}/formalize", headers=adm))
    wait_blocked_n("%FROM credit_product_versions%FOR SHARE%")
    assert "resp" not in out
    a.commit()
    a.close()
    t.join(30)
    assert out["resp"].status_code == 409 and out["resp"].json()["error"]["code"] == "product_not_available"
    assert count("credit_formalizations") == 0 and detail(client, adm, d["id"])["status"] == "approved"


# ================================ tenant isolation (T01-T04) =========================================
def test_t01_t04_cross_tenant_references_and_actions_are_rejected(client, sink, tenant_a, tenant_b):
    adm_a, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    wa = world(client, adm_a)
    wb = world(client, adm_b, code="PRD-B")
    body = {
        "customer_id": wa.c["id"],
        "product_id": wb.p["id"],
        "requested_amount": "1000",
        "currency_code": "DOP",
        "requested_term": 3,
        "requested_frequency": "monthly",
        "origin_branch_id": wb.b["id"],
    }
    assert (
        client.post(A, headers=adm_b, json=body | {"product_id": wb.p["id"], "customer_id": wa.c["id"]}).status_code
        == 404
    )  # T01
    assert (
        client.post(A, headers=adm_b, json=body | {"customer_id": wb.c["id"], "product_id": wa.p["id"]}).status_code
        == 404
    )  # T02
    assert (
        client.post(
            A, headers=adm_b, json=body | {"customer_id": wb.c["id"], "origin_branch_id": wa.b["id"]}
        ).status_code
        == 404
    )  # T03
    assert (
        client.post(
            A, headers=adm_b, json=body | {"customer_id": wb.c["id"], "managing_branch_id": wa.b["id"]}
        ).status_code
        == 404
    )
    assert count("credit_applications") == 0
    d = review(client, adm_a, wa)
    base = f"{A}/{d['id']}"
    for method, url, json in (
        ("get", base, None),
        ("get", f"{base}/formalization", None),
        ("get", f"{base}/evaluations", None),
        ("patch", base, {"row_version": 1, "requested_amount": "5"}),
        ("post", f"{base}/submit", None),
        ("post", f"{base}/start-review", None),
        ("post", f"{base}/approve", approve_body(d)),  # T04
        ("post", f"{base}/reject", {"row_version": d["row_version"], "reason": "intruso"}),
        ("post", f"{base}/cancel", {"reason": "intruso"}),
        ("post", f"{base}/formalize", None),
        ("post", f"{base}/evaluate", {"notes": "x"}),
        ("post", f"{base}/reopen", {"reason": "intruso"}),
        ("post", f"{base}/documents", {"requirement": "x"}),
    ):
        r = getattr(client, method)(url, headers=adm_b, **({"json": json} if json is not None else {}))
        assert r.status_code == 404, (method, url, r.text)
    assert client.get(A, headers=adm_b).json() == [] and detail(client, adm_a, d["id"])["status"] == "under_review"
    # limits/policies of another tenant's users, roles, products are not reachable either
    assert client.put(POLICY, headers=adm_b, json=OPEN_POLICY | {"product_id": wa.p["id"]}).status_code == 404
    users_a = client.get(f"{V2}/users", headers=adm_a).json()
    assert (
        client.post(
            LIMITS, headers=adm_b, json={"user_id": users_a[0]["id"], "currency_code": "DOP", "max_amount": "5"}
        ).status_code
        == 404
    )
    # database level: composite FKs refuse a row that mixes tenants
    with SessionLocal() as db:
        with pytest.raises(DBAPIError, match="fk_credit_applications_tenant_customer"):
            db.execute(
                text(
                    "INSERT INTO credit_applications (tenant_id, application_number, customer_id, product_id, product_version_id, "
                    "requested_amount, currency_code, requested_term, requested_frequency, origin_branch_id, status, row_version, "
                    "submission_count, created_by, created_at, updated_at) VALUES (:t, 'X-1', :c, :p, :v, 10, 'DOP', 3, 'monthly', :b, "
                    "'draft', 1, 0, 1, now(), now())"
                ),
                {"t": tenant_b["tenant_id"], "c": wa.c["id"], "p": wb.p["id"], "v": wb.v["id"], "b": wb.b["id"]},
            )
            db.commit()
        db.rollback()
        with pytest.raises(DBAPIError, match="fk_credit_applications_tenant_product_version"):
            db.execute(
                text(
                    "INSERT INTO credit_applications (tenant_id, application_number, customer_id, product_id, product_version_id, "
                    "requested_amount, currency_code, requested_term, requested_frequency, origin_branch_id, status, row_version, "
                    "submission_count, created_by, created_at, updated_at) VALUES (:t, 'X-2', :c, :p, :v, 10, 'DOP', 3, 'monthly', :b, "
                    "'draft', 1, 0, 1, now(), now())"
                ),
                {"t": tenant_b["tenant_id"], "c": wb.c["id"], "p": wb.p["id"], "v": wa.v["id"], "b": wb.b["id"]},
            )
            db.commit()
        db.rollback()


# ================================ permissions & scope ================================================
def test_every_action_needs_its_own_server_side_permission(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    reader = user_hdr(client, sink, adm, tenant_a, "r@x.com", ["credit.applications.read"])
    nothing = user_hdr(client, sink, adm, tenant_a, "n@x.com", ["users.read"])
    app = new_app(client, adm, w)
    d = review(client, adm, w)
    for method, url, body in (
        (
            "post",
            A,
            {
                "customer_id": 1,
                "product_id": 1,
                "requested_amount": "1",
                "currency_code": "DOP",
                "requested_term": 1,
                "requested_frequency": "monthly",
                "origin_branch_id": w.b["id"],
            },
        ),
        ("post", f"{A}/{app['id']}/submit", None),
        ("post", f"{A}/{d['id']}/start-review", None),
        ("post", f"{A}/{d['id']}/approve", approve_body(d)),
        ("post", f"{A}/{d['id']}/reject", {"row_version": d["row_version"], "reason": "sin capacidad"}),
        ("post", f"{A}/{app['id']}/cancel", {"reason": "no vale"}),
        ("post", f"{A}/{d['id']}/formalize", None),
        ("patch", f"{A}/{app['id']}", {"row_version": 1, "requested_amount": "5"}),
    ):
        r = getattr(client, method)(url, headers=reader, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (url, r.status_code)
    assert client.put(POLICY, headers=reader, json=OPEN_POLICY).status_code == 403
    assert (
        client.post(LIMITS, headers=reader, json={"user_id": 1, "currency_code": "DOP", "max_amount": "5"}).status_code
        == 403
    )
    assert client.get(LIMITS, headers=reader).status_code == 403
    for r in (
        client.get(A, headers=nothing),
        client.get(f"{A}/{app['id']}", headers=nothing),
        client.get(POLICY, headers=nothing),
    ):
        assert r.status_code == 403
    assert client.get(A).status_code == 401
    # approve and formalize are separate permissions
    approver = user_hdr(
        client, sink, adm, tenant_a, "ap@x.com", ["credit.applications.read", "credit.applications.approve"]
    )
    assert approve(client, approver, d)["status"] == "approved"
    assert client.post(f"{A}/{d['id']}/formalize", headers=approver).status_code == 403
    assert client.post(f"{A}/{d['id']}/formalize", headers=adm).status_code == 200


def test_branch_scoped_officers_only_see_and_act_on_their_branches(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    b2 = mk_branch(client, adm, "B2")
    officer = user_hdr(
        client, sink, adm, tenant_a, "off@x.com", ALL_APPLICATION_PERMS, scope="branch", branch_id=b2["id"]
    )
    mine = client.post(
        A,
        headers=officer,
        json={
            "customer_id": w.c["id"],
            "product_id": w.p["id"],
            "requested_amount": "1000",
            "currency_code": "DOP",
            "requested_term": 3,
            "requested_frequency": "monthly",
            "origin_branch_id": b2["id"],
        },
    )
    assert mine.status_code == 201
    elsewhere = client.post(
        A,
        headers=officer,
        json={
            "customer_id": w.c["id"],
            "product_id": w.p["id"],
            "requested_amount": "1000",
            "currency_code": "DOP",
            "requested_term": 3,
            "requested_frequency": "monthly",
            "origin_branch_id": w.b["id"],
        },
    )
    assert elsewhere.status_code == 403
    other = new_app(client, adm, w)  # origin B1
    assert [a["id"] for a in client.get(A, headers=officer).json()] == [mine.json()["id"]]
    assert client.get(f"{A}/{other['id']}", headers=officer).status_code == 403
    assert client.post(f"{A}/{other['id']}/submit", headers=officer).status_code == 403
    assert client.post(f"{A}/{mine.json()['id']}/submit", headers=officer).status_code == 200
    assert len(client.get(A, headers=adm).json()) == 2


# ================================ no money, no writes on GET, audit ==================================
def test_x01_x03_the_whole_flow_moves_no_money_and_activates_no_loan(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    before = money_counts()
    with SessionLocal() as db:
        balances = db.execute(text("SELECT coalesce(sum(amount), 0) FROM cash_movements")).scalar()
    d = review(client, adm, w)
    approve(client, adm, d, conditions=[{"kind": "other", "description": "nota", "blocks_formalization": False}])
    f = post(client, adm, d["id"], "formalize")
    assert f["status"] == "ready_for_disbursement"
    assert money_counts() == before  # X01 cash movement, X02 bank/capital, X03 payments, no loan, no installments
    with SessionLocal() as db:
        assert db.execute(text("SELECT coalesce(sum(amount), 0) FROM cash_movements")).scalar() == balances
        assert db.execute(text("SELECT count(*) FROM bank_accounts")).scalar() == 0
    assert "loan_id" not in f and "disbursed_amount" not in f


def test_every_get_performs_no_database_write(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    d = approved(client, adm, w, conditions=[{"kind": "other", "description": "nota", "blocks_formalization": False}])
    post(client, adm, d["id"], "formalize")
    client.post(f"{A}/{d['id']}/documents", headers=adm, json={"requirement": "Cedula"})
    statements = []

    def before(conn, cursor, statement, parameters, context, executemany):
        if re.match(r"\s*(INSERT|UPDATE|DELETE)", statement, re.I):
            statements.append(statement[:90])

    event.listen(engine, "before_cursor_execute", before)
    try:
        for url in (
            A,
            f"{A}?status=formalized&product_id={w.p['id']}&customer_id={w.c['id']}",
            f"{A}/{d['id']}",
            f"{A}/{d['id']}/formalization",
            f"{A}/{d['id']}/evaluations",
            POLICY,
            LIMITS,
        ):
            assert client.get(url, headers=adm).status_code == 200, url
    finally:
        event.remove(engine, "before_cursor_execute", before)
    assert statements == []


def test_audit_trail_has_actor_correlation_before_after_reason_and_digests_without_sensitive_values(
    client, sink, tenant_a
):
    adm = admin_headers(client, tenant_a)
    w = world(client, adm)
    d = review(client, adm, w)
    approve(client, adm, d, amount="8000", reason="capacidad verificada")
    post(client, adm, d["id"], "formalize")
    names = [e.event_type for e in audit()]
    assert names == [
        "credit_application.created",
        "credit_application.submitted",
        "credit_application.review_started",
        "credit_application.approved",
        "credit_application.formalized",
    ]
    events = {e.event_type: e for e in audit()}
    for e in events.values():
        assert e.actor_id == tenant_a["admin_id"] and e.tenant_id == tenant_a["tenant_id"] and e.correlation_id
        assert e.details["application_id"] == d["id"]
    ap = events["credit_application.approved"].details
    assert ap["before"]["requested_amount"] == "10000.0000" and ap["after"]["approved_amount"] == "8000.0000"
    assert ap["reason"] == "capacidad verificada" and ap["rules_digest"] == w.v["rules_hash"]
    assert ap["product_version_id"] == w.v["id"]
    fz = events["credit_application.formalized"].details
    assert (
        fz["rules_digest"] == w.v["rules_hash"]
        and fz["contract_digest"].startswith("sha256:")
        and fz["reference"] == "FRM-000001"
    )
    assert fz["before"]["status"] == "approved" and fz["after"]["formalization_status"] == "ready_for_disbursement"
    blob = " ".join(str(e.details) for e in audit("credit_"))
    assert "Perez" not in blob and "001-0000001-1" not in blob  # no customer identity in the trail
    client.put(POLICY, headers=adm, json=OPEN_POLICY | {"limits_enforced": True})
    pol = audit("credit_approval_policy.set")[-1].details
    assert pol["before"]["limits_enforced"] is False and pol["after"]["limits_enforced"] is True


# ================================ migration ==========================================================
def test_migration_0008_upgrade_downgrade_reupgrade(scratch_db):
    from sqlalchemy import create_engine

    assert _alembic(scratch_db, "upgrade", "0007").returncode == 0
    eng = create_engine(scratch_db)
    try:
        with eng.begin() as c:
            c.execute(
                text(
                    "INSERT INTO companies (name, slug, tax_id, address, phone, status, base_currency_code, default_timezone, "
                    "created_at, updated_at) VALUES ('X', 'x-1', '', '', '', 'active', 'DOP', 'America/Santo_Domingo', now(), now())"
                )
            )
            c.execute(
                text(
                    "INSERT INTO tenant_currencies (tenant_id, currency_code, enabled_at) "
                    "SELECT id, 'DOP', now() FROM companies"
                )
            )
            c.execute(
                text(
                    "INSERT INTO roles (tenant_id, name, description, status, system_defined, created_at, updated_at) "
                    "SELECT id, 'Administrador de agencia', 'x', 'active', true, now(), now() FROM companies"
                )
            )
        up = _alembic(scratch_db, "upgrade", "head")
        assert up.returncode == 0, up.stderr
        with eng.connect() as c:
            assert (
                c.execute(
                    text(
                        "SELECT count(*) FROM permissions WHERE code LIKE 'credit.applications.%' "
                        "OR code LIKE 'credit.approval_%'"
                    )
                ).scalar()
                == 11
            )
            assert (
                c.execute(
                    text(
                        "SELECT count(*) FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id "
                        "WHERE p.code LIKE 'credit.applications.%' OR p.code LIKE 'credit.approval_%'"
                    )
                ).scalar()
                == 11
            )
            tables = {r[0] for r in c.execute(text("SELECT tablename FROM pg_tables WHERE tablename LIKE 'credit_%'"))}
            assert {
                "credit_applications",
                "credit_application_submissions",
                "credit_application_evaluations",
                "credit_decisions",
                "credit_approvals",
                "credit_application_conditions",
                "credit_application_document_links",
                "credit_formalizations",
                "credit_approval_policies",
                "credit_approval_limits",
            } <= tables
            triggers = {
                r[0] for r in c.execute(text("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'trg_credit_%_guard'"))
            }
            assert {
                "trg_credit_decisions_guard",
                "trg_credit_approvals_guard",
                "trg_credit_formalizations_guard",
                "trg_credit_application_evaluations_guard",
                "trg_credit_application_conditions_guard",
                "trg_credit_application_submissions_guard",
            } <= triggers
            idx = {
                r[0]
                for r in c.execute(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = 'credit_approval_policies'")
                )
            }
            assert {"uq_credit_approval_policies_tenant_default", "uq_credit_approval_policies_tenant_product"} <= idx
        down = _alembic(scratch_db, "downgrade", "0007")
        assert down.returncode == 0, down.stderr
        with eng.connect() as c:
            assert (
                c.execute(
                    text(
                        "SELECT count(*) FROM permissions WHERE code LIKE 'credit.applications.%' "
                        "OR code LIKE 'credit.approval_%'"
                    )
                ).scalar()
                == 0
            )
            assert c.execute(text("SELECT to_regclass('credit_applications')")).scalar() is None
            assert (
                c.execute(
                    text(
                        "SELECT count(*) FROM pg_proc WHERE proname IN ('origination_append_only', 'credit_formalizations_guard')"
                    )
                ).scalar()
                == 0
            )
            assert c.execute(text("SELECT to_regclass('credit_products')")).scalar() is not None  # T-005 untouched
        again = _alembic(scratch_db, "upgrade", "head")
        assert again.returncode == 0, again.stderr
        assert _alembic(scratch_db, "check").returncode == 0
    finally:
        eng.dispose()
