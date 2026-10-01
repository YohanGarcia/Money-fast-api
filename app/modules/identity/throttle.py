"""PostgreSQL-backed exponential backoff (safe across workers; no external cache needed).

* Counters are keyed by an HMAC of the identifier, so existence of an account never changes the path.
* Delay doubles after each failure beyond the threshold and is capped: blocking is always temporary,
  so an attacker cannot lock a victim out permanently.
* Attempts made while blocked are NOT counted, so an attacker cannot extend a block indefinitely.
"""

import math
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import keyed_digest
from app.modules.identity.models import AuthThrottle


def _locked_row(db: Session, scope: str, key: str) -> AuthThrottle:
    key_hash = keyed_digest(scope, key)
    db.execute(
        pg_insert(AuthThrottle)
        .values(scope=scope, key_hash=key_hash, failures=0)
        .on_conflict_do_nothing(index_elements=["scope", "key_hash"])
    )
    return db.execute(
        select(AuthThrottle).where(AuthThrottle.scope == scope, AuthThrottle.key_hash == key_hash).with_for_update()
    ).scalar_one()


def retry_after(db: Session, scope: str, key: str, now: datetime) -> int:
    """Seconds until the counter allows another attempt (0 = allowed)."""
    row = db.execute(
        select(AuthThrottle).where(AuthThrottle.scope == scope, AuthThrottle.key_hash == keyed_digest(scope, key))
    ).scalar_one_or_none()
    if row is None or row.blocked_until is None or row.blocked_until <= now:
        return 0
    return math.ceil((row.blocked_until - now).total_seconds())


def register_failure(db: Session, scope: str, key: str, threshold: int, now: datetime) -> int:
    """Count one failure; returns the new failure count (the caller commits)."""
    row = _locked_row(db, scope, key)
    if row.last_failure_at and (now - row.last_failure_at).total_seconds() > settings.auth_throttle_window_seconds:
        row.failures = 0
        row.blocked_until = None
    row.failures += 1
    row.last_failure_at = now
    if row.failures >= threshold:
        delay = min(
            settings.auth_throttle_base_seconds * 2 ** (row.failures - threshold), settings.auth_throttle_max_seconds
        )
        row.blocked_until = now + timedelta(seconds=delay)
    return row.failures


def reset(db: Session, scope: str, key: str) -> None:
    row = db.execute(
        select(AuthThrottle).where(AuthThrottle.scope == scope, AuthThrottle.key_hash == keyed_digest(scope, key))
    ).scalar_one_or_none()
    if row is not None:
        row.failures = 0
        row.blocked_until = None
        row.last_failure_at = None
