"""Password hashing (Argon2id via pwdlib) and password policy.

Parameters come from settings so they can be tuned per environment; hashes created
with older parameters are transparently upgraded on the next successful login.
"""

from functools import lru_cache

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from app.core.config import settings


@lru_cache(maxsize=1)
def password_hasher() -> PasswordHash:
    return PasswordHash(
        (
            Argon2Hasher(
                time_cost=settings.password_hash_time_cost,
                memory_cost=settings.password_hash_memory_kib,
                parallelism=settings.password_hash_parallelism,
            ),
        )
    )


def get_password_hash(password: str) -> str:
    return password_hasher().hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_hasher().verify(password, password_hash)


def verify_and_update(password: str, password_hash: str) -> tuple[bool, str | None]:
    """Verify; when valid and the stored hash is outdated, also return a fresh hash."""
    return password_hasher().verify_and_update(password, password_hash)


@lru_cache(maxsize=1)
def dummy_hash() -> str:
    """Hash verified when the account does not exist, to equalise login timing."""
    return get_password_hash("timing-equalisation-placeholder")


def password_policy_violation(password: str) -> str | None:
    """Return a human-readable reason when the password violates the policy, else None.

    Policy (DF-09 §9): length only; no arbitrary composition rules or forced rotation.
    """
    if len(password) < settings.password_min_length:
        return f"La contrasena debe tener al menos {settings.password_min_length} caracteres."
    if len(password) > settings.password_max_length:
        return f"La contrasena no puede superar {settings.password_max_length} caracteres."
    if not password.strip():
        return "La contrasena no puede estar vacia."
    return None
