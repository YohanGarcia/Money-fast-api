"""T-005 Credit Product Engine tests (T005-*). PostgreSQL only: versioning, immutability, tenant isolation,
permissions, audit, simulation without writes, concurrency and the migration."""

import copy
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from app.core.db import SessionLocal, engine
from app.core.time import business_date
from app.modules.credit.models import CreditProductCurrency, CreditProductVersion
from app.modules.identity.models import SecurityEvent
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
from tests.test_t005_engine import CUR, rules

P = f"{V2}/credit-products"
TZ = "America/Santo_Domingo"


def today():
    return business_date(tz=TZ)


def mk_product(client, hdr, code="PRD-1", expect=201):
    r = client.post(P, headers=hdr, json={"code": code, "name": f"Producto {code}"})
    assert r.status_code == expect, r.text
    return r.json()


def mk_version(client, hdr, pid, raw=None, cur=None, expect=201, **extra):
    body = {"rules": raw or rules(), "currencies": CUR if cur is None else cur, **extra}
    r = client.post(f"{P}/{pid}/versions", headers=hdr, json=body)
    assert r.status_code == expect, r.text
    return r.json()


def publish(client, hdr, pid, vid, eff=None, expect=200, validate=True):
    if validate:
        v = client.post(f"{P}/{pid}/versions/{vid}/validate", headers=hdr)
        assert v.status_code == 200 and v.json()["valid"], v.text
    row = client.get(f"{P}/{pid}/versions/{vid}", headers=hdr).json()["row_version"]
    r = client.post(
        f"{P}/{pid}/versions/{vid}/publish",
        headers=hdr,
        json={"row_version": row, "effective_from": str(eff or today())},
    )
    assert r.status_code == expect, r.text
    return r.json()


def flow(client, hdr, code="PRD-1", raw=None, eff=None):
    p = mk_product(client, hdr, code)
    v = mk_version(client, hdr, p["id"], raw)
    return p, publish(client, hdr, p["id"], v["id"], eff)


def audit(prefix="credit_product"):
    with SessionLocal() as db:
        return [e for e in db.query(SecurityEvent).order_by(SecurityEvent.id) if e.event_type.startswith(prefix)]


SIM = {"currency": "DOP", "principal": "10000", "term_periods": 3, "start_date": "2026-01-15"}


# ================================ Versioning (V01-V07) ===============================================
def test_v01_create_draft_product(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    p = mk_product(client, adm, "prd-ab")
    assert p["code"] == "PRD-AB" and p["status"] == "draft" and p["tenant_id"] == tenant_a["tenant_id"]
    assert p["versions"] == [] and mk_product(client, adm, "PRD-AB", expect=409)
    spoof = client.post(P, headers=adm, json={"code": "X1", "name": "x", "tenant_id": 99})
    assert spoof.status_code == 422  # tenant comes from the session only
    assert client.post(P, headers=adm, json={"code": "bad code!", "name": "x"}).status_code == 409
    ev = audit()[0]
    assert ev.event_type == "credit_product.created" and ev.actor_id == tenant_a["admin_id"] and ev.correlation_id


def test_v02_create_versions_numbered_and_incomplete_draft_allowed(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    p = mk_product(client, adm)
    v1 = mk_version(client, adm, p["id"])
    sketch = mk_version(client, adm, p["id"], raw={"schema_version": 1}, cur=[])  # a draft may be incomplete
    assert (v1["version_number"], sketch["version_number"], v1["status"]) == (1, 2, "draft")
    bad = client.post(f"{P}/{p['id']}/versions/{sketch['id']}/validate", headers=adm).json()
    assert bad["valid"] is False and {"rules.calendar", "rules.rounding", "currencies"} <= {
        i["path"] for i in bad["issues"]
    }
    both = client.post(f"{P}/{p['id']}/versions", headers=adm, json={"rules": rules(), "based_on_version_id": v1["id"]})
    assert both.status_code == 422
    row = client.get(f"{P}/{p['id']}/versions/{sketch['id']}", headers=adm).json()
    upd = client.put(
        f"{P}/{p['id']}/versions/{sketch['id']}",
        headers=adm,
        json={"row_version": row["row_version"], "rules": rules(), "currencies": CUR},
    )
    assert (
        upd.status_code == 200
        and upd.json()["validated"] is False
        and upd.json()["row_version"] == row["row_version"] + 1
    )
    stale = client.put(
        f"{P}/{p['id']}/versions/{sketch['id']}",
        headers=adm,
        json={"row_version": row["row_version"], "rules": rules()},
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
    assert (
        client.put(
            f"{P}/{p['id']}/versions/{sketch['id']}",
            headers=adm,
            json={"row_version": 99, "rules": {"method": {"rate": {"value": 1.5}}}},
        ).status_code
        == 422
    )  # float refused


def test_v03_publish_flow_freezes_rules_hash_and_snapshot(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    p = mk_product(client, adm)
    v = mk_version(client, adm, p["id"])
    row = v["row_version"]
    unvalidated = client.post(
        f"{P}/{p['id']}/versions/{v['id']}/publish",
        headers=adm,
        json={"row_version": row, "effective_from": str(today())},
    )
    assert (
        unvalidated.status_code == 422 and unvalidated.json()["error"]["code"] == "business_rule_violation"
    )  # must validate first
    val = client.post(f"{P}/{p['id']}/versions/{v['id']}/validate", headers=adm).json()
    assert (
        val["valid"]
        and val["rules_hash"].startswith("sha256:")
        and any("BLOCKED_BY_SPEC" in w for w in val["warnings"])
    )
    past = client.post(
        f"{P}/{p['id']}/versions/{v['id']}/publish",
        headers=adm,
        json={"row_version": row + 0, "effective_from": str(today() - timedelta(days=1))},
    )
    assert past.status_code in (409, 422)  # stale row_version after validate OR past date: never published
    pub = publish(client, adm, p["id"], v["id"], validate=False)
    assert pub["status"] == "published" and pub["rules_hash"] == val["rules_hash"] and pub["effective_to"] is None
    prod = client.get(f"{P}/{p['id']}", headers=adm).json()
    assert prod["status"] == "active" and [x["status"] for x in prod["versions"]] == ["published"]
    snap = client.get(f"{P}/{p['id']}/versions/{v['id']}/snapshot", headers=adm).json()
    s = snap["snapshot"]
    assert snap["hash_verified"] is True and s["rules_hash"] == pub["rules_hash"] == snap["rules_hash"]
    assert (s["tenant_id"], s["product_id"], s["product_version_id"], s["version_number"]) == (
        tenant_a["tenant_id"],
        p["id"],
        v["id"],
        1,
    )
    assert s["rules"]["method"]["rate"]["value"] == "12" and s["currencies"][0]["code"] == "DOP"  # canonical decimals
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
        "restructure",
        "refinance",
    } <= set(s["rules"])
    ev = [e for e in audit("credit_product_version.published")][0]
    assert ev.details["rules_digest"] == pub["rules_hash"] and ev.actor_id == tenant_a["admin_id"] and ev.correlation_id
    assert ev.details["before"]["status"] == "draft" and ev.details["after"]["status"] == "published"
    assert any(e.event_type == "credit_product_version.validated" for e in audit())


def test_v04_published_version_is_immutable_in_api_and_database(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    p, v = flow(client, adm)
    base = f"{P}/{p['id']}/versions/{v['id']}"
    r = client.put(
        base,
        headers=adm,
        json={
            "row_version": v["row_version"],
            "rules": rules(grace={"delinquency_grace_days": 9, "principal_grace_periods": 0}),
        },
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_immutable"
    assert client.put(base, headers=adm, json={"row_version": v["row_version"], "currencies": CUR}).status_code == 409
    assert client.post(f"{base}/validate", headers=adm).status_code == 409
    assert (
        client.post(
            f"{base}/publish",
            headers=adm,
            json={"row_version": v["row_version"], "effective_from": str(today() + timedelta(days=9))},
        ).status_code
        == 409
    )
    # DB layer: even a direct write cannot change what was published
    forbidden = [
        "UPDATE credit_product_versions SET rules = '{}'::jsonb WHERE id = :id",
        "UPDATE credit_product_versions SET rules_hash = 'sha256:x' WHERE id = :id",
        "UPDATE credit_product_versions SET snapshot = '{}'::jsonb WHERE id = :id",
        "UPDATE credit_product_versions SET effective_from = effective_from + 1 WHERE id = :id",
        "UPDATE credit_product_versions SET status = 'draft', rules_hash = NULL, snapshot = NULL, effective_from = NULL, "
        "published_at = NULL, published_by = NULL WHERE id = :id",
        "DELETE FROM credit_product_versions WHERE id = :id",
        "DELETE FROM credit_product_currencies WHERE version_id = :id",
        "UPDATE credit_product_currencies SET max_amount = max_amount + 1 WHERE version_id = :id",
        "INSERT INTO credit_product_currencies (version_id, currency_code, tenant_id, min_amount, max_amount) "
        "SELECT id, 'USD', tenant_id, 1, 2 FROM credit_product_versions WHERE id = :id",
    ]
    for sql in forbidden:
        with SessionLocal() as db:
            with pytest.raises(
                DBAPIError, match="immutable|cannot be deleted|can only become|invalid credit|fk_|violates"
            ):
                db.execute(text(sql), {"id": v["id"]})
                db.commit()
    with SessionLocal() as db:  # lifecycle fields still work: close the validity window once
        db.execute(
            text("UPDATE credit_product_versions SET effective_to = effective_from + 30 WHERE id = :id"),
            {"id": v["id"]},
        )
        db.commit()
        with pytest.raises(DBAPIError, match="already closed"):
            db.execute(
                text("UPDATE credit_product_versions SET effective_to = effective_from + 40 WHERE id = :id"),
                {"id": v["id"]},
            )
            db.commit()
        db.rollback()
    got = client.get(base, headers=adm).json()
    assert got["rules"] == rules() and got["rules_hash"] == v["rules_hash"]


def test_v05_new_version_does_not_alter_the_previous_one(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    p, v1 = flow(client, adm)
    before_sim = client.post(f"{P}/{p['id']}/versions/{v1['id']}/simulate", headers=adm, json=SIM).json()
    before_snap = client.get(f"{P}/{p['id']}/versions/{v1['id']}/snapshot", headers=adm).json()
    v2 = client.post(f"{P}/{p['id']}/versions", headers=adm, json={"based_on_version_id": v1["id"]}).json()
    assert (
        v2["version_number"] == 2
        and v2["status"] == "draft"
        and v2["rules"] == v1["rules"]
        and v2["currencies"] == v1["currencies"]
    )
    row = v2["row_version"]
    new_rules = copy.deepcopy(rules())
    new_rules["method"]["rate"]["value"] = "24"
    assert (
        client.put(
            f"{P}/{p['id']}/versions/{v2['id']}", headers=adm, json={"row_version": row, "rules": new_rules}
        ).status_code
        == 200
    )
    soon = publish(client, adm, p["id"], v2["id"], eff=today() + timedelta(days=10))
    assert soon["rules_hash"] != v1["rules_hash"] and soon["version_number"] == 2
    again = client.get(f"{P}/{p['id']}/versions/{v1['id']}", headers=adm).json()
    assert again["rules"] == v1["rules"] and again["rules_hash"] == v1["rules_hash"] and again["status"] == "published"
    assert again["effective_to"] == str(today() + timedelta(days=9))  # only its validity window was closed
    assert client.get(f"{P}/{p['id']}/versions/{v1['id']}/snapshot", headers=adm).json() == before_snap
    assert client.post(f"{P}/{p['id']}/versions/{v1['id']}/simulate", headers=adm, json=SIM).json() == before_sim
    s2 = client.post(f"{P}/{p['id']}/versions/{v2['id']}/simulate", headers=adm, json=SIM).json()
    assert s2["totals"]["interest"] != before_sim["totals"]["interest"]  # the new rule applies only to version 2


def test_v06_historical_versions_stay_queryable_and_effective_resolution(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    p, v1 = flow(client, adm)
    v2 = mk_version(client, adm, p["id"])
    publish(client, adm, p["id"], v2["id"], eff=today() + timedelta(days=5))
    eff = lambda d=None: client.get(f"{P}/{p['id']}/effective", headers=adm, params={"on": str(d)} if d else {})  # noqa: E731
    assert eff().json()["version_number"] == 1 and eff(today() + timedelta(days=4)).json()["version_number"] == 1
    assert eff(today() + timedelta(days=5)).json()["version_number"] == 2
    assert eff(today() - timedelta(days=1)).status_code == 404  # before any version existed
    listed = client.get(P, headers=adm).json()
    assert listed[0]["current_version"]["version_number"] == 2 and listed[0]["status"] == "active"
    assert [v["version_number"] for v in client.get(f"{P}/{p['id']}", headers=adm).json()["versions"]] == [1, 2]
    stale = publish(
        client, adm, p["id"], mk_version(client, adm, p["id"])["id"], eff=today() + timedelta(days=5), expect=422
    )
    assert "despues" in stale["error"]["message"]  # validity windows cannot overlap or go backwards


def test_retire_deactivate_and_reactivate_are_explicit_and_audited(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    p, v = flow(client, adm)
    assert client.post(f"{P}/{p['id']}/activate", headers=adm, json={"reason": "ya esta activo"}).status_code == 409
    d = client.post(f"{P}/{p['id']}/deactivate", headers=adm, json={"reason": "fuera de oferta"})
    assert d.status_code == 200 and d.json()["status"] == "inactive"
    assert client.post(f"{P}/{p['id']}/deactivate", headers=adm, json={"reason": "otra vez"}).status_code == 409
    nv = mk_version(client, adm, p["id"])
    blocked = client.post(f"{P}/{p['id']}/versions/{nv['id']}/validate", headers=adm)
    assert blocked.status_code == 200
    assert (
        client.post(
            f"{P}/{p['id']}/versions/{nv['id']}/publish",
            headers=adm,
            json={
                "row_version": client.get(f"{P}/{p['id']}/versions/{nv['id']}", headers=adm).json()["row_version"],
                "effective_from": str(today() + timedelta(days=3)),
            },
        ).status_code
        == 409
    )  # inactive product
    assert (
        client.post(f"{P}/{p['id']}/activate", headers=adm, json={"reason": "se retoma"}).json()["status"] == "active"
    )
    r = client.post(f"{P}/{p['id']}/versions/{v['id']}/retire", headers=adm, json={"reason": "obsoleta"})
    assert r.status_code == 200 and r.json()["status"] == "retired" and r.json()["effective_to"] is not None
    assert (
        client.post(f"{P}/{p['id']}/versions/{v['id']}/retire", headers=adm, json={"reason": "otra vez"}).status_code
        == 409
    )
    got = client.get(f"{P}/{p['id']}/versions/{v['id']}", headers=adm).json()
    assert got["rules_hash"] == v["rules_hash"] and got["rules"] == v["rules"]  # retired keeps its rules
    names = [e.event_type for e in audit()]
    assert {"credit_product.deactivated", "credit_product.activated", "credit_product_version.retired"} <= set(names)
    ev = [e for e in audit("credit_product_version.retired")][0]
    assert ev.details["reason"] == "obsoleta" and ev.details["before"]["status"] == "published"


# ================================ Validation at the API (C0x) ========================================
def test_invalid_configuration_cannot_publish_and_is_audited(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    p = mk_product(client, adm)
    raw = rules()
    del raw["calendar"]
    raw["rounding"] = {"mode": "half_up"}
    v = mk_version(client, adm, p["id"], raw=raw)
    r = client.post(
        f"{P}/{p['id']}/versions/{v['id']}/publish",
        headers=adm,
        json={"row_version": 1, "effective_from": str(today())},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "product_validation_failed"
    paths = {i["path"] for i in r.json()["error"]["details"]}
    assert {"rules.calendar", "rules.rounding.scale"} <= paths
    assert client.get(f"{P}/{p['id']}/versions/{v['id']}", headers=adm).json()["status"] == "draft"
    assert [e.details["issue_count"] for e in audit("credit_product_version.publish_rejected")][0] >= 2


def test_currency_must_be_enabled_for_the_tenant_at_save_and_at_publish(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    p = mk_product(client, adm)
    usd = [{"code": "USD", "min_amount": "100", "max_amount": "5000"}]
    bad = client.post(f"{P}/{p['id']}/versions", headers=adm, json={"rules": rules(), "currencies": usd})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "business_rule_violation"  # USD not enabled yet
    assert client.post(f"{V2}/tenant/currencies", headers=adm, json={"code": "USD"}).status_code == 201
    v = mk_version(client, adm, p["id"], cur=usd)
    assert client.post(f"{P}/{p['id']}/versions/{v['id']}/validate", headers=adm).json()["valid"] is True
    assert client.delete(f"{V2}/tenant/currencies/USD", headers=adm).status_code == 204  # disabled after validation
    r = client.post(
        f"{P}/{p['id']}/versions/{v['id']}/publish",
        headers=adm,
        json={"row_version": 1, "effective_from": str(today())},
    )
    assert r.status_code == 422 and any(i["code"] == "currency_not_enabled" for i in r.json()["error"]["details"])


# ================================ Tenant isolation (T01-T03) =========================================
def test_t01_cross_tenant_access_is_a_404_everywhere(client, tenant_a, tenant_b):
    adm_a, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    p, v = flow(client, adm_a)
    base = f"{P}/{p['id']}"
    for method, url, body in (
        ("get", base, None),
        ("get", f"{base}/effective", None),
        ("get", f"{base}/versions/{v['id']}", None),
        ("get", f"{base}/versions/{v['id']}/snapshot", None),
        ("post", f"{base}/versions/{v['id']}/simulate", SIM),
        ("post", f"{base}/versions", {"based_on_version_id": v["id"]}),
        ("post", f"{base}/versions/{v['id']}/retire", {"reason": "intruso"}),
        ("post", f"{base}/versions/{v['id']}/validate", None),
        ("post", f"{base}/versions/{v['id']}/publish", {"row_version": 1, "effective_from": str(today())}),
        ("post", f"{base}/deactivate", {"reason": "intruso"}),
        ("put", f"{base}/versions/{v['id']}", {"row_version": 1, "rules": rules()}),
    ):
        r = getattr(client, method)(url, headers=adm_b, **({"json": body} if body is not None else {}))
        assert r.status_code == 404, (method, url, r.text)
    assert client.get(P, headers=adm_b).json() == []
    assert client.get(f"{base}", headers=adm_a).json()["status"] == "active"  # untouched
    # a version id of another product of the SAME tenant is not reachable through this product either
    other = mk_product(client, adm_a, "OTHER")
    assert client.get(f"{P}/{other['id']}/versions/{v['id']}", headers=adm_a).status_code == 404


def test_t02_cross_tenant_currency_reference_is_denied_in_the_database(client, tenant_a, tenant_b):
    adm_a, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    client.post(f"{V2}/tenant/currencies", headers=adm_a, json={"code": "EUR"})
    p, v = flow(client, adm_b)  # tenant B never enabled EUR
    with SessionLocal() as db:
        with pytest.raises(DBAPIError, match="fk_credit_product_currencies_tenant_currency|immutable"):
            db.execute(
                CreditProductCurrency.__table__.insert().values(
                    version_id=v["id"], currency_code="EUR", tenant_id=tenant_b["tenant_id"], min_amount=1, max_amount=2
                )
            )
            db.commit()
        db.rollback()
        pb = mk_version(client, adm_b, p["id"], cur=[])  # a draft, so only the FK can refuse the row
        with pytest.raises(DBAPIError, match="fk_credit_product_currencies_tenant_currency"):
            db.execute(
                CreditProductCurrency.__table__.insert().values(
                    version_id=pb["id"],
                    currency_code="EUR",
                    tenant_id=tenant_b["tenant_id"],
                    min_amount=1,
                    max_amount=2,
                )
            )
            db.commit()
        db.rollback()
        # and a version row pointing at another tenant's product violates the composite FK
        with pytest.raises(DBAPIError, match="fk_credit_product_versions_tenant_product"):
            db.execute(
                text(
                    "INSERT INTO credit_product_versions (tenant_id, product_id, version_number, status, rules, row_version, created_at, updated_at) "
                    "VALUES (:t, :p, 99, 'draft', '{}'::jsonb, 1, now(), now())"
                ),
                {"t": tenant_a["tenant_id"], "p": p["id"]},
            )
            db.commit()
        db.rollback()


def test_t03_same_code_coexists_across_tenants_and_hash_is_tenant_independent(client, tenant_a, tenant_b):
    adm_a, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    pa, va = flow(client, adm_a, "PERSONAL")
    pb, vb = flow(client, adm_b, "PERSONAL")
    assert pa["id"] != pb["id"] and va["rules_hash"] == vb["rules_hash"]  # same logical rules, same hash
    assert [x["code"] for x in client.get(P, headers=adm_a).json()] == ["PERSONAL"]
    assert mk_product(client, adm_a, "PERSONAL", expect=409)


# ================================ Security (S01-S03) ================================================
def test_s01_s02_publish_needs_a_stronger_permission_than_editing_a_draft(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    editor = create_role(
        client, adm, "Editor", ["credit.products.read", "credit.products.create", "credit.products.update_draft"]
    )
    reader = create_role(client, adm, "Lector", ["credit.products.read"])
    approver = create_role(client, adm, "Aprobador", ["credit.products.read", "credit.products.publish"])
    for email, role in (("ed@x.com", editor), ("rd@x.com", reader), ("ap@x.com", approver)):
        activate_user(client, sink, adm, email, roles=[role])
    ed, rd, ap = (h(login(client, e, slug=tenant_a["slug"])) for e in ("ed@x.com", "rd@x.com", "ap@x.com"))
    p = mk_product(client, ed, "EDIT-1")  # editor creates + edits drafts
    v = mk_version(client, ed, p["id"])
    row = v["row_version"]
    assert (
        client.put(
            f"{P}/{p['id']}/versions/{v['id']}", headers=ed, json={"row_version": row, "rules": rules()}
        ).status_code
        == 200
    )
    assert client.post(f"{P}/{p['id']}/versions/{v['id']}/validate", headers=ed).status_code == 200
    pub = {"row_version": row + 1, "effective_from": str(today())}
    assert client.post(f"{P}/{p['id']}/versions/{v['id']}/publish", headers=ed, json=pub).status_code == 403
    assert client.post(f"{P}/{p['id']}/deactivate", headers=ed, json={"reason": "prueba"}).status_code == 403
    # the approver can publish but cannot edit or create
    assert (
        client.put(
            f"{P}/{p['id']}/versions/{v['id']}", headers=ap, json={"row_version": row + 1, "rules": rules()}
        ).status_code
        == 403
    )
    assert client.post(P, headers=ap, json={"code": "NOPE", "name": "x"}).status_code == 403
    assert client.post(f"{P}/{p['id']}/versions/{v['id']}/publish", headers=ap, json=pub).status_code == 200
    # reader: GET + simulate only
    assert client.get(P, headers=rd).status_code == 200
    assert client.post(f"{P}/{p['id']}/versions/{v['id']}/simulate", headers=rd, json=SIM).status_code == 200
    assert client.post(P, headers=rd, json={"code": "NOPE", "name": "x"}).status_code == 403
    assert client.post(f"{P}/{p['id']}/versions", headers=rd, json={"based_on_version_id": v["id"]}).status_code == 403
    assert (
        client.post(f"{P}/{p['id']}/versions/{v['id']}/retire", headers=rd, json={"reason": "no puedo"}).status_code
        == 403
    )
    assert client.get(P).status_code == 401  # anonymous
    nobody = create_role(client, adm, "Nada", ["users.read"])
    activate_user(client, sink, adm, "no@x.com", roles=[nobody])
    no = h(login(client, "no@x.com", slug=tenant_a["slug"]))
    for r in (
        client.get(P, headers=no),
        client.get(f"{P}/{p['id']}", headers=no),
        client.get(f"{P}/{p['id']}/effective", headers=no),
    ):
        assert r.status_code == 403
    actor = [e.actor_id for e in audit("credit_product_version.published")]
    assert actor and actor[0] != tenant_a["admin_id"]  # audited under the real publisher, not the admin


# ================================ Simulation & GET purity ============================================
def writes_during(fn):
    statements = []

    def before(conn, cursor, statement, parameters, context, executemany):
        if re.match(r"\s*(INSERT|UPDATE|DELETE)", statement, re.I):
            statements.append(statement[:90])

    event.listen(engine, "before_cursor_execute", before)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return statements


def test_simulation_and_every_get_perform_no_database_write(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    p, v = flow(client, adm)
    draft = mk_version(client, adm, p["id"])
    base = f"{P}/{p['id']}"

    def reads():
        for url in (
            P,
            f"{P}?status=active&q=prd",
            base,
            f"{base}/effective",
            f"{base}/versions/{v['id']}",
            f"{base}/versions/{v['id']}/snapshot",
            f"{base}/versions/{draft['id']}",
        ):
            assert client.get(url, headers=adm).status_code == 200, url
        for vid in (v["id"], draft["id"]):  # published AND draft versions
            r = client.post(f"{base}/versions/{vid}/simulate", headers=adm, json=SIM)
            assert r.status_code == 200, r.text
        rejected = client.post(f"{base}/versions/{v['id']}/simulate", headers=adm, json=SIM | {"principal": "1"})
        assert rejected.status_code == 422 and rejected.json()["error"]["code"] == "simulation_rejected"

    with SessionLocal() as db:
        counts = {
            t: db.execute(text(f"SELECT count(*) FROM {t}")).scalar()
            for t in (
                "credit_products",
                "credit_product_versions",
                "security_events",
                "loans",
                "payments",
                "cash_movements",
            )
        }
    assert writes_during(reads) == []
    with SessionLocal() as db:  # nothing financial was created either
        assert counts == {t: db.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in counts}


def test_simulation_details_timezone_boundary_and_rejections(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    p, v = flow(client, adm)
    url = f"{P}/{p['id']}/versions/{v['id']}/simulate"
    r = client.post(url, headers=adm, json=SIM).json()
    assert r["rules_hash"] == v["rules_hash"] and r["product_version_id"] == v["id"] and r["timezone"] == TZ
    row = r["schedule"][0]
    assert set(row) >= {
        "period",
        "contractual_date",
        "due_date",
        "principal",
        "interest",
        "fees",
        "total",
        "opening_balance",
        "closing_balance",
        "delinquency_starts_on",
    }
    assert r["result_digest"] == client.post(url, headers=adm, json=SIM).json()["result_digest"]  # reproducible
    # 2026-03-01T02:00Z is still 28 Feb in Santo Domingo (UTC-4): the business date decides the due dates
    utc = client.post(
        url,
        headers=adm,
        json={k: v for k, v in SIM.items() if k != "start_date"} | {"start_at": "2026-03-01T02:00:00Z"},
    ).json()
    assert utc["start_date"] == "2026-02-28" and utc["schedule"][0]["contractual_date"] == "2026-03-28"
    later = client.post(
        url,
        headers=adm,
        json={k: v for k, v in SIM.items() if k != "start_date"} | {"start_at": "2026-03-01T12:00:00Z"},
    ).json()
    assert later["start_date"] == "2026-03-01"
    assert client.post(url, headers=adm, json=SIM | {"start_at": "2026-03-01T12:00:00Z"}).status_code == 422  # both
    assert (
        client.post(
            url,
            headers=adm,
            json={k: v for k, v in SIM.items() if k != "start_date"} | {"start_at": "2026-03-01T12:00:00"},
        ).status_code
        == 422
    )  # naive
    assert (
        client.post(url, headers=adm, json=SIM | {"currency": "USD"}).status_code == 422
    )  # not allowed by the version
    assert client.post(url, headers=adm, json=SIM | {"principal": 10000.5}).status_code == 422  # float refused
    assert client.post(url, headers=adm, json=SIM | {"term_periods": 61}).status_code == 422
    inc = mk_version(client, adm, p["id"], raw={"schema_version": 1})
    assert (
        client.post(f"{P}/{p['id']}/versions/{inc['id']}/simulate", headers=adm, json=SIM).status_code == 422
    )  # incomplete


def test_corrupted_snapshot_is_detected_not_silently_used(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    p, v = flow(client, adm)
    with engine.begin() as c:  # simulate storage corruption: only possible with the guard trigger switched off
        c.execute(text("ALTER TABLE credit_product_versions DISABLE TRIGGER trg_credit_product_versions_guard"))
        c.execute(
            text(
                "UPDATE credit_product_versions SET rules = jsonb_set(rules, '{method,rate,value}', '\"99\"') WHERE id = :i"
            ),
            {"i": v["id"]},
        )
        c.execute(text("ALTER TABLE credit_product_versions ENABLE TRIGGER trg_credit_product_versions_guard"))
    snap = client.get(f"{P}/{p['id']}/versions/{v['id']}/snapshot", headers=adm).json()
    assert snap["hash_verified"] is False
    r = client.post(f"{P}/{p['id']}/versions/{v['id']}/simulate", headers=adm, json=SIM)
    assert r.status_code == 409 and r.json()["error"]["code"] == "rules_integrity_failed"


# ================================ Concurrency =======================================================
def test_concurrent_version_creation_gets_unique_consecutive_numbers(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    p = mk_product(client, adm)

    def create(_):
        return client.post(
            f"{P}/{p['id']}/versions", headers=adm, json={"rules": rules(), "currencies": CUR}
        ).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert list(pool.map(create, range(6))) == [201] * 6
    numbers = [v["version_number"] for v in client.get(f"{P}/{p['id']}", headers=adm).json()["versions"]]
    assert numbers == [1, 2, 3, 4, 5, 6]


def test_concurrent_publish_of_two_drafts_with_the_same_date_has_one_winner(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    p = mk_product(client, adm)
    vs = [mk_version(client, adm, p["id"]) for _ in range(2)]
    for v in vs:
        assert client.post(f"{P}/{p['id']}/versions/{v['id']}/validate", headers=adm).status_code == 200

    def go(v):
        row = client.get(f"{P}/{p['id']}/versions/{v['id']}", headers=adm).json()["row_version"]
        return client.post(
            f"{P}/{p['id']}/versions/{v['id']}/publish",
            headers=adm,
            json={"row_version": row, "effective_from": str(today())},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(go, vs))
    assert results == [200, 422]
    with SessionLocal() as db:
        open_versions = (
            db.query(CreditProductVersion)
            .filter_by(product_id=p["id"], status="published")
            .filter(CreditProductVersion.effective_to.is_(None))
            .count()
        )
    assert open_versions == 1  # never two versions offered at once


# ================================ Migration ============================================================
def test_migration_0007_upgrade_downgrade_reupgrade(scratch_db):
    from sqlalchemy import create_engine

    assert _alembic(scratch_db, "upgrade", "0006").returncode == 0
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
                    "INSERT INTO tenant_currencies (tenant_id, currency_code, enabled_at) SELECT id, 'DOP', now() FROM companies"
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
            assert c.execute(text("SELECT count(*) FROM permissions WHERE code LIKE 'credit.products.%'")).scalar() == 5
            assert (
                c.execute(
                    text(
                        "SELECT count(*) FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id "
                        "WHERE p.code LIKE 'credit.products.%'"
                    )
                ).scalar()
                == 5
            )  # existing system admin role received them
            assert {
                r[0] for r in c.execute(text("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'trg_credit_product%'"))
            } == {"trg_credit_product_versions_guard", "trg_credit_product_currencies_guard"}
            idx = {
                r[0]
                for r in c.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'credit_product_versions'"))
            }
            assert "uq_credit_product_versions_open" in idx
        with eng.begin() as c:  # the trigger exists in a migrated database too
            c.execute(
                text(
                    "INSERT INTO credit_products (tenant_id, code, name, status, row_version, created_at, updated_at) "
                    "SELECT id, 'MIG-1', 'm', 'draft', 1, now(), now() FROM companies"
                )
            )
            c.execute(
                text(
                    "INSERT INTO credit_product_versions (tenant_id, product_id, version_number, status, rules, row_version, "
                    "effective_from, rules_hash, snapshot, published_at, created_at, updated_at) "
                    "SELECT tenant_id, id, 1, 'published', '{}'::jsonb, 1, current_date, 'sha256:a', '{}'::jsonb, now(), now(), now() FROM credit_products"
                )
            )
        with eng.connect() as c:
            with pytest.raises(DBAPIError, match="immutable"):
                c.execute(text("UPDATE credit_product_versions SET rules = '[]'::jsonb"))
        down = _alembic(scratch_db, "downgrade", "0006")
        assert down.returncode == 0, down.stderr
        with eng.connect() as c:
            assert c.execute(text("SELECT count(*) FROM permissions WHERE code LIKE 'credit.products.%'")).scalar() == 0
            assert c.execute(text("SELECT to_regclass('credit_products')")).scalar() is None
            assert c.execute(text("SELECT count(*) FROM pg_proc WHERE proname LIKE 'credit_product%'")).scalar() == 0
        again = _alembic(scratch_db, "upgrade", "head")
        assert again.returncode == 0, again.stderr
        assert _alembic(scratch_db, "check").returncode == 0
    finally:
        eng.dispose()
