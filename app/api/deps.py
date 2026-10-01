from collections.abc import Generator
from typing import Callable

from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.modules.identity.auth import authenticate_access_token
from app.models.user import User


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")  # LEGACY path; v2 uses /api/v2/auth/login


def get_db() -> Generator[Session, None, None]:
    yield from get_session()


def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    # One authentication path for legacy and v2: validates the session against current server state
    # and binds actor/tenant into the request context (never from client headers).
    user, _ = authenticate_access_token(db, token)

    if user.role == 'cashier':
        path = request.url.path.removeprefix('/api/v1')
        allowed = path.startswith('/cash/') or path in ('/auth/me','/auth/logout','/auth/refresh')
        allowed = allowed or (request.method == 'GET' and (path == '/customers' or path.startswith('/customers/') or path == '/loans' or path.startswith('/loans/') or path == '/payments'))
        allowed = allowed or (request.method == 'POST' and path == '/payments')
        if not allowed: raise HTTPException(403, 'Esta operación no está disponible para el cajero.')
    return user


def get_company_id(current_user: User = Depends(get_current_user)) -> int:
    """Returns the company_id of the current user. Raises 403 if superadmin (no company)."""
    if current_user.company_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta accion requiere pertenecer a una empresa.",
        )
    return current_user.company_id


def require_roles(*roles: str) -> Callable[..., User]:
    """Dependency factory that restricts access to users with one of the given roles."""
    def _check(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permiso para realizar esta accion.",
            )
        return current_user
    return _check


# Shorthand dependencies
require_superadmin    = require_roles("superadmin")
require_admin         = require_roles("admin")
require_admin_manager = require_roles("admin", "manager")
