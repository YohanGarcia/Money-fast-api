"""FastAPI dependencies: authenticated principal and reusable permission gate."""

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.models.session import UserSession
from app.modules.identity.auth import authenticate_access_token
from app.modules.identity.authorization import Principal, build_principal, require
from app.modules.identity.models import UserAccount

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v2/auth/login")


def client_ip(request: Request) -> str | None:
    """Socket peer only. Forwarded headers are client-controlled and are not trusted here; behind a proxy,
    run the server with its proxy-header support restricted to the proxy's address."""
    return request.client.host if request.client else None


@dataclass
class AuthContext:
    user: UserAccount
    session: UserSession
    principal: Principal


def get_auth_context(token: str = Depends(oauth2_scheme), db: Session = Depends(get_session)) -> AuthContext:
    user, session = authenticate_access_token(db, token)
    return AuthContext(user, session, build_principal(db, user, session.id))


def get_principal(ctx: AuthContext = Depends(get_auth_context)) -> Principal:
    return ctx.principal


def require_permission(permission: str) -> Callable[..., Principal]:
    """Endpoint-level gate: deny unless the principal holds ``permission`` for its own tenant.
    Target-specific checks (a given user/role/branch) are enforced by the use case."""

    def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        require(principal, permission, tenant_id=principal.tenant_id)
        return principal

    return _dep
