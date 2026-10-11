"""T-002 Identity & Authorization Foundation tests (T002-A*, Z*, S* plus supporting checks). PostgreSQL only."""

import json
import logging
from datetime import timedelta

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import text, update

from app.core.config import settings
from app.core.context import get_context
from app.core.db import Base, SessionLocal, engine
from app.core.security import get_password_hash
from app.core.time import now_utc
from app.main import app, create_app
from app.models.branch import Branch
from app.models.company import Company
from app.models.session import UserSession
from app.modules.identity import auth as auth_service
from app.modules.identity.audit import clean_details
from app.modules.identity.authorization import Grant, Principal
from app.modules.identity.catalog import CATALOG, bootstrap_owner, sync_permission_catalog
from app.modules.identity.deps import get_auth_context
from app.modules.identity.errors import AccountLocked, InvalidCredentials, RateLimited
from app.modules.identity.models import (
    Person,
    RecoveryToken,
    Role,
    SecurityEvent,
    UserAccount,
    UserRoleAssignment,
)
from app.modules.identity.notifications import get_notifier
from tests import pg_env  # noqa: F401  (must precede app imports)
from tests.test_t001_foundation import _alembic, scratch_db  # noqa: F401  (fixture re-use)

PW = "correct-horse-battery"
NEW_PW = "another-long-passphrase"
V2 = "/api/v2"


class Sink:
    """In-memory notifier: the only place a recovery/activation secret is ever visible in tests."""

    def __init__(self):
        self.messages: list[dict] = []

    def send(self, *, email, secret, purpose, expires_at) -> bool:
        self.messages.append({"email": email, "secret": secret, "purpose": purpose, "expires_at": expires_at})
        return True

    def last(self, email=None):
        rows = [m for m in self.messages if email is None or m["email"] == email]
        return rows[-1] if rows else None


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        sync_permission_catalog(db)
        db.commit()
    yield


@pytest.fixture()
def sink():
    s = Sink()
    app.dependency_overrides[get_notifier] = lambda: s
    yield s
    app.dependency_overrides.pop(get_notifier, None)


@pytest.fixture()
def client(sink):
    with TestClient(app) as c:
        yield c


def make_tenant(name: str, admin_email: str) -> dict:
    with SessionLocal() as db:
        company = Company(name=name, slug=name.lower())
        db.add(company)
        db.flush()
        user = UserAccount(
            full_name=f"Admin {name}",
            email=admin_email,
            password_hash=get_password_hash(PW),
            role=None,
            company_id=company.id,
            activated_at=now_utc(),
        )
        db.add(user)
        db.flush()
        bootstrap_owner(db, user)
        branch = Branch(
            name=f"Sucursal {name}", address="x", manager_name="m", notary_name="n", phone="1", company_id=company.id
        )
        db.add(branch)
        db.commit()
        return {
            "tenant_id": company.id,
            "admin_id": user.id,
            "email": admin_email,
            "branch_id": branch.id,
            "slug": name.lower(),
        }


def login(client, email, password=PW, expect=200, slug="alfa"):
    r = client.post(f"{V2}/auth/login", json={"tenant_slug": slug, "email": email, "password": password})
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else r


def h(tokens) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def admin_headers(client, tenant) -> dict:
    return h(login(client, tenant["email"], slug=tenant["slug"]))


def create_role(client, headers, name, perms, expect=201):
    r = client.post(f"{V2}/roles", headers=headers, json={"name": name, "permissions": perms})
    assert r.status_code == expect, r.text
    return r.json()


def activate_user(client, sink, admin_hdrs, email, *, roles=(), scope="tenant", branch_id=None) -> dict:
    """Create (pending) -> activate with the invitation secret -> optionally assign roles."""
    r = client.post(
        f"{V2}/users", headers=admin_hdrs, json={"email": email, "given_names": "Ana", "family_names": "Perez"}
    )
    assert r.status_code == 201, r.text
    user = r.json()
    msg = sink.last(email)
    assert msg and msg["purpose"] == "activation"
    done = client.post(f"{V2}/auth/recovery/complete", json={"token": msg["secret"], "new_password": PW})
    assert done.status_code == 200, done.text
    for role in roles:
        body = {"role_id": role["id"], "scope": scope}
        if branch_id:
            body["branch_id"] = branch_id
        a = client.post(f"{V2}/users/{user['id']}/roles", headers=admin_hdrs, json=body)
        assert a.status_code == 201, a.text
    return user


def events(event_type=None) -> list[SecurityEvent]:
    with SessionLocal() as db:
        q = db.query(SecurityEvent).order_by(SecurityEvent.id)
        if event_type:
            q = q.filter(SecurityEvent.event_type == event_type)
        rows = q.all()
        db.expunge_all()
        return rows


@pytest.fixture()
def tenant_a():
    return make_tenant("Alfa", "admin-a@example.com")


@pytest.fixture()
def tenant_b():
    return make_tenant("Beta", "admin-b@example.com")


# ================================ Authentication ====================================================
def test_a01_login_valid_and_me(client, tenant_a):
    tokens = login(client, tenant_a["email"])
    assert tokens["token_type"] == "bearer" and tokens["expires_in"] == settings.access_token_expire_minutes * 60
    assert tokens["user"]["status"] == "active" and tokens["user"]["tenant_id"] == tenant_a["tenant_id"]
    me = client.get(f"{V2}/auth/me", headers=h(tokens)).json()
    assert me["person"]["given_names"] and {g["permission"] for g in me["permissions"]} >= {
        "users.read",
        "roles.assign",
    }
    assert events("auth.login.succeeded")


def test_a02_login_invalid_credentials(client, tenant_a):
    wrong = login(client, tenant_a["email"], "wrong-password-xyz", expect=401)
    unknown = login(client, "nobody@example.com", "wrong-password-xyz", expect=401)
    assert wrong.json()["error"]["code"] == unknown.json()["error"]["code"] == "invalid_credentials"
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]
    assert len(events("auth.login.failed")) == 2


def test_a03_disabled_user_denied(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    staff = activate_user(client, sink, adm, "staff@example.com")
    staff_tokens = login(client, "staff@example.com")
    assert client.get(f"{V2}/auth/me", headers=h(staff_tokens)).status_code == 200
    assert client.post(f"{V2}/users/{staff['id']}/disable", headers=adm).status_code == 200
    # existing token dies immediately, new login is refused (only after the password proved right)
    assert client.get(f"{V2}/auth/me", headers=h(staff_tokens)).status_code == 401
    assert login(client, "staff@example.com", expect=403).json()["error"]["code"] == "account_disabled"
    assert login(client, "staff@example.com", "wrong-password-xyz", expect=401)


def test_a04_locked_user_denied_and_auto_lock_expires(client, tenant_a):
    t0 = now_utc()
    with SessionLocal() as db:
        for i in range(settings.auth_account_lock_failures):  # distinct IPs: only the account counter accumulates
            with pytest.raises(InvalidCredentials):
                auth_service.login(
                    db,
                    tenant_slug="alfa",
                    email=tenant_a["email"],
                    password="bad-password-1",
                    device_name=None,
                    client_ip=f"10.0.0.{i}",
                    now=t0,
                )
        user = db.query(UserAccount).filter_by(email=tenant_a["email"]).one()
        assert user.status == "locked" and user.locked_until > t0
        with pytest.raises(AccountLocked):
            auth_service.login(
                db,
                tenant_slug="alfa",
                email=tenant_a["email"],
                password=PW,
                device_name=None,
                client_ip="10.9.9.9",
                now=t0,
            )
        with pytest.raises(InvalidCredentials):  # wrong password reveals nothing about the lock
            auth_service.login(
                db,
                tenant_slug="alfa",
                email=tenant_a["email"],
                password="bad-password-1",
                device_name=None,
                client_ip="10.9.9.8",
                now=t0,
            )
        later = t0 + timedelta(seconds=settings.auth_account_lock_seconds + 1)
        ok = auth_service.login(
            db,
            tenant_slug="alfa",
            email=tenant_a["email"],
            password=PW,
            device_name=None,
            client_ip="10.9.9.7",
            now=later,
        )
        assert ok.user.status == "active"
    assert events("account.locked") and events("account.unlocked")
    # administratively locked state is denied through the API as well
    with SessionLocal() as db:
        db.execute(
            update(UserAccount)
            .where(UserAccount.email == tenant_a["email"])
            .values(status="locked", locked_until=now_utc() + timedelta(hours=1))
        )
        db.commit()
    assert login(client, tenant_a["email"], expect=403).json()["error"]["code"] == "account_locked"


def test_a05_login_rate_limiting_backoff(client, tenant_a):
    for _ in range(settings.auth_max_failures_account_ip):
        login(client, tenant_a["email"], "bad-password-1", expect=401)
    blocked = login(client, tenant_a["email"], PW, expect=429)  # even the right password while blocked
    assert blocked.json()["error"]["code"] == "rate_limited" and int(blocked.headers["Retry-After"]) >= 1
    # unknown accounts are throttled identically (no existence signal)
    for _ in range(settings.auth_max_failures_account_ip):
        login(client, "ghost@example.com", "bad-password-1", expect=401)
    assert login(client, "ghost@example.com", PW, expect=429).json()["error"]["code"] == "rate_limited"
    # blocked attempts are not counted, so the block cannot be extended indefinitely
    first = int(
        client.post(
            f"{V2}/auth/login", json={"tenant_slug": "alfa", "email": tenant_a["email"], "password": PW}
        ).headers["Retry-After"]
    )
    second = int(
        client.post(
            f"{V2}/auth/login", json={"tenant_slug": "alfa", "email": tenant_a["email"], "password": PW}
        ).headers["Retry-After"]
    )
    assert second <= first <= settings.auth_throttle_base_seconds
    # a different account from the same IP is not blocked by the per-account counter
    assert (
        client.post(
            f"{V2}/auth/login", json={"tenant_slug": "alfa", "email": "other@example.com", "password": "bad-password-1"}
        ).status_code
        == 401
    )


def test_a05_backoff_is_exponential_capped_and_temporary(tenant_a):
    now = now_utc()
    base = settings.auth_throttle_base_seconds
    kw = dict(tenant_slug="alfa", email=tenant_a["email"], device_name=None, client_ip="10.1.1.1")
    with SessionLocal() as db:
        for _ in range(settings.auth_max_failures_account_ip):
            with pytest.raises(InvalidCredentials):
                auth_service.login(db, password="bad-password-1", now=now, **kw)
        expected = [min(base * 2**i, settings.auth_throttle_max_seconds) for i in range(5)]
        assert expected[:3] == [base, base * 2, base * 4]  # exponential
        for i, wait in enumerate(expected):
            with pytest.raises(RateLimited) as exc:  # even the right password is refused while blocked
                auth_service.login(db, password=PW, now=now, **kw)
            assert exc.value.retry_after_seconds == wait
            now += timedelta(seconds=wait + 1)
            if i < len(expected) - 1:  # one more failure after the block elapsed doubles the delay
                with pytest.raises(InvalidCredentials):
                    auth_service.login(db, password="bad-password-1", now=now, **kw)
        # the block is temporary: once it elapsed the right password works and counters reset
        assert auth_service.login(db, password=PW, now=now, **kw).user.status == "active"


def test_a06_recovery_single_use_and_revokes_sessions(client, sink, tenant_a):
    old = login(client, tenant_a["email"])
    r = client.post(f"{V2}/auth/recovery/request", json={"tenant_slug": "alfa", "email": tenant_a["email"]})
    assert r.status_code == 202
    secret = sink.last(tenant_a["email"])["secret"]
    done = client.post(f"{V2}/auth/recovery/complete", json={"token": secret, "new_password": NEW_PW})
    assert done.status_code == 200
    again = client.post(
        f"{V2}/auth/recovery/complete", json={"token": secret, "new_password": "yet-another-passphrase"}
    )
    assert again.status_code == 400 and again.json()["error"]["code"] == "recovery_token_reused"
    assert client.get(f"{V2}/auth/me", headers=h(old)).status_code == 401  # sessions revoked
    login(client, tenant_a["email"], PW, expect=401)
    login(client, tenant_a["email"], NEW_PW)
    # a newer request revokes the previous unused token
    client.post(f"{V2}/auth/recovery/request", json={"tenant_slug": "alfa", "email": tenant_a["email"]})
    first = sink.last()["secret"]
    client.post(f"{V2}/auth/recovery/request", json={"tenant_slug": "alfa", "email": tenant_a["email"]})
    second = sink.last()["secret"]
    assert first != second
    assert (
        client.post(f"{V2}/auth/recovery/complete", json={"token": first, "new_password": PW + "x"}).status_code == 400
    )
    assert (
        client.post(f"{V2}/auth/recovery/complete", json={"token": second, "new_password": PW + "x"}).status_code == 200
    )
    with SessionLocal() as db:  # only the hash is stored
        rows = db.query(RecoveryToken).all()
        assert all(len(r.token_hash) == 64 and r.token_hash not in (first, second) for r in rows)


def test_a07_expired_and_invalid_recovery_tokens_denied(client, sink, tenant_a):
    client.post(f"{V2}/auth/recovery/request", json={"tenant_slug": "alfa", "email": tenant_a["email"]})
    secret = sink.last()["secret"]
    with SessionLocal() as db:
        db.execute(update(RecoveryToken).values(expires_at=now_utc() - timedelta(minutes=1)))
        db.commit()
    r = client.post(f"{V2}/auth/recovery/complete", json={"token": secret, "new_password": NEW_PW})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_recovery_token"
    junk = client.post(f"{V2}/auth/recovery/complete", json={"token": "x" * 43, "new_password": NEW_PW})
    assert junk.json()["error"]["code"] == "invalid_recovery_token"
    weak = client.post(f"{V2}/auth/recovery/complete", json={"token": secret, "new_password": "short"})
    assert weak.status_code == 422 and weak.json()["error"]["code"] == "password_policy_violation"


def test_a07b_recovery_completion_is_rate_limited(client):
    for _ in range(settings.recovery_max_failures_ip):
        assert (
            client.post(f"{V2}/auth/recovery/complete", json={"token": "y" * 43, "new_password": NEW_PW}).status_code
            == 400
        )
    r = client.post(f"{V2}/auth/recovery/complete", json={"token": "y" * 43, "new_password": NEW_PW})
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_a07c_recovery_request_rate_limits_are_silent_per_account(client, sink, tenant_a):
    statuses = [
        client.post(f"{V2}/auth/recovery/request", json={"tenant_slug": "alfa", "email": tenant_a["email"]}).status_code
        for _ in range(6)
    ]
    assert statuses == [202] * 6  # same answer every time
    assert len(sink.messages) == settings.recovery_max_requests_account  # but no further tokens are issued


def test_a08_logout_revokes_and_refresh_rotation(client, tenant_a):
    tokens = login(client, tenant_a["email"])
    rotated = client.post(f"{V2}/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert rotated.status_code == 200
    new = rotated.json()
    # replaying the superseded refresh token is theft evidence: the whole session dies
    replay = client.post(f"{V2}/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert replay.status_code == 401
    assert client.get(f"{V2}/auth/me", headers=h(new)).status_code == 401
    assert events("session.refresh_reuse_detected")
    fresh = login(client, tenant_a["email"])
    assert client.post(f"{V2}/auth/logout", headers=h(fresh)).status_code == 204
    assert client.get(f"{V2}/auth/me", headers=h(fresh)).status_code == 401
    assert client.post(f"{V2}/auth/refresh", json={"refresh_token": fresh["refresh_token"]}).status_code == 401
    assert events("auth.logout")


def test_a08b_expired_session_denied(client, tenant_a):
    tokens = login(client, tenant_a["email"])
    with SessionLocal() as db:
        db.execute(update(UserSession).values(expires_at=now_utc() - timedelta(seconds=1)))
        db.commit()
    r = client.get(f"{V2}/auth/me", headers=h(tokens))
    assert r.status_code == 401 and r.json()["error"]["code"] == "session_expired"


def test_password_change_revokes_other_sessions(client, tenant_a):
    one, two = login(client, tenant_a["email"]), login(client, tenant_a["email"])
    wrong = client.post(
        f"{V2}/auth/password/change",
        headers=h(one),
        json={"current_password": "nope-nope-nope", "new_password": NEW_PW},
    )
    assert wrong.status_code == 401
    ok = client.post(
        f"{V2}/auth/password/change", headers=h(one), json={"current_password": PW, "new_password": NEW_PW}
    )
    assert ok.status_code == 200 and ok.json()["sessions_revoked"] == 1
    assert client.get(f"{V2}/auth/me", headers=h(one)).status_code == 200
    assert client.get(f"{V2}/auth/me", headers=h(two)).status_code == 401
    assert events("password.changed")


# ================================ Authorization =====================================================
def test_z01_deny_by_default(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    assert client.get(f"{V2}/users").status_code == 401  # unauthenticated
    bare = activate_user(client, sink, adm, "bare@example.com")  # authenticated, no role
    bh = h(login(client, "bare@example.com"))
    for method, path in (
        ("get", "/users"),
        ("get", "/roles"),
        ("get", "/permissions"),
        ("get", "/security/events"),
        ("post", f"/users/{bare['id']}/disable"),
        ("post", f"/users/{bare['id']}/sessions/revoke"),
    ):
        r = getattr(client, method)(f"{V2}{path}", headers=bh)
        assert r.status_code == 403 and r.json()["error"]["code"] == "permission_denied", (path, r.text)
    assert client.get(f"{V2}/auth/me", headers=bh).json()["permissions"] == []
    # legacy API: a v2-created user holds no legacy role either
    assert client.get("/api/v1/users", headers=bh).status_code == 403


def test_z01b_unknown_permission_codes_never_allow():
    p = Principal(1, 1, None, 1, (Grant("made.up", "tenant"),))
    assert not p.allows("made.up") and not p.allows("users.read")


def test_z02_permission_grants_access_and_lists_only_own_tenant(client, sink, tenant_a, tenant_b):
    adm = admin_headers(client, tenant_a)
    reader_role = create_role(client, adm, "Lector", ["users.read"])
    activate_user(client, sink, adm, "reader@example.com", roles=[reader_role])
    rh = h(login(client, "reader@example.com"))
    r = client.get(f"{V2}/users", headers=rh)
    assert r.status_code == 200
    assert {u["email"] for u in r.json()} == {"admin-a@example.com", "reader@example.com"}  # nothing of tenant B
    assert (
        client.post(f"{V2}/users", headers=rh, json={"email": "z@example.com", "given_names": "Z"}).status_code == 403
    )


def test_z03_removed_permission_denies_future_access(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    role = create_role(client, adm, "Lector", ["users.read"])
    user = activate_user(client, sink, adm, "reader@example.com", roles=[role])
    rh = h(login(client, "reader@example.com"))
    assert client.get(f"{V2}/users", headers=rh).status_code == 200
    assert client.delete(f"{V2}/users/{user['id']}/roles/{role['id']}", headers=adm).status_code == 204
    assert client.get(f"{V2}/users", headers=rh).status_code == 403  # same token, permission gone
    with SessionLocal() as db:  # history preserved: the assignment row remains, revoked
        row = db.query(UserRoleAssignment).filter_by(user_id=user["id"], role_id=role["id"]).one()
        assert row.revoked_at is not None and row.revoked_by is not None
    assert events("role.removed")


def test_z04_cross_tenant_read_write_and_role_assignment_denied(client, sink, tenant_a, tenant_b):
    adm_a, adm_b = admin_headers(client, tenant_a), admin_headers(client, tenant_b)
    b_user = activate_user(client, sink, adm_b, "b-staff@example.com")
    a_user = activate_user(client, sink, adm_a, "a-staff@example.com")
    b_role = create_role(client, adm_b, "Rol B", ["users.read"])
    a_role = create_role(client, adm_a, "Rol A", ["users.read"])
    uid_b, uid_a = b_user["id"], a_user["id"]
    assert client.get(f"{V2}/users/{uid_b}", headers=adm_a).status_code == 404  # read
    assert client.post(f"{V2}/users/{uid_b}/disable", headers=adm_a).status_code == 404  # write
    assert client.post(f"{V2}/users/{uid_b}/sessions/revoke", headers=adm_a).status_code == 404
    assert client.post(f"{V2}/users/{uid_b}/roles", headers=adm_a, json={"role_id": a_role["id"]}).status_code == 404
    assert client.post(f"{V2}/users/{uid_a}/roles", headers=adm_a, json={"role_id": b_role["id"]}).status_code == 404
    assert client.delete(f"{V2}/users/{uid_b}/roles/{b_role['id']}", headers=adm_a).status_code == 404
    assert (
        client.post(f"{V2}/users", headers=adm_a, json={"email": "b-staff@example.com", "given_names": "x"}).status_code
        == 201  # same email in another tenant is not a conflict (and reveals nothing)
    )
    assert {r["name"] for r in client.get(f"{V2}/roles", headers=adm_a).json()} == {"Administrador de agencia", "Rol A"}
    assert all(e["subject_id"] != uid_b for e in client.get(f"{V2}/security/events", headers=adm_a).json())
    # nothing leaked into tenant B by the attempts
    assert client.get(f"{V2}/users/{uid_b}", headers=adm_b).json()["status"] == "active"
    # a role from another tenant can never grant permissions, even if a corrupt row existed
    with SessionLocal() as db:
        db.add(
            UserRoleAssignment(
                tenant_id=tenant_a["tenant_id"], user_id=uid_a, role_id=b_role["id"], scope_kind="tenant"
            )
        )
        db.commit()
    assert client.get(f"{V2}/users", headers=h(login(client, "a-staff@example.com"))).status_code == 403


def test_z05_spoofed_tenant_rejected_or_ignored(client, tenant_a, tenant_b):
    adm_a = admin_headers(client, tenant_a)
    r = client.post(
        f"{V2}/users",
        headers=adm_a,
        json={"email": "spoof@example.com", "given_names": "S", "tenant_id": tenant_b["tenant_id"]},
    )
    assert r.status_code == 422  # unknown field rejected
    r = client.post(
        f"{V2}/roles", headers=adm_a, json={"name": "X", "permissions": [], "tenant_id": tenant_b["tenant_id"]}
    )
    assert r.status_code == 422
    spoof = {**adm_a, "X-Tenant-ID": str(tenant_b["tenant_id"]), "X-User-ID": str(tenant_b["admin_id"])}
    users = client.get(f"{V2}/users", headers=spoof).json()
    assert {u["tenant_id"] for u in users} == {tenant_a["tenant_id"]}
    isolated = create_app()

    @isolated.get("/ctx")
    def ctx(_=Depends(get_auth_context)):
        c = get_context()
        return {"tenant": c.tenant_id, "actor": c.actor_id}

    with TestClient(isolated) as c2:
        got = c2.get("/ctx", headers=spoof).json()
    assert got == {"tenant": str(tenant_a["tenant_id"]), "actor": str(tenant_a["admin_id"])}


def test_z06_branch_and_resource_scope_foundation(client, sink, tenant_a, tenant_b):
    p = Principal(
        1, 1, None, 1, (Grant("users.read", "branch", 10), Grant("roles.read", "own"), Grant("roles.create", "tenant"))
    )
    assert (
        p.allows("users.read", branch_id=10) and not p.allows("users.read", branch_id=11) and not p.allows("users.read")
    )
    assert p.allows("roles.read", owner_id=1) and not p.allows("roles.read", owner_id=2) and not p.allows("roles.read")
    assert p.allows("roles.create") and not p.allows("roles.create", tenant_id=2)
    # through the API: a branch-scoped assignment needs a branch of the actor's own tenant
    adm = admin_headers(client, tenant_a)
    role = create_role(client, adm, "Lector", ["users.read"])
    user = activate_user(client, sink, adm, "branchy@example.com")
    bad = client.post(f"{V2}/users/{user['id']}/roles", headers=adm, json={"role_id": role["id"], "scope": "branch"})
    assert bad.status_code == 404
    foreign = client.post(
        f"{V2}/users/{user['id']}/roles",
        headers=adm,
        json={"role_id": role["id"], "scope": "branch", "branch_id": tenant_b["branch_id"]},
    )
    assert foreign.status_code == 404
    ok = client.post(
        f"{V2}/users/{user['id']}/roles",
        headers=adm,
        json={"role_id": role["id"], "scope": "branch", "branch_id": tenant_a["branch_id"]},
    )
    assert ok.status_code == 201
    me = client.get(f"{V2}/auth/me", headers=h(login(client, "branchy@example.com"))).json()
    assert me["permissions"] == [
        {"permission": "users.read", "scope": "branch", "branch_id": tenant_a["branch_id"], "cash_point_id": None}
    ]
    # a branch-scoped grant does not open the tenant-wide endpoint
    assert client.get(f"{V2}/users", headers=h(login(client, "branchy@example.com"))).status_code == 403


def test_z07_self_escalation_denied(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    assigner = create_role(client, adm, "Asignador", ["roles.assign", "roles.read"])
    extra = create_role(client, adm, "Extra", ["roles.read"])
    delegate = activate_user(client, sink, adm, "delegate@example.com", roles=[assigner])
    dh = h(login(client, "delegate@example.com"))
    r = client.post(f"{V2}/users/{delegate['id']}/roles", headers=dh, json={"role_id": extra["id"]})
    assert r.status_code == 403 and r.json()["error"]["code"] == "self_escalation_denied"
    me_admin = client.get(f"{V2}/auth/me", headers=adm).json()["user"]
    r = client.post(f"{V2}/users/{me_admin['id']}/roles", headers=adm, json={"role_id": extra["id"]})
    assert r.json()["error"]["code"] == "self_escalation_denied"  # nobody may assign to themselves, admins included
    assert (
        client.post(f"{V2}/users/{me_admin['id']}/disable", headers=adm).json()["error"]["code"]
        == "self_escalation_denied"
    )


def test_z08_assignment_above_delegation_ceiling_denied(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    assigner = create_role(
        client, adm, "Asignador", ["roles.assign", "roles.read", "roles.create", "users.disable", "users.read"]
    )
    rich = create_role(client, adm, "Rico", ["users.create", "roles.read"])
    activate_user(client, sink, adm, "delegate@example.com", roles=[assigner])
    target = activate_user(client, sink, adm, "target@example.com")
    boss = activate_user(client, sink, adm, "boss@example.com", roles=[rich])
    dh = h(login(client, "delegate@example.com"))
    r = client.post(f"{V2}/users/{target['id']}/roles", headers=dh, json={"role_id": rich["id"]})
    assert r.status_code == 403 and r.json()["error"]["code"] == "delegation_ceiling_violation"
    # cannot mint a role richer than oneself, nor strip/disable someone richer
    assert (
        client.post(f"{V2}/roles", headers=dh, json={"name": "Nuevo", "permissions": ["users.create"]}).status_code
        == 403
    )
    assert (
        client.post(f"{V2}/users/{boss['id']}/disable", headers=dh).json()["error"]["code"]
        == "delegation_ceiling_violation"
    )
    assert client.delete(f"{V2}/users/{boss['id']}/roles/{rich['id']}", headers=dh).status_code == 403
    # within the ceiling it works
    ok = client.post(f"{V2}/users/{target['id']}/roles", headers=dh, json={"role_id": assigner["id"]})
    assert ok.status_code == 201
    # branch-scoped holders cannot hand out tenant-wide authority
    branch_only = activate_user(client, sink, adm, "branch@example.com")
    client.post(
        f"{V2}/users/{branch_only['id']}/roles",
        headers=adm,
        json={"role_id": assigner["id"], "scope": "branch", "branch_id": tenant_a["branch_id"]},
    )
    assert (
        client.post(
            f"{V2}/users/{target['id']}/roles",
            headers=h(login(client, "branch@example.com")),
            json={"role_id": assigner["id"]},
        ).status_code
        == 403
    )


def test_platform_and_tenant_permissions_are_separate(client, tenant_a):
    adm = admin_headers(client, tenant_a)
    r = client.post(f"{V2}/roles", headers=adm, json={"name": "Plat", "permissions": ["platform.users.read"]})
    assert r.status_code == 404  # platform capabilities cannot be placed in a tenant role
    perms = {p["code"] for p in client.get(f"{V2}/permissions", headers=adm).json()}
    assert perms and not any(c.startswith("platform.") for c in perms)
    tenant_principal = Principal(1, 1, None, 1, (Grant("users.read", "tenant"),))
    assert not tenant_principal.allows("platform.users.read")


# ================================ Security ==========================================================
def test_s01_password_material_never_returned(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    bodies = [client.get(f"{V2}/users", headers=adm).text, client.get(f"{V2}/auth/me", headers=adm).text]
    user = activate_user(client, sink, adm, "x@example.com")
    bodies += [client.get(f"{V2}/users/{user['id']}", headers=adm).text, json.dumps(login(client, "x@example.com"))]
    bodies.append(client.get(f"{V2}/security/events", headers=adm).text)
    blob = "\n".join(bodies)
    assert "password_hash" not in blob and "argon2" not in blob.lower() and PW not in blob


def test_s02_secrets_never_reach_logs_or_stdout(client, sink, tenant_a, caplog, capfd):
    with caplog.at_level(logging.DEBUG):
        adm = admin_headers(client, tenant_a)
        user = activate_user(client, sink, adm, "log@example.com")
        client.post(f"{V2}/auth/recovery/request", json={"tenant_slug": "alfa", "email": "log@example.com"})
        secret = sink.last("log@example.com")["secret"]
        client.post(f"{V2}/auth/recovery/complete", json={"token": secret, "new_password": NEW_PW})
        login(client, "log@example.com", "bad-password-1", expect=401)
        login(client, "log@example.com", NEW_PW)
    out = capfd.readouterr()
    haystack = caplog.text + out.out + out.err
    with SessionLocal() as db:
        hashes = [u.password_hash for u in db.query(UserAccount).all()]
    secrets_seen = [m["secret"] for m in sink.messages] + [PW, NEW_PW, "bad-password-1"] + hashes
    for s in secrets_seen:
        assert s not in haystack
    assert "$argon2" not in haystack and user["id"]
    assert (
        "debug_code"
        not in client.post(f"{V2}/auth/recovery/request", json={"tenant_slug": "alfa", "email": "log@example.com"}).text
    )


def test_s03_account_enumeration_mitigated(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    staff = activate_user(client, sink, adm, "staff@example.com")
    client.post(f"{V2}/users/{staff['id']}/disable", headers=adm)

    def shape(email):
        r = client.post(f"{V2}/auth/recovery/request", json={"tenant_slug": "alfa", "email": email})
        return r.status_code, r.json()

    known, unknown, disabled = shape(tenant_a["email"]), shape("ghost@example.com"), shape("staff@example.com")
    assert known == unknown == disabled
    assert len([m for m in sink.messages if m["email"] == "staff@example.com"]) == 1  # only the invitation
    assert len([m for m in sink.messages if m["email"] == "ghost@example.com"]) == 0
    # the legacy enumerating endpoint is gone
    assert (
        client.post("/api/v1/auth/verify-reset-code", json={"email": "ghost@example.com", "code": "123456"}).status_code
        == 404
    )


def test_s04_secrets_absent_from_audit(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    user = activate_user(client, sink, adm, "audit@example.com")
    client.post(f"{V2}/auth/recovery/request", json={"tenant_slug": "alfa", "email": "audit@example.com"})
    secret = sink.last("audit@example.com")["secret"]
    client.post(f"{V2}/auth/recovery/complete", json={"token": secret, "new_password": NEW_PW})
    login(client, "audit@example.com", "bad-password-1", expect=401)
    with SessionLocal() as db:
        dump = json.dumps([[e.event_type, e.details, e.client_ip] for e in db.query(SecurityEvent).all()])
        hashes = [u.password_hash for u in db.query(UserAccount).all()]
    for s in [m["secret"] for m in sink.messages] + [PW, NEW_PW, "bad-password-1"] + hashes:
        assert s not in dump
    assert "audit@example.com" not in dump  # emails are not copied into audit details
    cleaned = clean_details(
        {"password": "x", "reset_token": "t", "role_id": 3, "api_key": "k", "note": "Bearer abc.def"}
    )
    assert set(cleaned) == {"role_id", "note"} and "abc.def" not in cleaned["note"]
    assert user["id"]


def test_s05_disable_preserves_history(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    role = create_role(client, adm, "Lector", ["users.read"])
    user = activate_user(client, sink, adm, "hist@example.com", roles=[role])
    login(client, "hist@example.com")
    client.post(f"{V2}/users/{user['id']}/disable", headers=adm)
    with SessionLocal() as db:
        u = db.get(UserAccount, user["id"])
        assert u.status == "disabled" and u.disabled_at is not None and u.person_id is not None
        assert db.get(Person, u.person_id) is not None
        assert db.query(UserRoleAssignment).filter_by(user_id=u.id).count() == 1
        sessions = db.query(UserSession).filter_by(user_id=u.id).all()
        assert sessions and all(not s.is_active and s.revoked_reason == "user_disabled" for s in sessions)
        assert db.query(SecurityEvent).filter_by(subject_id=u.id).count() >= 3
    assert client.post(f"{V2}/users/{user['id']}/enable", headers=adm).json()["status"] == "active"


# ================================ Audit / state / constraints ==========================================
def test_audit_events_cover_the_security_lifecycle(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    role = create_role(client, adm, "Lector", ["users.read"])
    user = activate_user(client, sink, adm, "life@example.com", roles=[role])
    login(client, "life@example.com", "bad-password-1", expect=401)
    tokens = login(client, "life@example.com")
    client.post(f"{V2}/users/{user['id']}/sessions/revoke", headers=adm)
    client.post(f"{V2}/users/{user['id']}/disable", headers=adm)
    client.post(f"{V2}/users/{user['id']}/enable", headers=adm)
    client.delete(f"{V2}/users/{user['id']}/roles/{role['id']}", headers=adm)
    client.post(f"{V2}/auth/logout", headers=adm)
    seen = {e.event_type for e in events()}
    assert seen >= {
        "auth.login.succeeded",
        "auth.login.failed",
        "auth.logout",
        "recovery.completed",
        "password.changed",
        "user.created",
        "user.invited",
        "user.disabled",
        "user.enabled",
        "role.assigned",
        "role.removed",
        "role.created",
        "session.revoked",
    }
    assert client.get(f"{V2}/auth/me", headers=h(tokens)).status_code == 401
    with SessionLocal() as db:  # correlation id of the request is stored with the event
        ev = db.query(SecurityEvent).filter_by(event_type="user.disabled").one()
        assert ev.correlation_id and ev.actor_id == tenant_a["admin_id"] and ev.tenant_id == tenant_a["tenant_id"]


def test_recovery_request_event_without_secret_and_correlation(client, sink, tenant_a):
    r = client.post(
        f"{V2}/auth/recovery/request",
        json={"tenant_slug": "alfa", "email": tenant_a["email"]},
        headers={"X-Correlation-ID": "trace-recovery-001"},
    )
    assert r.status_code == 202
    ev = events("recovery.requested")[-1]
    assert ev.correlation_id == "trace-recovery-001" and ev.details == {"issued": True, "purpose": "recovery"}


def test_invalid_state_transitions_and_duplicates(client, sink, tenant_a, tenant_b):
    adm = admin_headers(client, tenant_a)
    role = create_role(client, adm, "Lector", ["users.read"])
    assert create_role(client, adm, "Lector", ["users.read"], expect=409)["error"]["code"] == "conflict"
    create_role(client, admin_headers(client, tenant_b), "Lector", ["users.read"])  # same name, other tenant: fine
    user = activate_user(client, sink, adm, "state@example.com", roles=[role])
    dup = client.post(f"{V2}/users/{user['id']}/roles", headers=adm, json={"role_id": role["id"]})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "duplicate_role_assignment"
    assert (
        client.post(f"{V2}/users/{user['id']}/enable", headers=adm).json()["error"]["code"]
        == "invalid_state_transition"
    )
    client.post(f"{V2}/users/{user['id']}/disable", headers=adm)
    assert (
        client.post(f"{V2}/users/{user['id']}/disable", headers=adm).json()["error"]["code"]
        == "invalid_state_transition"
    )
    assert client.post(f"{V2}/users/{user['id']}/roles", headers=adm, json={"role_id": role["id"]}).status_code == 409


def test_pending_user_activation_flow_and_person_separation(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    r = client.post(
        f"{V2}/users", headers=adm, json={"email": "NEW@Example.com", "given_names": "Maria", "family_names": "Gomez"}
    )
    user = r.json()
    assert r.status_code == 201 and user["status"] == "pending" and user["email"] == "new@example.com"
    assert login(client, "new@example.com", PW, expect=401)  # no usable password yet
    with SessionLocal() as db:
        u = db.get(UserAccount, user["id"])
        person = db.get(Person, u.person_id)
        assert person.given_names == "Maria" and person.tenant_id == tenant_a["tenant_id"] and u.role is None
        assert not hasattr(person, "password_hash")  # Person != UserAccount
        assert db.query(UserRoleAssignment).filter_by(user_id=u.id).count() == 0  # creating != granting
    client.post(
        f"{V2}/auth/recovery/complete", json={"token": sink.last("new@example.com")["secret"], "new_password": PW}
    )
    assert login(client, "new@example.com")["user"]["status"] == "active"


def test_sync_catalog_is_idempotent_and_complete():
    with SessionLocal() as db:
        first = sync_permission_catalog(db)
        db.commit()
        again = sync_permission_catalog(db)
        assert set(first) == set(again) == {p.code for p in CATALOG}


def test_unique_constraints_are_tenant_aware(tenant_a, tenant_b):
    with SessionLocal() as db:
        db.add(Role(tenant_id=tenant_a["tenant_id"], name="Dup"))
        db.add(Role(tenant_id=tenant_b["tenant_id"], name="Dup"))
        db.commit()
        db.add(Role(tenant_id=tenant_a["tenant_id"], name="Dup"))
        with pytest.raises(Exception, match="uq_roles_tenant_name"):
            db.commit()
        db.rollback()
        db.add_all([Role(tenant_id=None, name="Plat"), Role(tenant_id=None, name="Plat")])
        with pytest.raises(Exception, match="uq_roles_platform_name"):
            db.commit()


def test_legacy_v1_login_is_throttled_and_register_bootstraps_admin_role(client, tenant_a):
    r = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Nuevo Dueno",
            "email": "owner@example.com",
            "password": "legacy-pass-123",
            "company_name": "Gamma",
        },
    )
    assert r.status_code == 201, r.text
    tokens = client.post(
        "/api/v1/auth/login", json={"email": "owner@example.com", "password": "legacy-pass-123"}
    ).json()
    me = client.get(f"{V2}/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}).json()
    assert {g["permission"] for g in me["permissions"]} >= {
        "users.read",
        "roles.assign",
    }  # tenant admin role bootstrapped
    for _ in range(settings.auth_max_failures_account_ip):
        assert (
            client.post(
                "/api/v1/auth/login", json={"email": "owner@example.com", "password": "bad-password-1"}
            ).status_code
            == 401
        )
    blocked = client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "legacy-pass-123"})
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "rate_limited"


# ================================ Migration artefacts ================================================
def test_security_events_are_append_only_and_catalog_seeded_by_migration(scratch_db):
    assert _alembic(scratch_db, "upgrade", "head").returncode == 0
    from sqlalchemy import create_engine

    eng = create_engine(scratch_db)
    try:
        with eng.begin() as c:
            assert c.execute(text("SELECT count(*) FROM permissions")).scalar() == len(CATALOG)
            c.execute(
                text(
                    "INSERT INTO security_events (event_type, outcome, details, occurred_at) "
                    "VALUES ('x','success','{}', now())"
                )
            )
        for stmt in ("UPDATE security_events SET outcome='tampered'", "DELETE FROM security_events"):
            with pytest.raises(Exception, match="append-only"):
                with eng.begin() as c:
                    c.execute(text(stmt))
        with eng.begin() as c:
            assert c.execute(text("SELECT count(*) FROM security_events")).scalar() == 1
    finally:
        eng.dispose()
    down = _alembic(scratch_db, "downgrade", "0001")
    assert down.returncode == 0, down.stderr
    assert _alembic(scratch_db, "upgrade", "head").returncode == 0


def test_recovery_token_is_single_use_under_concurrency(sink, tenant_a):
    from concurrent.futures import ThreadPoolExecutor

    with TestClient(app) as c:
        c.post(f"{V2}/auth/recovery/request", json={"tenant_slug": "alfa", "email": tenant_a["email"]})
    secret = sink.last(tenant_a["email"])["secret"]

    def attempt(i):
        with TestClient(app) as c:
            return c.post(
                f"{V2}/auth/recovery/complete", json={"token": secret, "new_password": f"concurrent-pass-{i}-xyz"}
            ).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, range(6)))
    assert results.count(200) == 1 and all(r == 400 for r in results if r != 200)


# ---------------------------- /auth/me exposes the CashPoint of a cash_point-scoped grant (WEB-CASH-PRE-01) ----------
def _me_grants(client, email):
    r = client.get(f"{V2}/auth/me", headers=h(login(client, email)))
    assert r.status_code == 200, r.text
    return r.json()["permissions"]


def test_me_grants_expose_cash_point_id_per_scope(client, sink, tenant_a):
    adm = admin_headers(client, tenant_a)
    role = create_role(client, adm, "Apertura", ["cash.sessions.open"])
    cp_a = client.post(
        f"{V2}/cash-points", headers=adm, json={"branch_id": tenant_a["branch_id"], "code": "PA", "name": "A"}
    ).json()
    cp_b = client.post(
        f"{V2}/cash-points", headers=adm, json={"branch_id": tenant_a["branch_id"], "code": "PB", "name": "B"}
    ).json()
    assert cp_a["id"] != cp_b["id"]

    def assign(email, **body):
        user = activate_user(client, sink, adm, email)
        r = client.post(f"{V2}/users/{user['id']}/roles", headers=adm, json={"role_id": role["id"], **body})
        assert r.status_code == 201, r.text

    assign("tenant-u@example.com", scope="tenant")
    assign("branch-u@example.com", scope="branch", branch_id=tenant_a["branch_id"])
    assign("cp-u@example.com", scope="cash_point", cash_point_id=cp_a["id"])
    assign("own-u@example.com", scope="own")

    open_ = "cash.sessions.open"
    assert _me_grants(client, "tenant-u@example.com") == [
        {"permission": open_, "scope": "tenant", "branch_id": None, "cash_point_id": None}
    ]
    assert _me_grants(client, "branch-u@example.com") == [
        {"permission": open_, "scope": "branch", "branch_id": tenant_a["branch_id"], "cash_point_id": None}
    ]
    cp_grants = _me_grants(client, "cp-u@example.com")
    assert len(cp_grants) == 1 and cp_grants[0]["permission"] == open_ and cp_grants[0]["scope"] == "cash_point"
    assert cp_grants[0]["cash_point_id"] == cp_a["id"]  # exact target, never the sibling CashPoint
    assert cp_grants[0]["cash_point_id"] != cp_b["id"]
    assert set(cp_grants[0]) == {"permission", "scope", "branch_id", "cash_point_id"}  # nothing else leaks
    assert _me_grants(client, "own-u@example.com") == [
        {"permission": open_, "scope": "own", "branch_id": None, "cash_point_id": None}
    ]
