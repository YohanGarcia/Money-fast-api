"""Authentication: login, refresh, logout, session validation/revocation, password change.

Anti-abuse (DR-010): three exponential-backoff counters guard login — per (account, client IP), per
client IP, and per account (which also drives a temporary account lock). Counters are keyed by the
normalised identifier whether or not the account exists, so the response never depends on existence.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from jose import JWTError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.context import bind_context, get_context
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    dummy_hash,
    get_password_hash,
    hash_token,
    password_policy_violation,
    verify_and_update,
    verify_token_hash,
)
from app.core.time import now_utc
from app.models.session import UserSession
from app.modules.identity import throttle
from app.modules.identity.audit import record_event
from app.modules.identity.errors import (
    AccountDisabled,
    AccountLocked,
    InvalidCredentials,
    InvalidStateTransition,
    PasswordPolicyViolation,
    RateLimited,
    SessionExpired,
    SessionInvalid,
)
from app.modules.identity.models import UserAccount

log = logging.getLogger("app.identity.auth")

S_ACCT_IP = "login_acct_ip"
S_IP = "login_ip"
S_ACCT = "login_acct"


def normalize_identifier(email: str) -> str:
    return email.strip().lower()


@dataclass
class LoginResult:
    user: UserAccount
    session: UserSession
    access_token: str
    refresh_token: str


def _clock() -> datetime:
    return now_utc()


def revoke_session(db: Session, session: UserSession, reason: str, now: datetime | None = None) -> bool:
    if not session.is_active:
        return False
    session.is_active = False
    session.revoked_at = now or _clock()
    session.revoked_reason = reason
    return True


def revoke_user_sessions(
    db: Session, user_id: int, reason: str, *, except_session_id: int | None = None, now: datetime | None = None
) -> int:
    """Revoke every active session of a user; returns how many were revoked."""
    stmt = (
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.is_active.is_(True))
        .values(is_active=False, revoked_at=now or _clock(), revoked_reason=reason)
    )
    if except_session_id is not None:
        stmt = stmt.where(UserSession.id != except_session_id)
    return db.execute(stmt).rowcount or 0


def _issue_session(db: Session, user: UserAccount, device_name: str | None, client_ip: str | None, now: datetime):
    session = UserSession(
        user_id=user.id,
        device_name=device_name,
        created_ip=client_ip,
        last_seen_at=now,
        expires_at=now + timedelta(days=settings.refresh_token_expire_days),
    )
    db.add(session)
    db.flush()
    access = create_access_token(user.id, session.id)
    refresh = create_refresh_token(user.id, session.id)
    session.refresh_token_hash = hash_token(refresh)
    return session, access, refresh


def login(
    db: Session,
    *,
    email: str,
    password: str,
    device_name: str | None,
    client_ip: str | None,
    now: datetime | None = None,
) -> LoginResult:
    now = now or _clock()
    ident = normalize_identifier(email)
    ip = client_ip or "unknown"
    acct_ip_key = f"{ident}|{ip}"

    wait = max(throttle.retry_after(db, S_ACCT_IP, acct_ip_key, now), throttle.retry_after(db, S_IP, ip, now))
    if wait:
        # Not audited per attempt: a flood would otherwise grow the audit table without bound.
        log.warning("login_throttled", extra={"retry_after": wait})
        raise RateLimited(wait)

    user = db.scalar(select(UserAccount).where(UserAccount.email == ident))
    if user is None:
        verify_and_update(password, dummy_hash())  # equalise timing with the existing-account path
        valid, new_hash = False, None
    else:
        valid, new_hash = verify_and_update(password, user.password_hash)

    if not valid:
        throttle.register_failure(db, S_IP, ip, settings.auth_max_failures_ip, now)
        throttle.register_failure(db, S_ACCT_IP, acct_ip_key, settings.auth_max_failures_account_ip, now)
        failures = throttle.register_failure(db, S_ACCT, ident, settings.auth_account_lock_failures, now)
        if user is not None and user.status == "active" and failures >= settings.auth_account_lock_failures:
            user.status = "locked"
            user.locked_at = now
            user.locked_until = now + timedelta(seconds=settings.auth_account_lock_seconds)
            revoke_user_sessions(db, user.id, "account_locked", now=now)
            record_event(
                db,
                "account.locked",
                outcome="success",
                tenant_id=user.company_id,
                subject_id=user.id,
                client_ip=client_ip,
                details={"reason": "repeated_failures"},
            )
        record_event(
            db,
            "auth.login.failed",
            outcome="failure",
            tenant_id=user.company_id if user else None,
            subject_id=user.id if user else None,
            client_ip=client_ip,
            details={"reason": "invalid_credentials"},
        )
        db.commit()
        raise InvalidCredentials()

    # Credentials are valid from here on: status-specific answers no longer leak existence.
    if user.status == "locked" and user.locked_until is not None and user.locked_until <= now:
        user.status, user.locked_at, user.locked_until = "active", None, None
        record_event(
            db, "account.unlocked", tenant_id=user.company_id, subject_id=user.id, details={"reason": "lock_expired"}
        )
    if user.status == "disabled":
        record_event(
            db,
            "auth.login.denied",
            outcome="denied",
            tenant_id=user.company_id,
            subject_id=user.id,
            client_ip=client_ip,
            details={"reason": "disabled"},
        )
        db.commit()
        raise AccountDisabled()
    if user.status == "locked":
        record_event(
            db,
            "auth.login.denied",
            outcome="denied",
            tenant_id=user.company_id,
            subject_id=user.id,
            client_ip=client_ip,
            details={"reason": "locked"},
        )
        db.commit()
        raise AccountLocked()
    if user.status != "active":  # pending: the activation flow must set the password first
        db.commit()
        raise InvalidCredentials()

    throttle.reset(db, S_ACCT_IP, acct_ip_key)
    throttle.reset(db, S_ACCT, ident)
    if new_hash:
        user.password_hash = new_hash
    user.last_login_at = now
    session, access, refresh = _issue_session(db, user, device_name, client_ip, now)
    record_event(
        db,
        "auth.login.succeeded",
        tenant_id=user.company_id,
        actor_id=user.id,
        subject_id=user.id,
        client_ip=client_ip,
        details={"session_id": session.id},
    )
    db.commit()
    return LoginResult(user, session, access, refresh)


def _decode(token: str, expected_type: str) -> tuple[int, int]:
    try:
        claims = decode_token(token)
        if claims.get("type") != expected_type:
            raise SessionInvalid()
        return int(claims["sub"]), int(claims["sid"])
    except (JWTError, KeyError, TypeError, ValueError):
        raise SessionInvalid() from None


def authenticate_access_token(db: Session, token: str, now: datetime | None = None) -> tuple[UserAccount, UserSession]:
    """Validate an access token against *current* server state; never trusts claims beyond identifiers."""
    now = now or _clock()
    user_id, session_id = _decode(token, "access")
    session = db.get(UserSession, session_id)
    user = db.get(UserAccount, user_id)
    if session is None or user is None or session.user_id != user.id or not session.is_active:
        raise SessionInvalid()
    if session.expires_at is not None and session.expires_at <= now:
        raise SessionExpired()
    if user.status != "active":
        raise SessionInvalid()
    if get_context() is not None:  # HTTP requests only (WebSocket/scripts have no request context)
        bind_context(
            actor_id=str(user.id),
            tenant_id=str(user.company_id) if user.company_id is not None else None,
            branch_id=str(user.branch_id) if user.branch_id is not None else None,
        )
    return user, session


def refresh(db: Session, *, refresh_token: str, client_ip: str | None, now: datetime | None = None) -> LoginResult:
    now = now or _clock()
    user_id, session_id = _decode(refresh_token, "refresh")
    session = db.get(UserSession, session_id, with_for_update=True)
    user = db.get(UserAccount, user_id)
    if session is None or user is None or session.user_id != user.id or not session.is_active:
        raise SessionInvalid()
    if not verify_token_hash(refresh_token, session.refresh_token_hash):
        # A validly signed but superseded refresh token = replay of a stolen token: kill the session.
        revoke_session(db, session, "refresh_reuse_detected", now)
        record_event(
            db,
            "session.refresh_reuse_detected",
            outcome="denied",
            tenant_id=user.company_id,
            subject_id=user.id,
            client_ip=client_ip,
            details={"session_id": session.id},
        )
        db.commit()
        raise SessionInvalid()
    if (session.expires_at is not None and session.expires_at <= now) or user.status != "active":
        raise SessionExpired() if user.status == "active" else SessionInvalid()
    access = create_access_token(user.id, session.id)
    new_refresh = create_refresh_token(user.id, session.id)
    session.refresh_token_hash = hash_token(new_refresh)
    session.last_seen_at = now
    db.commit()
    return LoginResult(user, session, access, new_refresh)


def logout(db: Session, user: UserAccount, session: UserSession, client_ip: str | None) -> None:
    if revoke_session(db, session, "logout"):
        record_event(
            db,
            "auth.logout",
            tenant_id=user.company_id,
            actor_id=user.id,
            subject_id=user.id,
            client_ip=client_ip,
            details={"session_id": session.id},
        )
    db.commit()


def change_password(
    db: Session,
    user: UserAccount,
    session: UserSession,
    *,
    current_password: str,
    new_password: str,
    client_ip: str | None,
) -> int:
    """Authenticated change. Other sessions are revoked; returns how many."""
    valid, _ = verify_and_update(current_password, user.password_hash)
    if not valid:
        record_event(
            db,
            "password.change_failed",
            outcome="failure",
            tenant_id=user.company_id,
            actor_id=user.id,
            subject_id=user.id,
            client_ip=client_ip,
            details={"reason": "wrong_current_password"},
        )
        db.commit()
        raise InvalidCredentials()
    reason = password_policy_violation(new_password)
    if reason:
        raise PasswordPolicyViolation(reason)
    set_password(user, new_password)
    revoked = revoke_user_sessions(db, user.id, "password_changed", except_session_id=session.id)
    record_event(
        db,
        "password.changed",
        tenant_id=user.company_id,
        actor_id=user.id,
        subject_id=user.id,
        client_ip=client_ip,
        details={"sessions_revoked": revoked},
    )
    db.commit()
    return revoked


def set_password(user: UserAccount, new_password: str) -> None:
    user.password_hash = get_password_hash(new_password)
    user.password_changed_at = _clock()


def require_state(user: UserAccount, allowed: tuple[str, ...]) -> None:
    if user.status not in allowed:
        raise InvalidStateTransition()
