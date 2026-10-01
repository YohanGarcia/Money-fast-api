"""Identity & authorization HTTP API (v2). Thin: validation in, use case call, serialisation out."""

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_session
from app.modules.identity import admin, auth, recovery
from app.modules.identity.authorization import Principal
from app.modules.identity.deps import AuthContext, client_ip, get_auth_context, get_principal, require_permission
from app.modules.identity.models import Person
from app.modules.identity.notifications import SecretNotifier, get_notifier
from app.modules.identity.schemas import (
    AssignmentOut,
    EventOut,
    GrantOut,
    LoginIn,
    MeOut,
    PasswordChangeIn,
    PermissionOut,
    PersonOut,
    RecoveryCompleteIn,
    RecoveryRequestIn,
    RefreshIn,
    RoleAssignIn,
    RoleCreateIn,
    RoleOut,
    TokenOut,
    UserCreateIn,
    UserOut,
)

router = APIRouter(prefix="/api/v2", tags=["identity"])
GENERIC_RECOVERY_MESSAGE = "Si la cuenta existe, enviaremos instrucciones para recuperar el acceso."


def _token_out(result: auth.LoginResult) -> TokenOut:
    return TokenOut(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserOut.model_validate(result.user),
    )


def _role_out(role) -> RoleOut:
    return RoleOut(
        id=role.id,
        name=role.name,
        description=role.description,
        status=role.status,
        system_defined=role.system_defined,
        permissions=sorted(p.code for p in role.permissions),
    )


# --- authentication ---------------------------------------------------------------------------------
@router.post("/auth/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, db: Session = Depends(get_session)) -> TokenOut:
    result = auth.login(
        db, email=body.email, password=body.password, device_name=body.device_name, client_ip=client_ip(request)
    )
    return _token_out(result)


@router.post("/auth/refresh", response_model=TokenOut)
def refresh(body: RefreshIn, request: Request, db: Session = Depends(get_session)) -> TokenOut:
    return _token_out(auth.refresh(db, refresh_token=body.refresh_token, client_ip=client_ip(request)))


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request, ctx: AuthContext = Depends(get_auth_context), db: Session = Depends(get_session)
) -> Response:
    auth.logout(db, ctx.user, ctx.session, client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/auth/me", response_model=MeOut)
def me(ctx: AuthContext = Depends(get_auth_context), db: Session = Depends(get_session)) -> MeOut:
    person = db.get(Person, ctx.user.person_id) if ctx.user.person_id else None
    return MeOut(
        user=UserOut.model_validate(ctx.user),
        person=PersonOut.model_validate(person) if person else None,
        permissions=[
            GrantOut(permission=g.permission, scope=g.scope_kind, branch_id=g.branch_id) for g in ctx.principal.grants
        ],
    )


@router.post("/auth/password/change", status_code=status.HTTP_200_OK)
def change_password(
    body: PasswordChangeIn,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_session),
) -> dict:
    revoked = auth.change_password(
        db,
        ctx.user,
        ctx.session,
        current_password=body.current_password,
        new_password=body.new_password,
        client_ip=client_ip(request),
    )
    return {"sessions_revoked": revoked}


@router.post("/auth/recovery/request", status_code=status.HTTP_202_ACCEPTED)
def recovery_request(
    body: RecoveryRequestIn,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_session),
    notifier: SecretNotifier = Depends(get_notifier),
) -> dict:
    """Always the same answer for known and unknown accounts."""
    pending = recovery.request_recovery(db, email=body.email, client_ip=client_ip(request))
    if pending is not None:
        background.add_task(recovery.deliver, pending, notifier)
    return {"message": GENERIC_RECOVERY_MESSAGE}


@router.post("/auth/recovery/complete", status_code=status.HTTP_200_OK)
def recovery_complete(body: RecoveryCompleteIn, request: Request, db: Session = Depends(get_session)) -> dict:
    recovery.complete_recovery(db, token=body.token, new_password=body.new_password, client_ip=client_ip(request))
    return {"message": "Contrasena actualizada. Inicia sesion de nuevo."}


# --- users ------------------------------------------------------------------------------------------------
@router.get("/users", response_model=list[UserOut])
def list_users(
    limit: int = 100,
    offset: int = 0,
    actor: Principal = Depends(require_permission("users.read")),
    db: Session = Depends(get_session),
) -> list:
    return admin.list_users(db, actor, limit, offset)


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserCreateIn,
    request: Request,
    background: BackgroundTasks,
    actor: Principal = Depends(require_permission("users.create")),
    db: Session = Depends(get_session),
    notifier: SecretNotifier = Depends(get_notifier),
):
    user, pending = admin.create_user(
        db,
        actor,
        email=body.email,
        given_names=body.given_names,
        family_names=body.family_names,
        branch_id=body.branch_id,
        client_ip=client_ip(request),
    )
    background.add_task(recovery.deliver, pending, notifier)
    return user


@router.get("/users/{user_id}", response_model=UserOut)
def get_user(
    user_id: int, actor: Principal = Depends(require_permission("users.read")), db: Session = Depends(get_session)
):
    return admin.get_user(db, actor, user_id)


@router.post("/users/{user_id}/disable", response_model=UserOut)
def disable_user(
    user_id: int,
    request: Request,
    actor: Principal = Depends(require_permission("users.disable")),
    db: Session = Depends(get_session),
):
    return admin.disable_user(db, actor, user_id, client_ip(request))


@router.post("/users/{user_id}/enable", response_model=UserOut)
def enable_user(
    user_id: int,
    request: Request,
    actor: Principal = Depends(require_permission("users.enable")),
    db: Session = Depends(get_session),
):
    return admin.enable_user(db, actor, user_id, client_ip(request))


@router.post("/users/{user_id}/sessions/revoke")
def revoke_sessions(
    user_id: int,
    request: Request,
    actor: Principal = Depends(require_permission("security.sessions.revoke")),
    db: Session = Depends(get_session),
) -> dict:
    return {"revoked": admin.revoke_sessions(db, actor, user_id, client_ip(request))}


@router.post("/users/{user_id}/roles", response_model=AssignmentOut, status_code=status.HTTP_201_CREATED)
def assign_role(
    user_id: int,
    body: RoleAssignIn,
    request: Request,
    actor: Principal = Depends(require_permission("roles.assign")),
    db: Session = Depends(get_session),
):
    return admin.assign_role(
        db, actor, user_id, body.role_id, scope_kind=body.scope, branch_id=body.branch_id, client_ip=client_ip(request)
    )


@router.delete("/users/{user_id}/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_role(
    user_id: int,
    role_id: int,
    request: Request,
    actor: Principal = Depends(require_permission("roles.assign")),
    db: Session = Depends(get_session),
) -> Response:
    admin.remove_role(db, actor, user_id, role_id, client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- roles, permissions, audit ------------------------------------------------------------------------
@router.get("/roles", response_model=list[RoleOut])
def list_roles(actor: Principal = Depends(require_permission("roles.read")), db: Session = Depends(get_session)):
    return [_role_out(r) for r in admin.list_roles(db, actor)]


@router.post("/roles", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
def create_role(
    body: RoleCreateIn,
    request: Request,
    actor: Principal = Depends(require_permission("roles.create")),
    db: Session = Depends(get_session),
):
    role = admin.create_role(
        db,
        actor,
        name=body.name,
        description=body.description,
        permission_codes=body.permissions,
        client_ip=client_ip(request),
    )
    return _role_out(role)


@router.get("/permissions", response_model=list[PermissionOut])
def list_permissions(
    actor: Principal = Depends(require_permission("permissions.read")), db: Session = Depends(get_session)
):
    return admin.list_permissions(db, actor)


@router.get("/security/events", response_model=list[EventOut])
def list_events(
    limit: int = 100,
    actor: Principal = Depends(require_permission("security.events.read")),
    db: Session = Depends(get_session),
):
    return admin.list_events(db, actor, limit)


__all__ = ["get_principal", "router"]
