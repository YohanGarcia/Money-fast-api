"""Administrative use cases: users, roles, assignments, session revocation.

Every function receives the already-authenticated ``Principal``. The tenant is *always* the principal's;
client-supplied tenant ids are not accepted anywhere. Other tenants' ids behave exactly like missing ones.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import Conflict, ResourceNotFound
from app.core.security import get_password_hash, new_opaque_token
from app.core.time import now_utc
from app.models.branch import Branch
from app.modules.identity.audit import record_event
from app.modules.identity.auth import normalize_identifier, require_state, revoke_user_sessions
from app.modules.identity.authorization import Principal, assert_within_ceiling, effective_grants, require
from app.modules.identity.catalog import sync_permission_catalog
from app.modules.identity.errors import (
    DuplicateAssignment,
    InvalidStateTransition,
    SelfEscalationDenied,
    TenantMismatch,
)
from app.modules.identity.models import (
    Permission,
    Person,
    Role,
    RolePermission,
    SecurityEvent,
    UserAccount,
    UserRoleAssignment,
)
from app.modules.identity.recovery import PendingDelivery, issue_token


def _target_user(db: Session, actor: Principal, user_id: int) -> UserAccount:
    user = db.get(UserAccount, user_id)
    if user is None or user.company_id != actor.tenant_id:
        raise TenantMismatch()  # indistinguishable from "not found"
    return user


def _role_permission_codes(role: Role) -> set[str]:
    return {p.code for p in role.permissions}


def _within_actor_authority(db: Session, actor: Principal, target: UserAccount) -> None:
    """The actor may not act on a user holding permissions the actor does not hold (conservative)."""
    held = {g.permission for g in effective_grants(db, target)}
    assert_within_ceiling(actor, held)


# --- users ---------------------------------------------------------------------------------------
def list_users(db: Session, actor: Principal, limit: int = 100, offset: int = 0) -> list[UserAccount]:
    require(actor, "users.read", tenant_id=actor.tenant_id)
    return list(
        db.scalars(
            select(UserAccount)
            .where(UserAccount.company_id == actor.tenant_id)
            .order_by(UserAccount.id)
            .limit(min(limit, 200))
            .offset(offset)
        )
    )


def get_user(db: Session, actor: Principal, user_id: int) -> UserAccount:
    require(actor, "users.read", tenant_id=actor.tenant_id)
    return _target_user(db, actor, user_id)


def create_user(
    db: Session,
    actor: Principal,
    *,
    email: str,
    given_names: str,
    family_names: str,
    branch_id: int | None,
    client_ip: str | None,
) -> tuple[UserAccount, PendingDelivery]:
    """Identity + pending account + activation invitation. No privileges are granted (separate step)."""
    require(actor, "users.create", tenant_id=actor.tenant_id)
    if actor.tenant_id is None:
        raise TenantMismatch()
    ident = normalize_identifier(email)
    if branch_id is not None:
        branch = db.get(Branch, branch_id)
        if branch is None or branch.company_id != actor.tenant_id:
            raise TenantMismatch()
    if db.scalar(select(UserAccount.id).where(UserAccount.email == ident, UserAccount.company_id == actor.tenant_id)):
        raise Conflict("Ya existe un usuario con ese correo.")
    now = now_utc()
    person = Person(tenant_id=actor.tenant_id, given_names=given_names.strip(), family_names=family_names.strip())
    db.add(person)
    db.flush()
    user = UserAccount(
        person_id=person.id,
        full_name=person.full_name,
        email=ident,
        # Unknown, unrecoverable placeholder: the person chooses the real password via the activation token.
        password_hash=get_password_hash(new_opaque_token()),
        role=None,  # no legacy privileges
        status="pending",
        company_id=actor.tenant_id,
        branch_id=branch_id,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise Conflict("Ya existe un usuario con ese correo.") from None
    secret, expires_at = issue_token(db, user, "activation", now, created_by=actor.user_id)
    record_event(
        db,
        "user.created",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        subject_id=user.id,
        client_ip=client_ip,
        details={"person_id": person.id},
    )
    record_event(
        db,
        "user.invited",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        subject_id=user.id,
        client_ip=client_ip,
        details={"purpose": "activation"},
    )
    db.commit()
    return user, PendingDelivery(user.id, user.company_id, user.email, "activation", expires_at, secret)


def disable_user(db: Session, actor: Principal, user_id: int, client_ip: str | None) -> UserAccount:
    require(actor, "users.disable", tenant_id=actor.tenant_id)
    user = _target_user(db, actor, user_id)
    if user.id == actor.user_id:
        raise SelfEscalationDenied("No puedes desactivar tu propia cuenta.")
    require_state(user, ("pending", "active", "locked"))
    _within_actor_authority(db, actor, user)
    now = now_utc()
    user.status = "disabled"
    user.disabled_at = now
    revoked = revoke_user_sessions(db, user.id, "user_disabled", now=now)
    record_event(
        db,
        "user.disabled",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        subject_id=user.id,
        client_ip=client_ip,
        details={"sessions_revoked": revoked},
    )
    db.commit()
    return user


def enable_user(db: Session, actor: Principal, user_id: int, client_ip: str | None) -> UserAccount:
    require(actor, "users.enable", tenant_id=actor.tenant_id)
    user = _target_user(db, actor, user_id)
    require_state(user, ("disabled", "locked"))
    # A never-activated account goes back to pending (it still has no usable password).
    user.status = "active" if user.activated_at is not None else "pending"
    user.disabled_at = user.locked_at = user.locked_until = None
    record_event(
        db,
        "user.enabled",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        subject_id=user.id,
        client_ip=client_ip,
        details={"new_status": user.status},
    )
    db.commit()
    return user


def revoke_sessions(db: Session, actor: Principal, user_id: int, client_ip: str | None) -> int:
    require(actor, "security.sessions.revoke", tenant_id=actor.tenant_id)
    user = _target_user(db, actor, user_id)
    revoked = revoke_user_sessions(db, user.id, "revoked_by_admin")
    record_event(
        db,
        "session.revoked",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        subject_id=user.id,
        client_ip=client_ip,
        details={"count": revoked, "scope": "all_sessions"},
    )
    db.commit()
    return revoked


# --- roles & permissions ---------------------------------------------------------------------------
def list_permissions(db: Session, actor: Principal) -> list[Permission]:
    require(actor, "permissions.read", tenant_id=actor.tenant_id)
    sync_permission_catalog(db)
    kind = "platform" if actor.is_platform else "tenant"
    return list(db.scalars(select(Permission).where(Permission.scope_kind == kind).order_by(Permission.code)))


def list_roles(db: Session, actor: Principal) -> list[Role]:
    require(actor, "roles.read", tenant_id=actor.tenant_id)
    return list(db.scalars(select(Role).where(Role.tenant_id == actor.tenant_id).order_by(Role.id)))


def create_role(
    db: Session, actor: Principal, *, name: str, description: str, permission_codes: list[str], client_ip: str | None
) -> Role:
    require(actor, "roles.create", tenant_id=actor.tenant_id)
    if actor.tenant_id is None:
        raise TenantMismatch()
    codes = set(permission_codes)
    catalog = {p.code: p for p in db.scalars(select(Permission).where(Permission.code.in_(codes)))} if codes else {}
    unknown = codes - set(catalog)
    if unknown or any(p.scope_kind != "tenant" for p in catalog.values()):
        raise ResourceNotFound("Permiso no encontrado.")
    assert_within_ceiling(actor, codes)  # cannot create a role richer than oneself
    role = Role(tenant_id=actor.tenant_id, name=name.strip(), description=description.strip())
    db.add(role)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise Conflict("Ya existe un rol con ese nombre.") from None
    for p in catalog.values():
        db.add(RolePermission(role_id=role.id, permission_id=p.id))
    record_event(
        db,
        "role.created",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={"role_id": role.id, "permissions": sorted(codes)},
    )
    db.commit()
    db.refresh(role)
    return role


def _role_of_tenant(db: Session, actor: Principal, role_id: int) -> Role:
    role = db.get(Role, role_id)
    if role is None or role.tenant_id != actor.tenant_id or role.status != "active":
        raise TenantMismatch()
    return role


def assign_role(
    db: Session,
    actor: Principal,
    user_id: int,
    role_id: int,
    *,
    scope_kind: str,
    branch_id: int | None,
    cash_point_id: int | None = None,
    client_ip: str | None,
) -> UserRoleAssignment:
    require(actor, "roles.assign", tenant_id=actor.tenant_id)
    target = _target_user(db, actor, user_id)
    role = _role_of_tenant(db, actor, role_id)
    if target.id == actor.user_id:
        raise SelfEscalationDenied()
    if target.status == "disabled":
        raise InvalidStateTransition("El usuario esta desactivado.")
    if scope_kind == "branch":
        branch = db.get(Branch, branch_id) if branch_id is not None else None
        if branch is None or branch.company_id != actor.tenant_id:
            raise TenantMismatch()
    else:
        branch_id = None
    cp_branch_id = None
    if scope_kind == "cash_point":
        from app.modules.organization.models import CashPoint

        cash_point = db.get(CashPoint, cash_point_id) if cash_point_id is not None else None
        if cash_point is None or cash_point.tenant_id != actor.tenant_id:
            raise TenantMismatch()
        cp_branch_id = cash_point.branch_id
    else:
        cash_point_id = None
    assert_within_ceiling(
        actor,
        _role_permission_codes(role),
        scope_kind=scope_kind,
        branch_id=branch_id,
        cash_point_id=cash_point_id,
        cash_point_branch_id=cp_branch_id,
    )
    row = UserRoleAssignment(
        tenant_id=actor.tenant_id,
        user_id=target.id,
        role_id=role.id,
        scope_kind=scope_kind,
        branch_id=branch_id,
        cash_point_id=cash_point_id,
        assigned_by=actor.user_id,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise DuplicateAssignment() from None
    record_event(
        db,
        "role.assigned",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        subject_id=target.id,
        client_ip=client_ip,
        details={"role_id": role.id, "scope": scope_kind, "branch_id": branch_id, "cash_point_id": cash_point_id},
    )
    db.commit()
    return row


def remove_role(db: Session, actor: Principal, user_id: int, role_id: int, client_ip: str | None) -> int:
    require(actor, "roles.assign", tenant_id=actor.tenant_id)
    target = _target_user(db, actor, user_id)
    role = db.get(Role, role_id)
    if role is None or role.tenant_id != actor.tenant_id:
        raise TenantMismatch()
    assert_within_ceiling(actor, _role_permission_codes(role))  # cannot strip a role richer than oneself
    now: datetime = now_utc()
    rows = db.scalars(
        select(UserRoleAssignment).where(
            UserRoleAssignment.user_id == target.id,
            UserRoleAssignment.role_id == role.id,
            UserRoleAssignment.revoked_at.is_(None),
        )
    ).all()
    if not rows:
        raise ResourceNotFound("El usuario no tiene ese rol.")
    for r in rows:
        r.revoked_at, r.revoked_by = now, actor.user_id  # history kept; effective immediately
    record_event(
        db,
        "role.removed",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        subject_id=target.id,
        client_ip=client_ip,
        details={"role_id": role.id, "assignments": len(rows)},
    )
    db.commit()
    return len(rows)


def list_events(db: Session, actor: Principal, limit: int = 100) -> list[SecurityEvent]:
    require(actor, "security.events.read", tenant_id=actor.tenant_id)
    return list(
        db.scalars(
            select(SecurityEvent)
            .where(SecurityEvent.tenant_id == actor.tenant_id)
            .order_by(SecurityEvent.id.desc())
            .limit(min(limit, 200))
        )
    )
