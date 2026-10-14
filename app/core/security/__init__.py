"""Backend security primitives (centralised in T-002).

Legacy import paths (``from app.core.security import get_password_hash, ...``) keep working.
"""

from app.core.security.passwords import (
    dummy_hash,
    get_password_hash,
    password_hasher,
    password_policy_violation,
    verify_and_update,
    verify_password,
)
from app.core.security.tokens import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_token,
    keyed_digest,
    new_opaque_token,
    verify_token_hash,
)

__all__ = [
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "dummy_hash",
    "get_password_hash",
    "hash_token",
    "keyed_digest",
    "new_opaque_token",
    "password_hasher",
    "password_policy_violation",
    "verify_and_update",
    "verify_password",
    "verify_token_hash",
]
