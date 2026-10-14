"""Compatibility alias: the user model now lives in the identity module (T-002)."""

from app.modules.identity.models import UserAccount as User
from app.modules.identity.models import UserRole

__all__ = ["User", "UserRole"]
