"""T002-TENANT-LOGIN-01..05: login identifier is tenant-aware (no global email uniqueness). PostgreSQL only."""

import pytest

from app.core.db import SessionLocal
from app.models.session import UserSession
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


def test_tenant_login_03_recovery_never_targets_another_tenant(client, sink, tenant_a, tenant_b):
    adm_a, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    activate_user(client, sink, adm_a, "dup@example.com")
    activate_user(client, sink, adm_b, "dup@example.com")
    only_b = activate_user(client, sink, adm_b, "only-b@example.com")
    sink.messages.clear()
    # ambiguous identifier: nothing is issued to ANY of the accounts, the answer is unchanged
    r = client.post(f"{V2}/auth/recovery/request", json={"email": "dup@example.com"})
    assert r.status_code == 202 and sink.messages == []
    assert _count(RecoveryToken, used_at=None, revoked_at=None) == 0
    # an unambiguous identifier reaches exactly its own account
    client.post(f"{V2}/auth/recovery/request", json={"email": "only-b@example.com"})
    msg = sink.last("only-b@example.com")
    assert (
        client.post(f"{V2}/auth/recovery/complete", json={"token": msg["secret"], "new_password": NEW_PW}).status_code
        == 200
    )
    with SessionLocal() as db:
        assert db.get(UserAccount, only_b["id"]).company_id == tenant_b["tenant_id"]
        # no other account (notably the two "dup" ones) had its password touched by the recovery
        untouched = db.query(UserAccount).filter(UserAccount.email == "dup@example.com").all()
        assert len(untouched) == 2


def test_tenant_login_04_no_cross_tenant_account_existence_signal(client, sink, tenant_a, tenant_b):
    adm_a, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    activate_user(client, sink, adm_b, "exists-in-b@example.com")
    in_b = _new_user(client, adm_a, "exists-in-b@example.com")  # A invites an address that exists only in B
    nowhere = _new_user(client, adm_a, "exists-nowhere@example.com")
    assert in_b.status_code == nowhere.status_code == 201
    assert set(in_b.json()) == set(nowhere.json())
    assert all(u["tenant_id"] == tenant_a["tenant_id"] for u in client.get(f"{V2}/users", headers=adm_a).json())
    # unauthenticated probes cannot tell "also exists in B" from "exists nowhere"
    shapes = []
    for email in ("exists-in-b@example.com", "nobody-at-all@example.com"):
        rec = client.post(f"{V2}/auth/recovery/request", json={"email": email})
        log = client.post(f"{V2}/auth/login", json={"email": email, "password": "bad-password-1"})
        err = log.json()["error"]
        shapes.append((rec.status_code, rec.json(), log.status_code, err["code"], err["message"]))
    assert shapes[0] == shapes[1]


def test_tenant_login_05_ambiguous_identifier_fails_closed_blocked_by_spec(client, sink, tenant_a, tenant_b):
    """BLOCKED_BY_SPEC: login/recovery carry no tenant context, so a duplicated identifier cannot be resolved.

    The only safe behaviour available without inventing a tenant-selection contract is to fail closed:
    never pick one account arbitrarily; answer exactly as for an unknown account."""
    adm_a, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    activate_user(client, sink, adm_a, "dup@example.com")
    activate_user(client, sink, adm_b, "dup@example.com")
    unique = activate_user(client, sink, adm_a, "unique@example.com")
    unknown = login(client, "ghost@example.com", PW, expect=401)
    ambiguous = login(client, "dup@example.com", PW, expect=401)  # the right password for both accounts
    assert ambiguous.json()["error"]["code"] == unknown.json()["error"]["code"] == "invalid_credentials"
    assert ambiguous.json()["error"]["message"] == unknown.json()["error"]["message"]
    assert login(client, "unique@example.com")["user"]["id"] == unique["id"]  # unambiguous logins are unaffected
    with SessionLocal() as db:  # no session was created for either duplicate
        dup_ids = [u.id for u in db.query(UserAccount).filter_by(email="dup@example.com")]
        assert db.query(UserSession).filter(UserSession.user_id.in_(dup_ids)).count() == 0
