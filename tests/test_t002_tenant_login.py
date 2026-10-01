"""T002-TENANT-LOGIN-01..05: login identifier is tenant-aware (no global email uniqueness). PostgreSQL only."""

import pytest

from app.core.db import SessionLocal
from app.models.company import Company
from app.modules.identity.models import RecoveryToken, UserAccount
from tests import pg_env  # noqa: F401  (must precede app imports)
from tests.test_t002_identity import (  # noqa: F401  (fixtures + helpers shared with the T-002 suite)
    NEW_PW,
    PW,
    V2,
    activate_user,
    admin_headers,
    client,
    fresh_db,
    h,
    login,
    sink,
    tenant_a,
    tenant_b,
)


def _new_user(client, headers, email, expect=201):
    r = client.post(
        f"{V2}/users", headers=headers, json={"email": email, "given_names": "Ana", "family_names": "Perez"}
    )
    assert r.status_code == expect, r.text
    return r


def _count(model, **filters) -> int:
    with SessionLocal() as db:
        return db.query(model).filter_by(**filters).count()


def test_tenant_login_01_same_normalised_email_allowed_in_two_tenants(client, sink, tenant_a, tenant_b):
    adm_a, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    a = _new_user(client, adm_a, "Shared@Example.com").json()
    b = _new_user(client, adm_b, "shared@example.com").json()
    assert a["email"] == b["email"] == "shared@example.com" and a["tenant_id"] != b["tenant_id"]
    # uniqueness still holds inside one tenant
    assert _new_user(client, adm_a, "SHARED@example.com", expect=409).json()["error"]["code"] == "conflict"
    assert _count(UserAccount, email="shared@example.com") == 2
    with SessionLocal() as db:
        # DB level: normalised identifier enforced; (tenant, email) unique; platform accounts are a separate namespace
        for bad in (
            UserAccount(
                full_name="x", email="Not.Normalised@example.com", password_hash="h", company_id=tenant_a["tenant_id"]
            ),
            UserAccount(full_name="x", email="shared@example.com", password_hash="h", company_id=tenant_a["tenant_id"]),
        ):
            db.add(bad)
            with pytest.raises(Exception, match="email_normalized|uq_users_tenant_email"):
                db.commit()
            db.rollback()
        db.add(UserAccount(full_name="p1", email="platform@example.com", password_hash="h", company_id=None))
        db.add(
            UserAccount(
                full_name="t", email="platform@example.com", password_hash="h", company_id=tenant_a["tenant_id"]
            )
        )
        db.commit()  # a platform account and a tenant account may share an email at the database level
        db.add(UserAccount(full_name="p2", email="platform@example.com", password_hash="h", company_id=None))
        with pytest.raises(Exception, match="uq_users_platform_email"):
            db.commit()


def test_tenant_login_02_create_and_update_in_one_tenant_do_not_conflict_with_another(client):
    for i in (1, 2):
        r = client.post(
            "/api/v1/auth/register",
            json={
                "full_name": f"Owner {i}",
                "email": f"owner{i}@example.com",
                "password": "legacy-pass-123",
                "company_name": f"Legacy {i}",
            },
        )
        assert r.status_code == 201
    hdr = {}
    for i in (1, 2):
        t = client.post(
            "/api/v1/auth/login", json={"email": f"owner{i}@example.com", "password": "legacy-pass-123"}
        ).json()
        hdr[i] = {"Authorization": f"Bearer {t['access_token']}"}
    body = {
        "full_name": "Worker Uno",
        "email": "worker@example.com",
        "password": "worker-pass-123",
        "role": "collector",
    }
    assert client.post("/api/v1/users", headers=hdr[1], json=body).status_code == 201
    second = client.post("/api/v1/users", headers=hdr[2], json=body)  # same email, other tenant: no 409
    assert second.status_code == 201, second.text
    assert client.post("/api/v1/users", headers=hdr[1], json=body).status_code == 409  # own tenant: conflict
    other = client.post("/api/v1/users", headers=hdr[2], json={**body, "email": "other@example.com"}).json()
    edit = {"full_name": "Worker Dos", "role": "collector", "is_active": True}
    own_clash = client.put(f"/api/v1/users/{other['id']}", headers=hdr[2], json={**edit, "email": "worker@example.com"})
    assert own_clash.status_code == 409  # collides with its OWN tenant's worker@
    cross = client.put(f"/api/v1/users/{other['id']}", headers=hdr[2], json={**edit, "email": "owner1@example.com"})
    assert cross.status_code == 200, cross.text  # owner1@ lives in tenant 1: no conflict, nothing revealed


def _slug_login(client, slug, email, password=PW, expect=200):
    r = client.post(f"{V2}/auth/login", json={"tenant_slug": slug, "email": email, "password": password})
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else r


def test_tenant_login_03_recovery_resolves_only_inside_the_given_slug(client, sink, tenant_a, tenant_b):
    adm_a, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    a = activate_user(client, sink, adm_a, "dup@example.com")
    b = activate_user(client, sink, adm_b, "dup@example.com")
    sink.messages.clear()
    r = client.post(f"{V2}/auth/recovery/request", json={"tenant_slug": "beta", "email": "dup@example.com"})
    assert r.status_code == 202 and len(sink.messages) == 1  # exactly one account, the one in beta
    assert (
        client.post(
            f"{V2}/auth/recovery/complete", json={"token": sink.last()["secret"], "new_password": NEW_PW}
        ).status_code
        == 200
    )
    with SessionLocal() as db:
        assert db.get(UserAccount, b["id"]).password_changed_at is not None
        assert (
            db.get(UserAccount, a["id"]).password_changed_at is not None
        )  # set when A activated: not by this recovery
        a_token_rows = db.query(RecoveryToken).filter_by(user_id=a["id"], purpose="recovery").count()
        assert a_token_rows == 0  # alfa's account never got a recovery token
    # the old password stopped working only in beta
    _slug_login(client, "beta", "dup@example.com", PW, expect=401)
    _slug_login(client, "beta", "dup@example.com", NEW_PW)
    _slug_login(client, "alfa", "dup@example.com", PW)


def test_tenant_login_04_unknown_slug_and_cross_tenant_probes_reveal_nothing(client, sink, tenant_a, tenant_b):
    adm_a, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    activate_user(client, sink, adm_b, "only-in-beta@example.com")
    in_b = _new_user(client, adm_a, "only-in-beta@example.com")  # alfa invites an address that exists in beta
    nowhere = _new_user(client, adm_a, "exists-nowhere@example.com")
    assert in_b.status_code == nowhere.status_code == 201 and set(in_b.json()) == set(nowhere.json())
    assert all(u["tenant_id"] == tenant_a["tenant_id"] for u in client.get(f"{V2}/users", headers=adm_a).json())
    sink.messages.clear()
    shapes = []
    probes = (
        ("beta", "only-in-beta@example.com"),  # real tenant, real user
        ("alfa", "only-in-beta@example.com"),  # real tenant, the user lives elsewhere
        ("no-such-agency", "only-in-beta@example.com"),  # unknown slug
        ("beta", "nobody-at-all@example.com"),  # real tenant, unknown email
    )
    for slug, email in probes:
        rec = client.post(f"{V2}/auth/recovery/request", json={"tenant_slug": slug, "email": email})
        log = _slug_login(client, slug, email, "bad-password-1", expect=401)
        err = log.json()["error"]
        shapes.append((rec.status_code, rec.json(), log.status_code, err["code"], err["message"]))
    assert len(set(map(repr, shapes))) == 1  # byte-for-byte the same answer in every case
    # mail went only to the two real accounts that hold that address (beta's recovery, alfa's pending invite)
    assert [m["email"] for m in sink.messages] == ["only-in-beta@example.com"] * 2


def test_tenant_login_05_same_email_authenticates_in_each_tenant_by_slug(client, sink, tenant_a, tenant_b):
    adm_a, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    a = activate_user(client, sink, adm_a, "dup@example.com")
    b = activate_user(client, sink, adm_b, "dup@example.com")
    ta = _slug_login(client, "alfa", "dup@example.com")
    tb = _slug_login(client, "beta", "dup@example.com")
    assert ta["user"]["id"] == a["id"] and ta["user"]["tenant_id"] == tenant_a["tenant_id"]
    assert tb["user"]["id"] == b["id"] and tb["user"]["tenant_id"] == tenant_b["tenant_id"]
    assert client.get(f"{V2}/auth/me", headers=h(ta)).json()["user"]["id"] == a["id"]
    # slug is case-insensitive; a wrong slug is just "invalid credentials"; no slug means the platform namespace
    assert _slug_login(client, "ALFA", "dup@example.com")["user"]["id"] == a["id"]
    _slug_login(client, "gamma", "dup@example.com", expect=401)
    assert client.post(f"{V2}/auth/login", json={"email": "dup@example.com", "password": PW}).status_code == 401
    # throttling is per tenant namespace: failures in alfa do not block the same email in beta
    for _ in range(5):
        _slug_login(client, "alfa", "dup@example.com", "bad-password-1", expect=401)
    _slug_login(client, "alfa", "dup@example.com", expect=429)
    assert _slug_login(client, "beta", "dup@example.com")["user"]["id"] == b["id"]


def test_tenant_slug_is_unique_valid_and_generated_for_legacy_tenants(client):
    r1 = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Dueno Uno",
            "email": "one@example.com",
            "password": "legacy-pass-123",
            "company_name": "Agencia Ñandú",
        },
    )
    r2 = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Dueno Dos",
            "email": "two@example.com",
            "password": "legacy-pass-123",
            "company_name": "Agencia Ñandú",
        },
    )
    assert r1.status_code == r2.status_code == 201
    with SessionLocal() as db:
        slugs = sorted(c.slug for c in db.query(Company).all())
    assert slugs == ["agencia-nandu", "agencia-nandu-2"]
    custom = {
        "full_name": "Dueno Tres",
        "email": "three@example.com",
        "password": "legacy-pass-123",
        "company_name": "X",
    }
    assert client.post("/api/v1/auth/register", json={**custom, "tenant_slug": "mi-agencia"}).status_code == 201
    taken = {**custom, "email": "four@example.com", "tenant_slug": "MI-agencia"}
    assert client.post("/api/v1/auth/register", json=taken).status_code == 409
    for bad in ("Bad Slug", "-x", "admin", "a" * 64):
        r = client.post("/api/v1/auth/register", json={**custom, "email": "five@example.com", "tenant_slug": bad})
        assert r.status_code == 422, bad
    login_ok = _slug_login(client, "mi-agencia", "three@example.com", "legacy-pass-123")
    assert login_ok["user"]["email"] == "three@example.com"
    with SessionLocal() as db:
        with pytest.raises(Exception, match="slug_format"):
            db.add(Company(name="y", slug="Not A Slug"))
            db.commit()
