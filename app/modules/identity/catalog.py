"""Code-defined permission catalogue (minimal security set for T-002) and tenant bootstrap.

Permissions for Credit/Cash/etc. are added by their own packages; none are invented here.
Platform and tenant administration are separate capabilities (DR-008): a permission has exactly one
``scope_kind`` and can only be held through a role of the same kind.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.identity.models import Permission, Person, Role, RolePermission, UserAccount, UserRoleAssignment


@dataclass(frozen=True)
class PermissionDef:
    code: str
    scope_kind: str  # "tenant" | "platform"
    description: str
    sensitive: bool = False


CATALOG: tuple[PermissionDef, ...] = (
    PermissionDef("users.read", "tenant", "Consultar usuarios de la agencia"),
    PermissionDef("users.create", "tenant", "Crear usuarios (invitacion de activacion)", True),
    PermissionDef("users.disable", "tenant", "Desactivar usuarios", True),
    PermissionDef("users.enable", "tenant", "Reactivar usuarios", True),
    PermissionDef("roles.read", "tenant", "Consultar roles"),
    PermissionDef("roles.create", "tenant", "Crear roles", True),
    PermissionDef("roles.assign", "tenant", "Asignar y quitar roles a usuarios", True),
    PermissionDef("permissions.read", "tenant", "Consultar el catalogo de permisos"),
    PermissionDef("security.sessions.revoke", "tenant", "Revocar sesiones de un usuario", True),
    PermissionDef("security.events.read", "tenant", "Consultar eventos de seguridad"),
    PermissionDef("platform.users.read", "platform", "Consultar usuarios de plataforma"),
    PermissionDef("platform.users.disable", "platform", "Desactivar usuarios de plataforma", True),
)
CATALOG_CODES = frozenset(p.code for p in CATALOG)
TENANT_ADMIN_ROLE = "Administrador de agencia"
PLATFORM_ADMIN_ROLE = "Administrador de plataforma"


def sync_permission_catalog(db: Session) -> dict[str, Permission]:
    """Idempotently insert missing catalogue rows (never deletes or edits existing ones)."""
    existing = {p.code: p for p in db.scalars(select(Permission))}
    for d in CATALOG:
        if d.code not in existing:
            row = Permission(code=d.code, scope_kind=d.scope_kind, description=d.description, is_sensitive=d.sensitive)
            db.add(row)
            existing[d.code] = row
    db.flush()
    return existing


def ensure_system_role(db: Session, tenant_id: int | None) -> Role:
    """Tenant admin role (all tenant permissions) or platform admin role, created once."""
    name = TENANT_ADMIN_ROLE if tenant_id is not None else PLATFORM_ADMIN_ROLE
    kind = "tenant" if tenant_id is not None else "platform"
    role = db.scalar(
        select(Role).where(Role.tenant_id == tenant_id, Role.name == name)
        if tenant_id is not None
        else select(Role).where(Role.tenant_id.is_(None), Role.name == name)
    )
    if role is not None:
        return role
    perms = sync_permission_catalog(db)
    role = Role(tenant_id=tenant_id, name=name, description="Rol de sistema", system_defined=True)
    db.add(role)
    db.flush()
    for d in CATALOG:
        if d.scope_kind == kind:
            db.add(RolePermission(role_id=role.id, permission_id=perms[d.code].id))
    db.flush()
    return role


def bootstrap_owner(db: Session, user: UserAccount) -> None:
    """Give a newly created tenant owner a Person and the tenant admin role (legacy registration path)."""
    if user.person_id is None:
        given, _, family = user.full_name.strip().partition(" ")
        person = Person(tenant_id=user.company_id, given_names=given or user.full_name, family_names=family)
        db.add(person)
        db.flush()
        user.person_id = person.id
    role = ensure_system_role(db, user.company_id)
    db.add(UserRoleAssignment(tenant_id=user.company_id, user_id=user.id, role_id=role.id, scope_kind="tenant"))
    db.flush()


def ensure_person(db: Session, user: UserAccount) -> None:
    """Link a legacy-created user to a Person (no privileges are granted)."""
    if user.person_id is None:
        given, _, family = user.full_name.strip().partition(" ")
        person = Person(tenant_id=user.company_id, given_names=given or user.full_name, family_names=family)
        db.add(person)
        db.flush()
        user.person_id = person.id
