from datetime import UTC, datetime, timedelta
import hashlib
import secrets

from jose import jwt
from pwdlib import PasswordHash

from app.core.config import settings

password_hasher = PasswordHash.recommended()


def get_password_hash(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_hasher.verify(password, password_hash)


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
    return _build_token(
        subject=subject,
        session_id=session_id,
        expires_delta=timedelta(minutes=settings.access_token_expire_minutes),
        token_type="access",
    )


def create_refresh_token(subject: int, session_id: int) -> str:
    return _build_token(
        subject=subject,
        session_id=session_id,
        expires_delta=timedelta(days=settings.refresh_token_expire_days),
        token_type="refresh",
    )


def create_password_reset_token(subject: int, reset_code_id: int) -> str:
    return _build_token(
        subject=subject,
        session_id=reset_code_id,
        expires_delta=timedelta(minutes=15),
        token_type="password_reset",
    )


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token_hash(token: str, token_hash: str | None) -> bool:
    if token_hash is None:
        return False
    return hash_token(token) == token_hash
