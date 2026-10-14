"""Access/refresh JWTs and opaque one-time secrets.

JWTs carry only identifiers (user, session) and a purpose. Tenant, status and
permissions are always re-read from the database, never trusted from the token.
"""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from jose import jwt

from app.core.config import settings


def _build_token(subject: int, session_id: int, expires_delta: timedelta, token_type: str) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(subject),
        "sid": session_id,
        "type": token_type,
        "jti": secrets.token_urlsafe(16),
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def create_access_token(subject: int, session_id: int) -> str:
    return _build_token(subject, session_id, timedelta(minutes=settings.access_token_expire_minutes), "access")


def create_refresh_token(subject: int, session_id: int) -> str:
    return _build_token(subject, session_id, timedelta(days=settings.refresh_token_expire_days), "refresh")


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])


def hash_token(token: str) -> str:
    """SHA-256 of a high-entropy opaque secret (refresh/recovery tokens); never stored in clear."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token_hash(token: str, token_hash: str | None) -> bool:
    if token_hash is None:
        return False
    return hmac.compare_digest(hash_token(token), token_hash)


def new_opaque_token() -> str:
    """256-bit random single-use secret (recovery/activation)."""
    return secrets.token_urlsafe(32)


def keyed_digest(scope: str, value: str) -> str:
    """HMAC of an identifier (email/IP) so throttle rows never store the raw value."""
    return hmac.new(settings.secret_key.encode(), f"{scope}:{value}".encode(), hashlib.sha256).hexdigest()
