"""Google sign-in on top of OIDC (login + explicit linking).

Google proves *who* is at the keyboard; Fast Money stays authoritative for tenant, account, status, roles,
permissions and sessions. Consequences enforced here:

* the tenant comes from the slug (never from the token) and is resolved before anything else;
* an account is found only through an explicitly linked ``(tenant, issuer, sub)`` — the email claim is never
  used to find or link an account;
* linking requires an authenticated session of the target account plus a fresh single-use nonce;
* disabled / locked accounts are refused even when Google authenticates correctly;
* the session is the same server-side session as with passwords (revocable, audited);
* no Google access/refresh/id tokens are stored.
"""

import hmac
import logging
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_token, new_opaque_token
from app.core.time import now_utc
from app.modules.identity import throttle
from app.modules.identity.audit import record_event
from app.modules.identity.auth import (
    S_IP,
    LoginResult,
    _issue_session,
)
from app.modules.identity.errors import (
    AccountDisabled,
    AccountLocked,
    ExternalIdentityConflict,
    ExternalIdentityNotLinked,
    InvalidExternalToken,
    PermissionDenied,
    RateLimited,
)
from app.modules.identity.models import ExternalIdentity, OidcChallenge, UserAccount
from app.modules.identity.oidc import OidcError, OidcVerifier, VerifiedIdentity
from app.modules.identity.tenant import resolve_tenant

log = logging.getLogger("app.identity.google")
PROVIDER = "google"
S_CHALLENGE_IP = "oidc_challenge_ip"
CHALLENGE_MAX_PER_WINDOW = 60


def create_challenge(
    db: Session,
    *,
    tenant_id: int | None,
    purpose: str,
    user_id: int | None,
    client_ip: str | None,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """Issue a single-use nonce for the client to embed in its provider request.

    ``tenant_id=None`` (unknown slug) still returns a plausible nonce without storing anything, so the
    response does not reveal which tenants exist; the later login fails generically.
    """
    now = now or now_utc()
    ip = client_ip or "unknown"
    wait = throttle.retry_after(db, S_CHALLENGE_IP, ip, now)
    if wait:
        raise RateLimited(wait)
    throttle.register_failure(db, S_CHALLENGE_IP, ip, CHALLENGE_MAX_PER_WINDOW, now)
    nonce = new_opaque_token()
    expires = now + timedelta(seconds=settings.oidc_challenge_ttl_seconds)
    if tenant_id is not None:
        db.add(
            OidcChallenge(
                nonce_hash=hash_token(nonce), tenant_id=tenant_id, purpose=purpose, user_id=user_id, expires_at=expires
            )
        )
    db.commit()
    return nonce, expires


def _consume_challenge(
    db: Session, nonce: str, *, tenant_id: int, purpose: str, user_id: int | None, now: datetime
) -> bool:
    """Atomic single use, bound to tenant, purpose (and user for linking)."""
    stmt = (
        update(OidcChallenge)
        .where(
            OidcChallenge.nonce_hash == hash_token(nonce),
            OidcChallenge.tenant_id == tenant_id,
            OidcChallenge.purpose == purpose,
            OidcChallenge.used_at.is_(None),
            OidcChallenge.expires_at > now,
        )
        .values(used_at=now)
    )
    stmt = stmt.where(OidcChallenge.user_id.is_(None) if user_id is None else OidcChallenge.user_id == user_id)
    return bool(db.execute(stmt).rowcount)


def _verify(verifier: OidcVerifier, id_token: str, nonce: str) -> VerifiedIdentity:
    try:
        identity = verifier.verify(id_token)
    except OidcError:
        raise InvalidExternalToken() from None
    if not identity.nonce or not hmac.compare_digest(identity.nonce, nonce):
        raise InvalidExternalToken()
    return identity


def _fail(db: Session, ip: str, client_ip: str | None, now: datetime, reason: str, tenant_id: int | None = None):
    throttle.register_failure(db, S_IP, ip, settings.auth_max_failures_ip, now)
    record_event(
        db,
        "auth.login.failed",
        outcome="failure",
        tenant_id=tenant_id,
        client_ip=client_ip,
        details={"method": PROVIDER, "reason": reason},
    )
    db.commit()


def login_google(
    db: Session,
    verifier: OidcVerifier,
    *,
    tenant_slug: str,
    id_token: str,
    nonce: str,
    device_name: str | None,
    client_ip: str | None,
    now: datetime | None = None,
) -> LoginResult:
    now = now or now_utc()
    ip = client_ip or "unknown"
    wait = throttle.retry_after(db, S_IP, ip, now)
    if wait:
        raise RateLimited(wait)

    try:
        identity = _verify(verifier, id_token, nonce)
    except InvalidExternalToken:
        _fail(db, ip, client_ip, now, "invalid_external_token")
        raise
    company = resolve_tenant(db, tenant_slug)
    consumed = company is not None and _consume_challenge(
        db, nonce, tenant_id=company.id, purpose="login", user_id=None, now=now
    )
    if not consumed:  # unknown tenant, or nonce unknown/expired/used/for another tenant
        _fail(db, ip, client_ip, now, "challenge_rejected", company.id if company else None)
        raise InvalidExternalToken() if company is not None else ExternalIdentityNotLinked()

    link = db.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.tenant_id == company.id,
            ExternalIdentity.provider == PROVIDER,
            ExternalIdentity.issuer == identity.issuer,
            ExternalIdentity.subject == identity.subject,
            ExternalIdentity.revoked_at.is_(None),
        )
    )
    user = db.get(UserAccount, link.user_id) if link else None
    if user is None or user.company_id != company.id or user.status == "pending":
        _fail(db, ip, client_ip, now, "identity_not_linked", company.id)
        raise ExternalIdentityNotLinked()

    if user.status == "locked" and user.locked_until is not None and user.locked_until <= now:
        user.status, user.locked_at, user.locked_until = "active", None, None
        record_event(
            db, "account.unlocked", tenant_id=company.id, subject_id=user.id, details={"reason": "lock_expired"}
        )
    if user.status in ("disabled", "locked"):
        record_event(
            db,
            "auth.login.denied",
            outcome="denied",
            tenant_id=company.id,
            subject_id=user.id,
            client_ip=client_ip,
            details={"method": PROVIDER, "reason": user.status},
        )
        db.commit()
        raise AccountDisabled() if user.status == "disabled" else AccountLocked()

    user.last_login_at = now
    link.last_login_at = now
    session, access, refresh = _issue_session(db, user, device_name, client_ip, now)
    record_event(
        db,
        "auth.login.succeeded",
        tenant_id=company.id,
        actor_id=user.id,
        subject_id=user.id,
        client_ip=client_ip,
        details={"method": PROVIDER, "session_id": session.id},
    )
    db.commit()
    return LoginResult(user, session, access, refresh)


def link_google(
    db: Session,
    verifier: OidcVerifier,
    user: UserAccount,
    *,
    id_token: str,
    nonce: str,
    client_ip: str | None,
    now: datetime | None = None,
) -> ExternalIdentity:
    """Explicit, authenticated linking. Never driven by an email match."""
    now = now or now_utc()
    if user.company_id is None:
        raise PermissionDenied()
    identity = _verify(verifier, id_token, nonce)
    if not _consume_challenge(db, nonce, tenant_id=user.company_id, purpose="link", user_id=user.id, now=now):
        raise InvalidExternalToken()

    existing = db.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.tenant_id == user.company_id,
            ExternalIdentity.provider == PROVIDER,
            ExternalIdentity.issuer == identity.issuer,
            ExternalIdentity.subject == identity.subject,
            ExternalIdentity.revoked_at.is_(None),
        )
    )
    if existing is not None:
        db.commit()  # the consumed challenge stays consumed
        if existing.user_id == user.id:
            return existing  # idempotent
        raise ExternalIdentityConflict()
    mine = db.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.user_id == user.id,
            ExternalIdentity.provider == PROVIDER,
            ExternalIdentity.revoked_at.is_(None),
        )
    )
    if mine is not None:
        db.commit()
        raise ExternalIdentityConflict("La cuenta ya tiene otra identidad de Google vinculada.")
    row = ExternalIdentity(
        tenant_id=user.company_id,
        user_id=user.id,
        provider=PROVIDER,
        issuer=identity.issuer,
        subject=identity.subject,
        email=identity.email,
        email_verified=identity.email_verified,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ExternalIdentityConflict() from None
    record_event(
        db,
        "identity.google.linked",
        tenant_id=user.company_id,
        actor_id=user.id,
        subject_id=user.id,
        client_ip=client_ip,
        details={"provider": PROVIDER},
    )
    db.commit()
    return row


def unlink_google(db: Session, user: UserAccount, client_ip: str | None, now: datetime | None = None) -> bool:
    now = now or now_utc()
    rows = db.scalars(
        select(ExternalIdentity).where(
            ExternalIdentity.user_id == user.id,
            ExternalIdentity.provider == PROVIDER,
            ExternalIdentity.revoked_at.is_(None),
        )
    ).all()
    for r in rows:
        r.revoked_at = now  # history kept
    if rows:
        record_event(
            db,
            "identity.google.unlinked",
            tenant_id=user.company_id,
            actor_id=user.id,
            subject_id=user.id,
            client_ip=client_ip,
            details={"provider": PROVIDER},
        )
    db.commit()
    return bool(rows)
