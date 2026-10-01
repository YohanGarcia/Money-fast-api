"""Google sign-in (OIDC) against a FAKE provider: own RSA key, JWKS, issuer and audience.

The real Google connection (client id + network) is BLOCKED_BY_EVIDENCE; everything the backend decides —
signature/alg/iss/aud/exp/nonce validation, tenant resolution, explicit linking, status checks, sessions — is
exercised here. PostgreSQL only.
"""

import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt
from sqlalchemy import select, text, update

from app.core.config import settings
from app.core.db import SessionLocal
from app.main import app
from app.models.session import UserSession
from app.modules.identity.models import ExternalIdentity, OidcChallenge, SecurityEvent, UserAccount
from app.modules.identity.oidc import GOOGLE_ISSUERS, OidcVerifier, build_google_verifier
from tests import pg_env  # noqa: F401  (must precede app imports)
from tests.test_t002_identity import (  # noqa: F401  (fixtures + helpers shared with the T-002 suite)
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

AUD = "fake-client-id.apps.fake"
ISS = "https://accounts.google.com"


def _keypair(kid):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    jwk_dict = jwk.construct(public, "RS256").to_dict()
    jwk_dict.update(kid=kid, use="sig")
    return private, jwk_dict


PRIVATE, PUBLIC_JWK = _keypair("fake-kid-1")
OTHER_PRIVATE, _ = _keypair("fake-kid-1")  # same kid, different key: a forged signature


def token(sub, nonce, *, iss=ISS, aud=AUD, exp_in=300, key=PRIVATE, alg="RS256", kid="fake-kid-1", email=None, **extra):
    claims = {"iss": iss, "aud": aud, "sub": sub, "iat": int(time.time()), "exp": int(time.time()) + exp_in}
    if nonce is not None:
        claims["nonce"] = nonce
    if email:
        claims.update(email=email, email_verified=True)
    claims.update(extra)
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


@pytest.fixture()
def provider():
    verifier = OidcVerifier(issuers=GOOGLE_ISSUERS, audience=AUD, jwks_provider=lambda: {"keys": [PUBLIC_JWK]})
    app.dependency_overrides[build_google_verifier] = lambda: verifier
    yield verifier
    app.dependency_overrides.pop(build_google_verifier, None)


def login_challenge(client, slug):
    r = client.post(f"{V2}/auth/google/challenge", json={"tenant_slug": slug})
    assert r.status_code == 200, r.text
    return r.json()["nonce"]


def google_login(client, slug, sub, *, nonce=None, expect=200, **tok):
    nonce = nonce or login_challenge(client, slug)
    r = client.post(
        f"{V2}/auth/google/login", json={"tenant_slug": slug, "id_token": token(sub, nonce, **tok), "nonce": nonce}
    )
    assert r.status_code == expect, r.text
    return r


def link(client, tokens, sub, *, expect=201, **tok):
    ch = client.post(f"{V2}/auth/google/link/challenge", headers=h(tokens))
    assert ch.status_code == 200, ch.text
    nonce = ch.json()["nonce"]
    r = client.post(
        f"{V2}/auth/google/link", headers=h(tokens), json={"id_token": token(sub, nonce, **tok), "nonce": nonce}
    )
    assert r.status_code == expect, r.text
    return r


def linked_user(client, sink, tenant, email, sub):
    adm = admin_headers(client, tenant)
    user = activate_user(client, sink, adm, email)
    tokens = login(client, email, slug=tenant["slug"])
    link(client, tokens, sub)
    return user, adm


# ====================================================================================================
def test_google_identity_resolves_inside_the_correct_tenant(client, sink, provider, tenant_a, tenant_b):
    a, _ = linked_user(client, sink, tenant_a, "dup@example.com", "google-sub-1")
    b, _ = linked_user(client, sink, tenant_b, "dup@example.com", "google-sub-1")  # same Google identity, other tenant
    ra = google_login(client, "alfa", "google-sub-1").json()
    rb = google_login(client, "beta", "google-sub-1").json()
    assert ra["user"]["id"] == a["id"] and ra["user"]["tenant_id"] == tenant_a["tenant_id"]
    assert rb["user"]["id"] == b["id"] and rb["user"]["tenant_id"] == tenant_b["tenant_id"]
    assert client.get(f"{V2}/auth/me", headers=h(ra)).json()["user"]["id"] == a["id"]
    # the password login of the same account keeps working (Google does not replace it)
    assert login(client, "dup@example.com", slug="alfa")["user"]["id"] == a["id"]
    # a Google identity linked only in alfa does not open beta
    linked_user(client, sink, tenant_a, "solo@example.com", "google-sub-only-alfa")
    assert (
        google_login(client, "beta", "google-sub-only-alfa", expect=401).json()["error"]["code"]
        == "external_identity_not_linked"
    )


def test_disabled_and_locked_google_users_cannot_enter(client, sink, provider, tenant_a):
    user, adm = linked_user(client, sink, tenant_a, "gu@example.com", "sub-gu")
    assert google_login(client, "alfa", "sub-gu").status_code == 200
    client.post(f"{V2}/users/{user['id']}/disable", headers=adm)
    denied = google_login(client, "alfa", "sub-gu", expect=403)
    assert denied.json()["error"]["code"] == "account_disabled"
    client.post(f"{V2}/users/{user['id']}/enable", headers=adm)
    with SessionLocal() as db:
        from datetime import timedelta

        from app.core.time import now_utc

        db.execute(
            update(UserAccount)
            .where(UserAccount.id == user["id"])
            .values(status="locked", locked_until=now_utc() + timedelta(hours=1))
        )
        db.commit()
        before = db.query(UserSession).filter_by(user_id=user["id"]).count()
    assert google_login(client, "alfa", "sub-gu", expect=403).json()["error"]["code"] == "account_locked"
    with SessionLocal() as db:
        assert db.query(UserSession).filter_by(user_id=user["id"]).count() == before  # no new session


def test_no_auto_link_by_email(client, sink, provider, tenant_a):
    adm = admin_headers(client, tenant_a)
    victim = activate_user(client, sink, adm, "victim@example.com")
    # Google says the holder owns victim@example.com (verified) — that alone must grant nothing
    r = google_login(client, "alfa", "attacker-sub", expect=401, email="victim@example.com")
    assert r.json()["error"]["code"] == "external_identity_not_linked"
    with SessionLocal() as db:
        assert db.query(ExternalIdentity).count() == 0
        assert db.query(UserSession).filter_by(user_id=victim["id"]).count() == 0
    # linking is explicit: needs an authenticated session, and the challenge is bound to that user
    assert client.post(f"{V2}/auth/google/link", json={"id_token": "x" * 40, "nonce": "y" * 40}).status_code == 401
    victim_tokens = login(client, "victim@example.com")
    other = activate_user(client, sink, adm, "other@example.com")
    other_tokens = login(client, "other@example.com")
    nonce = client.post(f"{V2}/auth/google/link/challenge", headers=h(victim_tokens)).json()["nonce"]
    stolen = client.post(
        f"{V2}/auth/google/link", headers=h(other_tokens), json={"id_token": token("s", nonce), "nonce": nonce}
    )
    assert stolen.status_code == 401 and stolen.json()["error"]["code"] == "invalid_external_token"
    assert other["id"] and link(client, victim_tokens, "victim-sub").status_code == 201


def test_duplicate_google_subject_in_the_same_tenant_is_rejected(client, sink, provider, tenant_a):
    adm = admin_headers(client, tenant_a)
    activate_user(client, sink, adm, "one@example.com")
    activate_user(client, sink, adm, "two@example.com")
    t1, t2 = login(client, "one@example.com"), login(client, "two@example.com")
    link(client, t1, "shared-sub")
    link(client, t1, "shared-sub", expect=201)  # idempotent for the same account
    conflict = link(client, t2, "shared-sub", expect=409)
    assert conflict.json()["error"]["code"] == "external_identity_conflict"
    assert link(client, t1, "a-different-sub", expect=409)  # one Google identity per account
    with SessionLocal() as db:  # and the database enforces it independently of the application
        row = db.scalar(select(ExternalIdentity))
        db.add(
            ExternalIdentity(
                tenant_id=row.tenant_id,
                user_id=row.user_id + 1,
                provider="google",
                issuer=row.issuer,
                subject=row.subject,
            )
        )
        with pytest.raises(Exception, match="uq_external_identity_active_subject"):
            db.commit()


@pytest.mark.parametrize(
    "bad",
    [
        {"iss": "https://evil.example.com"},
        {"aud": "someone-elses-client"},
        {"exp_in": -120},
        {"key": OTHER_PRIVATE},  # signature does not verify
        {"kid": "unknown-kid"},
    ],
    ids=["issuer", "audience", "expired", "forged-signature", "unknown-kid"],
)
def test_invalid_id_tokens_are_rejected(client, sink, provider, tenant_a, bad):
    linked_user(client, sink, tenant_a, "gu@example.com", "sub-ok")
    r = google_login(client, "alfa", "sub-ok", expect=401, **bad)
    assert r.json()["error"]["code"] == "invalid_external_token"
    assert google_login(client, "alfa", "sub-ok").status_code == 200  # control: the valid one passes


def test_alg_none_and_hs256_confusion_are_rejected(client, sink, provider, tenant_a):
    linked_user(client, sink, tenant_a, "gu@example.com", "sub-ok")
    nonce = login_challenge(client, "alfa")
    claims = {"iss": ISS, "aud": AUD, "sub": "sub-ok", "exp": int(time.time()) + 300, "nonce": nonce}
    import base64
    import json

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    unsigned = f"{b64({'alg': 'none', 'kid': 'fake-kid-1'})}.{b64(claims)}."
    hs = jwt.encode(claims, "anything", algorithm="HS256", headers={"kid": "fake-kid-1"})
    for forged in (unsigned, hs):
        r = client.post(f"{V2}/auth/google/login", json={"tenant_slug": "alfa", "id_token": forged, "nonce": nonce})
        assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_external_token"


def test_nonce_is_validated_single_use_and_tenant_bound(client, sink, provider, tenant_a, tenant_b):
    linked_user(client, sink, tenant_a, "gu@example.com", "sub-ok")
    # token without nonce / with a different nonce than the challenge
    nonce = login_challenge(client, "alfa")
    r = client.post(
        f"{V2}/auth/google/login", json={"tenant_slug": "alfa", "id_token": token("sub-ok", None), "nonce": nonce}
    )
    assert r.status_code == 401
    r = client.post(
        f"{V2}/auth/google/login", json={"tenant_slug": "alfa", "id_token": token("sub-ok", "z" * 43), "nonce": nonce}
    )
    assert r.status_code == 401
    # a good login consumes the nonce; replaying the same token+nonce fails
    nonce = login_challenge(client, "alfa")
    good = {"tenant_slug": "alfa", "id_token": token("sub-ok", nonce), "nonce": nonce}
    assert client.post(f"{V2}/auth/google/login", json=good).status_code == 200
    assert client.post(f"{V2}/auth/google/login", json=good).status_code == 401
    # a nonce issued for tenant beta is useless in alfa
    beta_nonce = login_challenge(client, "beta")
    r = client.post(
        f"{V2}/auth/google/login",
        json={"tenant_slug": "alfa", "id_token": token("sub-ok", beta_nonce), "nonce": beta_nonce},
    )
    assert r.status_code == 401
    # expired challenge
    nonce = login_challenge(client, "alfa")
    with SessionLocal() as db:
        from datetime import timedelta

        from app.core.time import now_utc

        db.execute(update(OidcChallenge).values(expires_at=now_utc() - timedelta(seconds=1)))
        db.commit()
    r = client.post(
        f"{V2}/auth/google/login", json={"tenant_slug": "alfa", "id_token": token("sub-ok", nonce), "nonce": nonce}
    )
    assert r.status_code == 401


def test_google_session_is_revocable_like_a_password_session(client, sink, provider, tenant_a):
    user, adm = linked_user(client, sink, tenant_a, "gu@example.com", "sub-ok")
    s1 = google_login(client, "alfa", "sub-ok").json()
    s2 = google_login(client, "alfa", "sub-ok").json()
    assert client.get(f"{V2}/auth/me", headers=h(s1)).status_code == 200
    assert client.post(f"{V2}/auth/logout", headers=h(s1)).status_code == 204
    assert client.get(f"{V2}/auth/me", headers=h(s1)).status_code == 401
    assert client.post(f"{V2}/auth/refresh", json={"refresh_token": s1["refresh_token"]}).status_code == 401
    assert client.post(f"{V2}/users/{user['id']}/sessions/revoke", headers=adm).json()["revoked"] >= 1
    assert client.get(f"{V2}/auth/me", headers=h(s2)).status_code == 401
    s3 = google_login(client, "alfa", "sub-ok").json()
    client.post(f"{V2}/users/{user['id']}/disable", headers=adm)
    assert client.get(f"{V2}/auth/me", headers=h(s3)).status_code == 401
    with SessionLocal() as db:
        types = {e.event_type for e in db.query(SecurityEvent).all()}
    assert {"identity.google.linked", "auth.login.succeeded", "auth.logout", "session.revoked"} <= types


def test_unknown_slug_reveals_nothing_and_unlink_keeps_history(client, sink, provider, tenant_a):
    user, _ = linked_user(client, sink, tenant_a, "gu@example.com", "sub-ok")
    # the challenge answer has the same shape for real and unknown tenants
    real = client.post(f"{V2}/auth/google/challenge", json={"tenant_slug": "alfa"})
    ghost = client.post(f"{V2}/auth/google/challenge", json={"tenant_slug": "no-such-agency"})
    assert real.status_code == ghost.status_code == 200 and set(real.json()) == set(ghost.json())
    nonce = ghost.json()["nonce"]
    a = client.post(
        f"{V2}/auth/google/login",
        json={"tenant_slug": "no-such-agency", "id_token": token("sub-ok", nonce), "nonce": nonce},
    )
    nonce2 = login_challenge(client, "alfa")
    b = google_login(client, "alfa", "not-linked-sub", nonce=nonce2, expect=401)
    assert a.status_code == b.status_code == 401
    assert a.json()["error"]["code"] == b.json()["error"]["code"] == "external_identity_not_linked"
    # unlinking revokes (row kept) and the identity can no longer log in
    tokens = login(client, "gu@example.com")
    assert client.delete(f"{V2}/auth/google/link", headers=h(tokens)).status_code == 204
    google_login(client, "alfa", "sub-ok", expect=401)
    with SessionLocal() as db:
        rows = db.query(ExternalIdentity).filter_by(user_id=user["id"]).all()
        assert len(rows) == 1 and rows[0].revoked_at is not None
    link(client, login(client, "gu@example.com"), "sub-ok")  # can be linked again afterwards
    assert google_login(client, "alfa", "sub-ok").status_code == 200


def test_no_provider_tokens_or_identifiers_are_stored_or_audited(client, sink, provider, tenant_a):
    linked_user(client, sink, tenant_a, "gu@example.com", "sub-secret-123")
    google_login(client, "alfa", "sub-secret-123", email="gu@example.com")
    with SessionLocal() as db:
        cols = {
            r[0]
            for r in db.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name='external_identities'")
            )
        }
        assert not {c for c in cols if "token" in c or "secret" in c}
        dump = str([[e.event_type, e.details] for e in db.query(SecurityEvent).all()])
    assert "sub-secret-123" not in dump and "gu@example.com" not in dump


def test_google_failures_are_throttled_and_unconfigured_provider_is_blocked_by_evidence(
    client, sink, tenant_a, monkeypatch
):
    # BLOCKED_BY_EVIDENCE: with no GOOGLE_CLIENT_ID (and no network) the real provider cannot be reached.
    assert settings.google_client_id == ""
    nonce = client.post(f"{V2}/auth/google/challenge", json={"tenant_slug": "alfa"}).json()["nonce"]
    r = client.post(f"{V2}/auth/google/login", json={"tenant_slug": "alfa", "id_token": "x" * 40, "nonce": nonce})
    assert r.status_code == 503 and r.json()["error"]["code"] == "service_unavailable"
    # with a (fake) provider, repeated failures hit the same per-IP backoff as password failures
    verifier = OidcVerifier(issuers=GOOGLE_ISSUERS, audience=AUD, jwks_provider=lambda: {"keys": [PUBLIC_JWK]})
    app.dependency_overrides[build_google_verifier] = lambda: verifier
    try:
        monkeypatch.setattr(settings, "auth_max_failures_ip", 2)
        codes = [google_login(client, "alfa", "nobody", expect=401).status_code for _ in range(2)]
        assert codes == [401, 401]
        blocked = client.post(
            f"{V2}/auth/google/login", json={"tenant_slug": "alfa", "id_token": "x" * 40, "nonce": "n" * 43}
        )
        assert blocked.status_code == 429 and "Retry-After" in blocked.headers
    finally:
        app.dependency_overrides.pop(build_google_verifier, None)
