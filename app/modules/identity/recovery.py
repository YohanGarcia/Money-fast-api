"""Account recovery and activation.

* tokens are 256-bit random, stored only as SHA-256, single-use (atomic UPDATE), expiring and revocable;
* requesting always yields the same outward result, whether or not the account exists;
* requesting again revokes earlier unused tokens;
* completing revokes every session of the user (sensitive event).
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import session_scope
from app.core.security import hash_token, new_opaque_token, password_policy_violation
from app.core.time import now_utc
from app.modules.identity import throttle
from app.modules.identity.audit import record_event
from app.modules.identity.auth import find_unambiguous_account, normalize_identifier, revoke_user_sessions, set_password
from app.modules.identity.errors import (
    InvalidRecoveryToken,
    PasswordPolicyViolation,
    RateLimited,
    RecoveryTokenReused,
)
from app.modules.identity.models import RecoveryToken, UserAccount
from app.modules.identity.notifications import SecretNotifier

log = logging.getLogger("app.identity.recovery")
S_REQ_ACCT = "recovery_req_acct"
S_REQ_IP = "recovery_req_ip"
S_DONE_IP = "recovery_done_ip"


def issue_token(
    db: Session, user: UserAccount, purpose: str, now: datetime, created_by: int | None = None
) -> tuple[str, datetime]:
    """Create a fresh token (revoking earlier unused ones). Returns (secret, expires_at); the secret
    exists only in memory and must go straight to a notifier."""
    db.execute(
        update(RecoveryToken)
        .where(RecoveryToken.user_id == user.id, RecoveryToken.used_at.is_(None), RecoveryToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    ttl = (
        timedelta(hours=settings.activation_token_ttl_hours)
        if purpose == "activation"
        else timedelta(minutes=settings.recovery_token_ttl_minutes)
    )
    secret = new_opaque_token()
    db.add(
        RecoveryToken(
            user_id=user.id, token_hash=hash_token(secret), purpose=purpose, expires_at=now + ttl, created_by=created_by
        )
    )
    db.flush()
    return secret, now + ttl


@dataclass(frozen=True)
class PendingDelivery:
    """A secret waiting to be handed to a notifier. ``repr`` hides it so it cannot reach a log."""

    user_id: int
    tenant_id: int | None
    email: str
    purpose: str
    expires_at: datetime
    secret: str = field(repr=False)


def deliver(delivery: PendingDelivery, notifier: SecretNotifier) -> None:
    """Send after the HTTP response (constant response time for known/unknown accounts)."""
    try:
        ok = notifier.send(
            email=delivery.email, secret=delivery.secret, purpose=delivery.purpose, expires_at=delivery.expires_at
        )
    except Exception:  # noqa: BLE001 - a delivery fault must not surface to the caller
        ok = False
    if not ok:
        with session_scope() as db:
            record_event(
                db,
                "recovery.delivery_failed",
                outcome="failure",
                tenant_id=delivery.tenant_id,
                subject_id=delivery.user_id,
                details={"purpose": delivery.purpose},
            )


def request_recovery(
    db: Session, *, email: str, client_ip: str | None, now: datetime | None = None
) -> PendingDelivery | None:
    now = now or now_utc()
    ident = normalize_identifier(email)
    ip = client_ip or "unknown"
    wait = throttle.retry_after(db, S_REQ_IP, ip, now)
    if wait:
        raise RateLimited(wait)
    throttle.register_failure(db, S_REQ_IP, ip, settings.recovery_max_requests_ip, now)
    # Per-account limit is silent: the caller still gets the same answer, but no token is issued.
    account_blocked = throttle.retry_after(db, S_REQ_ACCT, ident, now) > 0
    if not account_blocked:
        throttle.register_failure(db, S_REQ_ACCT, ident, settings.recovery_max_requests_account, now)

    user = find_unambiguous_account(db, ident)  # ambiguous across tenants => nothing is issued (fail closed)
    if user is None or user.status == "disabled" or account_blocked:
        record_event(
            db,
            "recovery.requested",
            client_ip=client_ip,
            tenant_id=user.company_id if user else None,
            subject_id=user.id if user else None,
            details={"issued": False},
        )
        db.commit()
        return None
    purpose = "activation" if user.status == "pending" else "recovery"
    secret, expires_at = issue_token(db, user, purpose, now)
    record_event(
        db,
        "recovery.requested",
        tenant_id=user.company_id,
        subject_id=user.id,
        client_ip=client_ip,
        details={"issued": True, "purpose": purpose},
    )
    db.commit()
    return PendingDelivery(user.id, user.company_id, user.email, purpose, expires_at, secret)


def complete_recovery(
    db: Session, *, token: str, new_password: str, client_ip: str | None, now: datetime | None = None
) -> UserAccount:
    now = now or now_utc()
    ip = client_ip or "unknown"
    wait = throttle.retry_after(db, S_DONE_IP, ip, now)
    if wait:
        raise RateLimited(wait)
    violation = password_policy_violation(new_password)
    if violation:
        raise PasswordPolicyViolation(violation)

    token_hash = hash_token(token)
    # Atomic single use: only one concurrent request can flip used_at.
    row = db.execute(
        update(RecoveryToken)
        .where(
            RecoveryToken.token_hash == token_hash,
            RecoveryToken.used_at.is_(None),
            RecoveryToken.revoked_at.is_(None),
            RecoveryToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(RecoveryToken.user_id, RecoveryToken.purpose)
    ).first()
    user = db.get(UserAccount, row.user_id, with_for_update=True) if row else None
    if row is None or user is None or user.status == "disabled":
        reused = db.scalar(select(RecoveryToken.used_at).where(RecoveryToken.token_hash == token_hash))
        throttle.register_failure(db, S_DONE_IP, ip, settings.recovery_max_failures_ip, now)
        record_event(
            db, "recovery.failed", outcome="failure", client_ip=client_ip, details={"reason": "token_rejected"}
        )
        db.commit()
        raise RecoveryTokenReused() if reused is not None else InvalidRecoveryToken()

    set_password(user, new_password)
    if user.status in ("pending", "locked"):
        user.status = "active"
        user.activated_at = user.activated_at or now
        user.locked_at = user.locked_until = None
    revoked = revoke_user_sessions(db, user.id, "password_recovery", now=now)
    throttle.reset(db, "login_acct", normalize_identifier(user.email))
    record_event(
        db,
        "recovery.completed",
        tenant_id=user.company_id,
        subject_id=user.id,
        client_ip=client_ip,
        details={"purpose": row.purpose, "sessions_revoked": revoked},
    )
    record_event(
        db,
        "password.changed",
        tenant_id=user.company_id,
        subject_id=user.id,
        client_ip=client_ip,
        details={"via": "recovery"},
    )
    db.commit()
    return user
