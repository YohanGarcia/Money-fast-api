"""T-003 Tenant / Branch / CashPoint / Currency foundation tests (T003-*). PostgreSQL only."""

import re
from pathlib import Path

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import event, text, update

from app.core.context import get_context
from app.core.db import SessionLocal, engine
from app.core.security import get_password_hash
from app.core.time import now_utc
from app.main import create_app
from app.models.branch import Branch
from app.models.company import Company
from app.modules.identity.audit import record_event  # noqa: F401
from app.modules.identity.authorization import build_principal
from app.modules.identity.catalog import ensure_system_role
from app.modules.identity.deps import get_auth_context
from app.modules.identity.errors import TenantInactive  # noqa: F401
from app.modules.identity.models import SecurityEvent, UserAccount, UserRoleAssignment
from app.modules.organization import service
from app.modules.organization.catalog import CURRENCY_CATALOG
from app.modules.organization.models import CashPoint, Currency
from app.modules.organization.seed import seed_test_organization
from tests import pg_env  # noqa: F401  (must precede app imports)
from tests.test_t001_foundation import _alembic, scratch_db  # noqa: F401
from tests.test_t002_identity import (  # noqa: F401  (fixtures + helpers shared with the T-002 suite)
    PW,
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

ROOT = Path(__file__).resolve().parents[1]


def mk_branch(client, hdr, code, name="Sucursal", expect=201, **extra):
    r = client.post(f"{V2}/branches", headers=hdr, json={"code": code, "name": name, **extra})
    assert r.status_code == expect, r.text
    return r.json()


def mk_cp(client, hdr, branch_id, code, currencies=(), expect=201):
    r = client.post(
        f"{V2}/cash-points",
        headers=hdr,
        json={"branch_id": branch_id, "code": code, "name": f"Caja {code}", "currencies": list(currencies)},
    )
    assert r.status_code == expect, r.text
    return r.json()


def events(prefix="org."):
    with SessionLocal() as db:
        rows = db.query(SecurityEvent).order_by(SecurityEvent.id).all()
        return [e for e in rows if e.event_type.startswith(prefix)]


# ================================ Tenant ================================================================
def test_t01_tenant_isolation(client, tenant_a, tenant_b):
    adm_a, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    assert client.get(f"{V2}/tenants/current", headers=adm_a).json()["code"] == "alfa"
    b_branch = mk_branch(client, adm_b, "B1")
    b_cp = mk_cp(client, adm_b, b_branch["id"], "CP1")
    mk_branch(client, adm_a, "A1")
    assert [b["code"] for b in client.get(f"{V2}/branches", headers=adm_a).json()] == [
        b["code"] for b in client.get(f"{V2}/branches", headers=adm_a).json() if b["tenant_id"] == tenant_a["tenant_id"]
    ]
    assert all(b["tenant_id"] == tenant_a["tenant_id"] for b in client.get(f"{V2}/branches", headers=adm_a).json())
    assert all(c["tenant_id"] == tenant_a["tenant_id"] for c in client.get(f"{V2}/cash-points", headers=adm_a).json())
    bid, cid = b_branch["id"], b_cp["id"]
    assert client.get(f"{V2}/branches/{bid}", headers=adm_a).status_code == 404
    assert client.put(f"{V2}/branches/{bid}", headers=adm_a, json={"name": "x"}).status_code == 404
    assert client.post(f"{V2}/branches/{bid}/disable", headers=adm_a).status_code == 404
    assert client.get(f"{V2}/cash-points/{cid}", headers=adm_a).status_code == 404
    assert client.post(f"{V2}/cash-points/{cid}/disable", headers=adm_a).status_code == 404
    assert client.get(f"{V2}/tenants/current/effective?branch_id={bid}", headers=adm_a).status_code == 404
    assert (
        client.post(f"{V2}/cash-points", headers=adm_a, json={"branch_id": bid, "code": "X", "name": "x"}).status_code
        == 404
    )
    # client-supplied tenant ids are rejected, headers are ignored
    r = client.post(
        f"{V2}/branches", headers=adm_a, json={"code": "Z", "name": "z", "tenant_id": tenant_b["tenant_id"]}
    )
    assert r.status_code == 422
    spoof = {**adm_a, "X-Tenant-ID": str(tenant_b["tenant_id"])}
    assert client.get(f"{V2}/tenants/current", headers=spoof).json()["id"] == tenant_a["tenant_id"]
    assert client.get(f"{V2}/branches/{bid}", headers=adm_b).status_code == 200  # untouched for its owner


def test_t02_inactive_tenant_rejects_new_operations(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    branch = mk_branch(client, adm, "A1")
    with SessionLocal() as db:
        principal = build_principal(db, db.get(UserAccount, tenant_a["admin_id"]), 1)
        db.get(Company, tenant_a["tenant_id"]).is_active = False  # legacy setter -> status inactive
        db.commit()
        with pytest.raises(Exception, match="inactiva"):
            service.create_branch(
                db, principal, code="A2", name="n", address="", phone="", timezone_override=None, client_ip=None
            )
        db.rollback()
    for path in ("/branches", "/tenants/current", "/users", "/auth/me"):
        r = client.get(f"{V2}{path}", headers=adm)
        assert r.status_code == 403 and r.json()["error"]["code"] == "tenant_inactive", path
    assert (
        login(client, tenant_a["email"], expect=401).json()["error"]["code"] == "invalid_credentials"
    )  # slug no longer resolves
    with SessionLocal() as db:  # history preserved: nothing was deleted
        assert db.get(Branch, branch["id"]) is not None and db.get(Company, tenant_a["tenant_id"]).status == "inactive"


def test_t03_tenant_code_is_unique_across_the_platform(tenant_a):
    with SessionLocal() as db:
        assert db.get(Company, tenant_a["tenant_id"]).code == "alfa"
        db.add(Company(name="Otra", slug="alfa"))
        with pytest.raises(Exception, match="uq_companies_slug"):
            db.commit()


# ================================ Branch ================================================================
def test_b01_b03_create_branch_codes_are_tenant_scoped(client, tenant_a, tenant_b):
    adm_a, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    b = mk_branch(client, adm_a, "centro", name="Centro", address="Calle 1", phone="809")
    assert b["code"] == "CENTRO" and b["status"] == "active" and b["tenant_id"] == tenant_a["tenant_id"]
    dup = mk_branch(client, adm_a, " Centro ", expect=409)
    assert dup["error"]["code"] == "conflict"
    assert mk_branch(client, adm_a, "bad code!", expect=422)["error"]["code"] == "validation_error"
    other = mk_branch(client, adm_b, "CENTRO")  # the same visible code in another tenant is fine
    assert other["id"] != b["id"] and other["tenant_id"] == tenant_b["tenant_id"]
    with SessionLocal() as db:  # the DB enforces both rules independently of the API
        db.add(Branch(company_id=tenant_a["tenant_id"], code="CENTRO", name="dup"))
        with pytest.raises(Exception, match="uq_branches_tenant_code"):
            db.commit()
        db.rollback()
        db.add(Branch(company_id=tenant_a["tenant_id"], code="lower", name="x"))
        with pytest.raises(Exception, match="code_format"):
            db.commit()
    assert [e.event_type for e in events("org.branch")] == ["org.branch.created", "org.branch.created"]


def test_b05_b06_timezone_inheritance_and_override(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    b1 = mk_branch(client, adm, "B1")
    b2 = mk_branch(client, adm, "B2", timezone_override="America/New_York")
    eff = lambda **q: client.get(f"{V2}/tenants/current/effective", headers=adm, params=q).json()  # noqa: E731
    assert eff() == {
        "tenant_id": tenant_a["tenant_id"],
        "tenant_code": "alfa",
        "branch_id": None,
        "timezone": "America/Santo_Domingo",
        "timezone_source": "tenant",
        "base_currency": "DOP",
        "enabled_currencies": ["DOP"],
    }
    assert (
        eff(branch_id=b1["id"])["timezone"] == "America/Santo_Domingo"
        and eff(branch_id=b1["id"])["timezone_source"] == "tenant"
    )
    assert (
        eff(branch_id=b2["id"])["timezone"] == "America/New_York"
        and eff(branch_id=b2["id"])["timezone_source"] == "branch"
    )
    # tenant default change flows to inheriting branches only
    r = client.put(f"{V2}/tenants/current/settings", headers=adm, json={"default_timezone": "America/Mexico_City"})
    assert r.status_code == 200 and r.json()["default_timezone"] == "America/Mexico_City"
    assert eff(branch_id=b1["id"])["timezone"] == "America/Mexico_City"
    assert eff(branch_id=b2["id"])["timezone"] == "America/New_York"
    # override can be changed and cleared
    assert (
        client.put(f"{V2}/branches/{b1['id']}", headers=adm, json={"timezone_override": "Europe/Madrid"}).status_code
        == 200
    )
    assert eff(branch_id=b1["id"])["timezone"] == "Europe/Madrid"
    assert (
        client.put(f"{V2}/branches/{b1['id']}", headers=adm, json={"clear_timezone_override": True}).json()[
            "timezone_override"
        ]
        is None
    )
    assert eff(branch_id=b1["id"])["timezone_source"] == "tenant"
    # IANA only: fixed offsets and junk are rejected everywhere
    for bad in ("UTC-4", "GMT+4", "Mars/Base", "-04:00"):
        assert (
            client.put(f"{V2}/tenants/current/settings", headers=adm, json={"default_timezone": bad}).status_code == 422
        ), bad
        assert (
            client.put(f"{V2}/branches/{b1['id']}", headers=adm, json={"timezone_override": bad}).status_code == 422
        ), bad
        mk_branch(client, adm, "BAD", timezone_override=bad, expect=422)
    # audited with before/after
    ev = [e for e in events("org.") if e.event_type in ("org.tenant.settings_changed", "org.branch.timezone_changed")]
    assert ev[0].details["before"]["default_timezone"] == "America/Santo_Domingo"
    assert ev[0].details["after"]["default_timezone"] == "America/Mexico_City"
    assert {e.event_type for e in ev} == {"org.tenant.settings_changed", "org.branch.timezone_changed"}
    assert all(
        e.actor_id == tenant_a["admin_id"] and e.correlation_id and e.tenant_id == tenant_a["tenant_id"] for e in ev
    )


def test_branch_disable_enable_keeps_history_and_blocks_new_cash_points(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    b = mk_branch(client, adm, "B1")
    cp = mk_cp(client, adm, b["id"], "C1")
    assert client.post(f"{V2}/branches/{b['id']}/disable", headers=adm).json()["status"] == "inactive"
    assert (
        client.post(f"{V2}/branches/{b['id']}/disable", headers=adm).json()["error"]["code"]
        == "invalid_state_transition"
    )
    mk_cp(client, adm, b["id"], "C2", expect=422)  # a new cash point in an inactive branch is refused
    assert client.get(f"{V2}/branches/{b['id']}", headers=adm).json()["status"] == "inactive"  # still readable
    with SessionLocal() as db:
        with pytest.raises(Exception, match="no esta disponible"):
            service.ensure_cash_point_usable(db, tenant_a["tenant_id"], cp["id"])
    assert client.post(f"{V2}/branches/{b['id']}/enable", headers=adm).json()["status"] == "active"
    with SessionLocal() as db:
        assert service.ensure_cash_point_usable(db, tenant_a["tenant_id"], cp["id"]).id == cp["id"]
    assert {"org.branch.disabled", "org.branch.enabled"} <= {e.event_type for e in events()}


# ================================ CashPoint =============================================================
def test_c01_c02_many_cash_points_per_branch_and_no_cross_tenant_mismatch(client, sink, tenant_a, tenant_b):
    adm_a, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    a_branch, b_branch = mk_branch(client, adm_a, "A1"), mk_branch(client, adm_b, "B1")
    cps = [mk_cp(client, adm_a, a_branch["id"], f"C{i}") for i in range(3)]
    assert len({c["branch_id"] for c in cps}) == 1  # Branch 1 -> N CashPoints
    assert len(client.get(f"{V2}/cash-points", headers=adm_a, params={"branch_id": a_branch["id"]}).json()) == 3
    mk_cp(client, adm_a, a_branch["id"], "c0", expect=409)  # code is unique per tenant (case-insensitive)
    assert (
        mk_cp(client, adm_b, b_branch["id"], "C0")["tenant_id"] == tenant_b["tenant_id"]
    )  # other tenant: same code ok
    assert mk_cp(client, adm_a, b_branch["id"], "X", expect=404)  # A cannot attach to B's branch
    with SessionLocal() as db:  # database-level tenant safety (composite foreign keys)
        db.add(CashPoint(tenant_id=tenant_a["tenant_id"], branch_id=b_branch["id"], code="EVIL", name="x"))
        with pytest.raises(Exception, match="fk_cash_points_tenant_branch"):
            db.commit()
        db.rollback()
        db.add(
            UserAccount(
                full_name="x",
                email="x@example.com",
                password_hash="h",
                company_id=tenant_a["tenant_id"],
                branch_id=b_branch["id"],
            )
        )
        with pytest.raises(Exception, match="fk_users_tenant_branch"):
            db.commit()
        db.rollback()
        role = ensure_system_role(db, tenant_a["tenant_id"])
        db.add(
            UserRoleAssignment(
                tenant_id=tenant_a["tenant_id"],
                user_id=tenant_a["admin_id"],
                role_id=role.id,
                scope_kind="branch",
                branch_id=b_branch["id"],
            )
        )
        with pytest.raises(Exception, match="fk_assignments_tenant_branch"):
            db.commit()


def test_c03_unusable_cash_points_cannot_be_selected_for_new_sessions(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    b = mk_branch(client, adm, "B1")
    cp = mk_cp(client, adm, b["id"], "C1")
    t = tenant_a["tenant_id"]
    with SessionLocal() as db:
        assert service.ensure_cash_point_usable(db, t, cp["id"]).status == "active"
    assert client.post(f"{V2}/cash-points/{cp['id']}/disable", headers=adm).json()["status"] == "inactive"
    with SessionLocal() as db, pytest.raises(Exception, match="no esta disponible"):
        service.ensure_cash_point_usable(db, t, cp["id"])
    assert client.post(f"{V2}/cash-points/{cp['id']}/enable", headers=adm).json()["status"] == "active"
    sus = client.post(f"{V2}/cash-points/{cp['id']}/suspend", headers=adm, json={"reason": "Revision administrativa"})
    assert sus.status_code == 200 and sus.json()["status"] == "suspended" and sus.json()["suspension_reason"]
    with SessionLocal() as db, pytest.raises(Exception, match="no esta disponible"):
        service.ensure_cash_point_usable(db, t, cp["id"])
    assert (
        client.post(f"{V2}/cash-points/{cp['id']}/enable", headers=adm).json()["error"]["code"]
        == "invalid_state_transition"
    )
    assert client.post(f"{V2}/cash-points/{cp['id']}/resume", headers=adm).json()["status"] == "active"
    with SessionLocal() as db:
        assert service.ensure_cash_point_usable(db, t, cp["id"]).suspension_reason is None
    types = [e.event_type for e in events("org.cash_point")]
    assert types == [
        "org.cash_point.created",
        "org.cash_point.disabled",
        "org.cash_point.enabled",
        "org.cash_point.suspended",
        "org.cash_point.resumed",
    ]
    suspended = events("org.cash_point.suspended")[0]
    assert suspended.details["before"]["status"] == "active" and suspended.details["after"]["suspension_reason"]


def test_c04_pending_difference_never_suspends_a_cash_point(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    b = mk_branch(client, adm, "B1")
    cp = mk_cp(client, adm, b["id"], "C1")
    # a cash session closed with a difference pending review (T-021 record) must not touch the cash point
    with SessionLocal() as db:
        from decimal import Decimal

        from app.models.cash import CashBox, CashSessionDifference
        from tests.cash_fixtures import close_with_handover, open_v2_session, receiver_user

        box = CashBox(company_id=tenant_a["tenant_id"], branch_id=b["id"], initial_balance=Decimal("0"))
        db.add(box)
        db.flush()
        s = open_v2_session(
            db, box_id=box.id, cashier_id=tenant_a["admin_id"], balance="100.00", cash_point_id=cp["id"]
        )
        close_with_handover(db, s, receiver_user(db, tenant_a["tenant_id"]), counted="90.00", note="Faltan 10")
        db.commit()
        assert db.query(CashSessionDifference).filter_by(session_id=s.id).one().status == "pending_review"
    with SessionLocal() as db:
        assert db.get(CashPoint, cp["id"]).status == "active"
        assert service.ensure_cash_point_usable(db, tenant_a["tenant_id"], cp["id"]).status == "active"
    # structurally: only the explicit organization service writes a cash point's status
    offenders = []
    for path in (ROOT / "app").rglob("*.py"):
        text_ = path.read_text(encoding="utf-8", errors="ignore")
        if "app/modules/organization" in path.as_posix() or "alembic" in path.as_posix():
            continue
        if re.search(r"CashPoint\b", text_) and re.search(r"status\s*=\s*['\"]suspended", text_):
            offenders.append(str(path))
    assert offenders == []
    # suspension needs its own permission and a reason
    assert client.post(f"{V2}/cash-points/{cp['id']}/suspend", headers=adm, json={"reason": "ab"}).status_code == 422
    with SessionLocal() as db, pytest.raises(Exception, match="suspension_consistent"):
        db.execute(update(CashPoint).where(CashPoint.id == cp["id"]).values(status="suspended"))  # needs suspended_at
        db.commit()


# ================================ Currency ==============================================================
def test_m01_base_currency_must_be_enabled_and_cannot_be_disabled(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    put = lambda body: client.put(f"{V2}/tenants/current/settings", headers=adm, json=body)  # noqa: E731
    r = put({"base_currency_code": "USD"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "business_rule_violation"
    assert client.post(f"{V2}/tenant/currencies", headers=adm, json={"code": "usd"}).status_code == 201
    ok = put({"base_currency_code": "usd"})
    assert ok.status_code == 200 and ok.json()["base_currency_code"] == "USD"
    ev = events("org.tenant.settings_changed")[-1]
    assert ev.details["before"]["base_currency_code"] == "DOP" and ev.details["after"]["base_currency_code"] == "USD"
    assert client.delete(f"{V2}/tenant/currencies/USD", headers=adm).status_code == 422  # base cannot be disabled
    assert put({"base_currency_code": "EUR"}).status_code == 422  # in the catalogue but not enabled for the tenant
    assert put({"base_currency_code": "XXX"}).status_code == 422
    # DB level: the base currency must be one of the tenant's own currencies (deferred composite FK)
    with SessionLocal() as db:
        db.execute(update(Company).where(Company.id == tenant_a["tenant_id"]).values(base_currency_code="EUR"))
        with pytest.raises(Exception, match="fk_companies_base_currency_enabled"):
            db.commit()


def test_m02_m03_disabled_currency_is_unavailable_but_history_stays_queryable(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    client.post(f"{V2}/tenant/currencies", headers=adm, json={"code": "USD"})
    b = mk_branch(client, adm, "B1")
    cp = mk_cp(client, adm, b["id"], "C1", currencies=["DOP", "USD"])
    assert cp["allowed_currencies"] == ["DOP", "USD"]
    assert client.delete(f"{V2}/tenant/currencies/USD", headers=adm).status_code == 204
    with SessionLocal() as db:
        with pytest.raises(Exception, match="no esta habilitada"):
            service.ensure_cash_point_usable(db, tenant_a["tenant_id"], cp["id"], currency="USD")
        assert service.ensure_cash_point_usable(db, tenant_a["tenant_id"], cp["id"], currency="DOP")
    # new use is refused ...
    r = client.put(f"{V2}/cash-points/{cp['id']}/currencies", headers=adm, json={"currencies": ["DOP", "USD"]})
    assert r.status_code == 422
    # ... but the configuration history is still there
    rows = {c["code"]: c for c in client.get(f"{V2}/tenant/currencies", headers=adm).json()}
    assert rows["USD"]["enabled"] is False and rows["USD"]["disabled_at"] and rows["DOP"]["is_base"] is True
    assert client.get(f"{V2}/cash-points/{cp['id']}", headers=adm).json()["allowed_currencies"] == ["DOP", "USD"]
    assert (
        client.delete(f"{V2}/tenant/currencies/USD", headers=adm).json()["error"]["code"] == "invalid_state_transition"
    )
    assert (
        client.post(f"{V2}/tenant/currencies", headers=adm, json={"code": "USD"}).status_code == 201
    )  # re-enable reuses the row
    with SessionLocal() as db:
        assert service.ensure_cash_point_usable(db, tenant_a["tenant_id"], cp["id"], currency="USD")
        with pytest.raises(Exception, match="no admite"):
            service.ensure_cash_point_usable(db, tenant_a["tenant_id"], mk_cp_id(db, tenant_a, b["id"]), currency="USD")
    assert {"org.currency.enabled", "org.currency.disabled"} <= {e.event_type for e in events()}


def mk_cp_id(db, tenant, branch_id):
    cp = CashPoint(tenant_id=tenant["tenant_id"], branch_id=branch_id, code="NARROW", name="n")
    db.add(cp)
    db.flush()
    from app.modules.organization.models import CashPointCurrency

    db.add(CashPointCurrency(cash_point_id=cp.id, currency_code="DOP", tenant_id=tenant["tenant_id"]))
    db.flush()
    return cp.id


def test_m04_decimal_exponent_metadata_and_constraints(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    catalog = {c["code"]: c for c in client.get(f"{V2}/currencies", headers=adm).json()}
    assert set(catalog) >= {c[0] for c in CURRENCY_CATALOG}
    assert (
        catalog["DOP"]["exponent"] == 2
        and catalog["USD"]["exponent"] == 2
        and isinstance(catalog["DOP"]["exponent"], int)
    )
    with SessionLocal() as db:
        assert {c.code: c.exponent for c in db.query(Currency)}["DOP"] == 2
        db.add(Currency(code="ZZZ", name="bad", exponent=9))
        with pytest.raises(Exception, match="exponent_range"):
            db.commit()
        db.rollback()
        db.add(Currency(code="zz", name="bad", exponent=2))
        with pytest.raises(Exception, match="code_format"):
            db.commit()
        db.rollback()
        # precision metadata is an integer column; there is no floating-point money in this package
        types = {
            r[0]: r[1]
            for r in db.execute(
                text("SELECT column_name, data_type FROM information_schema.columns WHERE table_name='currencies'")
            )
        }
        assert types["exponent"] == "smallint"
    src = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "app/modules/organization").glob("*.py"))
    assert not re.search(r"\bfloat\b|Float\(", src)


# ================================ Authorization =========================================================
def test_a01_a02_branch_scoped_user_cannot_manage_another_branch(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    b1, b2 = mk_branch(client, adm, "B1"), mk_branch(client, adm, "B2")
    c1, c2 = mk_cp(client, adm, b1["id"], "C1"), mk_cp(client, adm, b2["id"], "C2")
    role = create_role(
        client,
        adm,
        "Gerente de sucursal",
        [
            "organization.branches.read",
            "organization.branches.manage",
            "cash.points.read",
            "cash.points.manage",
            "tenant.settings.read",
        ],
    )
    activate_user(client, sink, adm, "mgr@example.com", roles=[role], scope="branch", branch_id=b1["id"])
    mh = h(login(client, "mgr@example.com"))
    assert [b["id"] for b in client.get(f"{V2}/branches", headers=mh).json()] == [b1["id"]]
    assert client.get(f"{V2}/branches/{b1['id']}", headers=mh).status_code == 200
    assert client.get(f"{V2}/branches/{b2['id']}", headers=mh).status_code == 403
    assert client.put(f"{V2}/branches/{b1['id']}", headers=mh, json={"name": "Mi sucursal"}).status_code == 200
    assert client.put(f"{V2}/branches/{b2['id']}", headers=mh, json={"name": "hack"}).status_code == 403
    assert client.post(f"{V2}/branches/{b2['id']}/disable", headers=mh).status_code == 403
    assert (
        client.post(f"{V2}/branches", headers=mh, json={"code": "NEW", "name": "n"}).status_code == 403
    )  # tenant-wide only
    assert [c["id"] for c in client.get(f"{V2}/cash-points", headers=mh).json()] == [c1["id"]]
    assert client.get(f"{V2}/cash-points/{c2['id']}", headers=mh).status_code == 403
    assert client.post(f"{V2}/cash-points/{c2['id']}/disable", headers=mh).status_code == 403
    assert mk_cp(client, mh, b2["id"], "C9", expect=403)
    assert mk_cp(client, mh, b1["id"], "C3")["branch_id"] == b1["id"]
    assert client.get(f"{V2}/tenants/current/effective", headers=mh).status_code == 403  # tenant view needs tenant-wide
    assert client.get(f"{V2}/tenants/current/effective?branch_id={b1['id']}", headers=mh).status_code == 200
    assert client.get(f"{V2}/tenants/current/effective?branch_id={b2['id']}", headers=mh).status_code == 403
    assert (
        client.put(f"{V2}/tenants/current/settings", headers=mh, json={"default_timezone": "Europe/Madrid"}).status_code
        == 403
    )
    # a tenant-wide admin manages every branch (A02)
    assert client.put(f"{V2}/branches/{b2['id']}", headers=adm, json={"name": "Otra"}).status_code == 200


def test_cash_point_scope_reaches_only_that_cash_point(client, sink, tenant_a, tenant_b):
    adm, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    b1 = mk_branch(client, adm, "B1")
    c1, c2 = mk_cp(client, adm, b1["id"], "C1"), mk_cp(client, adm, b1["id"], "C2")
    foreign = mk_cp(client, adm_b, mk_branch(client, adm_b, "BB")["id"], "CF")
    role = create_role(client, adm, "Responsable de caja", ["cash.points.read", "cash.points.manage"])
    user = activate_user(client, sink, adm, "cashmgr@example.com")
    ok = client.post(
        f"{V2}/users/{user['id']}/roles",
        headers=adm,
        json={"role_id": role["id"], "scope": "cash_point", "cash_point_id": c1["id"]},
    )
    assert ok.status_code == 201 and ok.json()["cash_point_id"] == c1["id"]
    assert (
        client.post(
            f"{V2}/users/{user['id']}/roles",
            headers=adm,
            json={"role_id": role["id"], "scope": "cash_point", "cash_point_id": c1["id"]},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"{V2}/users/{user['id']}/roles", headers=adm, json={"role_id": role["id"], "scope": "cash_point"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{V2}/users/{user['id']}/roles",
            headers=adm,
            json={"role_id": role["id"], "scope": "cash_point", "cash_point_id": foreign["id"]},
        ).status_code
        == 404
    )
    uh = h(login(client, "cashmgr@example.com"))
    assert [c["id"] for c in client.get(f"{V2}/cash-points", headers=uh).json()] == [c1["id"]]
    assert (
        client.put(f"{V2}/cash-points/{c1['id']}/currencies", headers=uh, json={"currencies": ["DOP"]}).status_code
        == 200
    )
    assert (
        client.put(f"{V2}/cash-points/{c2['id']}/currencies", headers=uh, json={"currencies": ["DOP"]}).status_code
        == 403
    )
    assert client.get(f"{V2}/cash-points/{c2['id']}", headers=uh).status_code == 403
    assert client.get(f"{V2}/branches", headers=uh).status_code == 403  # a cash point grant is not a branch grant
    me = client.get(f"{V2}/auth/me", headers=uh).json()
    assert {g["permission"] for g in me["permissions"]} == {"cash.points.read", "cash.points.manage"}


def test_a03_platform_scope_stays_distinct(client, tenant_a):
    with SessionLocal() as db:
        platform_role = ensure_system_role(db, None)
        user = UserAccount(
            full_name="Plat",
            email="plat@example.com",
            password_hash=get_password_hash(PW),
            company_id=None,
            activated_at=now_utc(),
        )
        db.add(user)
        db.flush()
        db.add(UserRoleAssignment(tenant_id=None, user_id=user.id, role_id=platform_role.id, scope_kind="tenant"))
        db.commit()
    tokens = client.post(
        f"{V2}/auth/login", json={"email": "plat@example.com", "password": PW}
    ).json()  # no slug = platform
    ph = h(tokens)
    me = client.get(f"{V2}/auth/me", headers=ph).json()
    assert {g["permission"] for g in me["permissions"]} == {"platform.users.read", "platform.users.disable"}
    for method, path, body in (
        ("get", "/branches", None),
        ("get", "/tenants/current", None),
        ("get", "/cash-points", None),
        ("get", "/tenant/currencies", None),
        ("post", "/branches", {"code": "X", "name": "x"}),
        ("put", "/tenants/current/settings", {"default_timezone": "America/New_York"}),
        ("post", "/tenant/currencies", {"code": "USD"}),
    ):
        r = getattr(client, method)(f"{V2}{path}", headers=ph, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (path, r.status_code)
    assert client.get(f"{V2}/branches", headers=ph).status_code == 403
    # and a tenant admin does not hold platform capabilities
    adm = admin_headers(client, tenant_a)
    assert not any(
        g["permission"].startswith("platform.") for g in client.get(f"{V2}/auth/me", headers=adm).json()["permissions"]
    )


# ================================ Context / reads / seeds / migration ====================================
def test_request_context_resolves_tenant_branch_timezone_and_base_currency(client, sink, tenant_a, tenant_b):
    adm = admin_headers(client, tenant_a)
    b = mk_branch(client, adm, "B1", timezone_override="America/New_York")
    client.post(f"{V2}/tenant/currencies", headers=adm, json={"code": "USD"})
    client.put(f"{V2}/tenants/current/settings", headers=adm, json={"base_currency_code": "USD"})
    user = activate_user(client, sink, adm, "branchuser@example.com")
    with SessionLocal() as db:
        db.execute(update(UserAccount).where(UserAccount.id == user["id"]).values(branch_id=b["id"]))
        db.commit()
    isolated = create_app()

    @isolated.get("/ctx")
    def ctx(_=Depends(get_auth_context)):
        c = get_context()
        return {
            "tenant": c.tenant_id,
            "branch": c.branch_id,
            "tz": c.timezone,
            "base": c.base_currency,
            "actor": c.actor_id,
        }

    spoof = {
        "X-Tenant-ID": str(tenant_b["tenant_id"]),
        "X-Branch-ID": "999",
        "X-Timezone": "Asia/Tokyo",
        "X-Base-Currency": "EUR",
    }
    with TestClient(isolated) as c2:
        got = c2.get("/ctx", headers={**h(login(client, "branchuser@example.com")), **spoof}).json()
        admin_ctx = c2.get("/ctx", headers=adm).json()
    assert got == {
        "tenant": str(tenant_a["tenant_id"]),
        "branch": str(b["id"]),
        "tz": "America/New_York",
        "base": "USD",
        "actor": str(user["id"]),
    }
    assert admin_ctx["tz"] == "America/Santo_Domingo" and admin_ctx["branch"] is None


def test_reads_never_write(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    b = mk_branch(client, adm, "B1")
    cp = mk_cp(client, adm, b["id"], "C1", currencies=["DOP"])
    statements = []

    def before(conn, cursor, statement, parameters, context, executemany):
        if re.match(r"\s*(INSERT|UPDATE|DELETE)", statement, re.I):
            statements.append(statement[:80])

    event.listen(engine, "before_cursor_execute", before)
    try:
        for path in (
            "/tenants/current",
            "/tenants/current/effective",
            f"/tenants/current/effective?branch_id={b['id']}",
            "/branches",
            f"/branches/{b['id']}",
            "/cash-points",
            f"/cash-points/{cp['id']}",
            "/currencies",
            "/tenant/currencies",
            "/auth/me",
            "/users",
            "/roles",
            "/security/events",
        ):
            assert client.get(f"{V2}{path}", headers=adm).status_code == 200, path
    finally:
        event.remove(engine, "before_cursor_execute", before)
    assert statements == []


def test_seed_creates_two_tenants_with_branches_cash_points_and_is_idempotent():
    with SessionLocal() as db:
        first = seed_test_organization(db)
        db.commit()
        second = seed_test_organization(db)
        db.commit()
        assert first == second == {"tenants": 2, "branches": 4, "cash_points": 4 + 1}
        from sqlalchemy import select

        by_slug = {c.slug: c for c in db.scalars(select(Company))}
        assert (
            by_slug["dominicana"].default_timezone == "America/Santo_Domingo"
            and by_slug["dominicana"].base_currency_code == "DOP"
        )
        assert (
            by_slug["nueva-york"].default_timezone == "America/New_York"
            and by_slug["nueva-york"].base_currency_code == "USD"
        )
        for tenant in by_slug.values():
            codes = {
                tc.currency_code
                for tc in db.execute(text("SELECT * FROM tenant_currencies WHERE tenant_id=:t"), {"t": tenant.id})
            }
            assert {"DOP", "USD"} <= codes
            branches = db.query(Branch).filter_by(company_id=tenant.id).all()
            assert len(branches) == 2
            assert all(db.query(CashPoint).filter_by(branch_id=b.id).count() >= 1 for b in branches)


def test_migration_reconciles_legacy_companies_branches_and_cash_boxes(scratch_db):
    from sqlalchemy import create_engine

    assert _alembic(scratch_db, "upgrade", "0004").returncode == 0
    eng = create_engine(scratch_db)
    try:
        with eng.begin() as c:
            c.execute(
                text(
                    "INSERT INTO companies (name, slug, tax_id, address, phone, is_active, created_at) "
                    "VALUES ('Vieja SA', 'vieja', '', '', '', true, now()), "
                    "('Dormida', 'dormida', '', '', '', false, now())"
                )
            )
            c.execute(
                text(
                    "INSERT INTO branches (name, address, manager_name, notary_name, phone, is_active, "
                    "company_id, created_at) "
                    "SELECT 'Central', 'x', 'm', 'n', '1', true, id, now() FROM companies WHERE slug='vieja'"
                )
            )
            c.execute(
                text(
                    "INSERT INTO cash_boxes (company_id, branch_id, initial_balance, version) "
                    "SELECT company_id, id, 0, 1 FROM branches"
                )
            )
        up = _alembic(scratch_db, "upgrade", "head")
        assert up.returncode == 0, up.stderr
        with eng.connect() as c:
            rows = {
                r[0]: r[1:]
                for r in c.execute(text("SELECT slug, status, base_currency_code, default_timezone FROM companies"))
            }
            assert rows["vieja"] == ("active", "DOP", "America/Santo_Domingo") and rows["dormida"][0] == "inactive"
            assert (
                c.execute(
                    text("SELECT count(*) FROM tenant_currencies WHERE currency_code='DOP' AND disabled_at IS NULL")
                ).scalar()
                == 2
            )
            br = c.execute(text("SELECT code, status, timezone_override FROM branches")).one()
            assert br[1] == "active" and br[2] is None and re.fullmatch(r"SUC-\d+", br[0])
            cp = c.execute(text("SELECT code, status FROM cash_points")).one()
            assert re.fullmatch(r"CAJA-\d+", cp[0]) and cp[1] == "active"
            assert (
                c.execute(
                    text(
                        "SELECT count(*) FROM permissions "
                        "WHERE code IN ('cash.points.suspend','tenant.settings.manage')"
                    )
                ).scalar()
                == 2
            )
        assert _alembic(scratch_db, "check").returncode == 0
        down = _alembic(scratch_db, "downgrade", "0004")
        assert down.returncode == 0, down.stderr
        with eng.connect() as c:
            assert dict(c.execute(text("SELECT slug, is_active FROM companies")).all()) == {
                "vieja": True,
                "dormida": False,
            }
            assert c.execute(text("SELECT is_active FROM branches")).scalar() is True
        assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    finally:
        eng.dispose()
