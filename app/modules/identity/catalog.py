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
    PermissionDef("tenant.settings.read", "tenant", "Consultar la configuracion de la agencia"),
    PermissionDef("tenant.settings.manage", "tenant", "Cambiar zona horaria y moneda base de la agencia", True),
    PermissionDef("organization.branches.read", "tenant", "Consultar sucursales"),
    PermissionDef("organization.branches.manage", "tenant", "Crear y administrar sucursales", True),
    PermissionDef("cash.points.read", "tenant", "Consultar cajas (puntos de caja)"),
    PermissionDef("cash.points.manage", "tenant", "Crear y administrar cajas", True),
    PermissionDef("cash.points.suspend", "tenant", "Suspender y reanudar cajas de forma explicita", True),
    PermissionDef("currencies.read", "tenant", "Consultar monedas de la agencia"),
    PermissionDef("currencies.manage", "tenant", "Habilitar y deshabilitar monedas", True),
    PermissionDef("customers.read", "tenant", "Consultar clientes (datos no sensibles)"),
    PermissionDef("customers.read_sensitive", "tenant", "Ver documento de identidad y fecha de nacimiento", True),
    PermissionDef("customers.create", "tenant", "Registrar clientes"),
    PermissionDef("customers.update", "tenant", "Editar datos basicos, contactos, direcciones y referencias"),
    PermissionDef("customers.identity.correct", "tenant", "Corregir o cambiar la identidad de un cliente", True),
    PermissionDef("customers.activate", "tenant", "Activar y desactivar clientes", True),
    PermissionDef("customers.assign_branch", "tenant", "Cambiar la sucursal de gestion de un cliente", True),
    PermissionDef("customers.duplicates.review", "tenant", "Revisar posibles duplicados (sin fusionar)", True),
    PermissionDef("credit.products.read", "tenant", "Consultar productos de credito, versiones y simulaciones"),
    PermissionDef("credit.products.create", "tenant", "Crear productos de credito"),
    PermissionDef("credit.products.update_draft", "tenant", "Editar y validar borradores de version de producto"),
    PermissionDef("credit.products.publish", "tenant", "Publicar versiones y reactivar productos de credito", True),
    PermissionDef("credit.products.deactivate", "tenant", "Desactivar productos y retirar versiones", True),
    PermissionDef("credit.applications.read", "tenant", "Consultar solicitudes de credito"),
    PermissionDef("credit.applications.create", "tenant", "Crear solicitudes de credito"),
    PermissionDef("credit.applications.update_draft", "tenant", "Editar borradores y reabrir solicitudes enviadas"),
    PermissionDef("credit.applications.submit", "tenant", "Enviar solicitudes de credito"),
    PermissionDef("credit.applications.evaluate", "tenant", "Revisar, evaluar y gestionar requisitos", True),
    PermissionDef("credit.applications.approve", "tenant", "Aprobar solicitudes (sujeto a politica y limites)", True),
    PermissionDef("credit.applications.reject", "tenant", "Rechazar solicitudes de credito", True),
    PermissionDef("credit.applications.cancel", "tenant", "Cancelar solicitudes no formalizadas", True),
    PermissionDef("credit.applications.formalize", "tenant", "Formalizar solicitudes aprobadas", True),
    PermissionDef("credit.approval_policy.manage", "tenant", "Configurar la politica de aprobacion de credito", True),
    PermissionDef("credit.approval_limits.manage", "tenant", "Configurar limites de aprobacion", True),
    PermissionDef("loans.read", "tenant", "Consultar prestamos, desembolsos y cronogramas"),
    PermissionDef("loans.disburse", "tenant", "Desembolsar un contrato formalizado (sale dinero)", True),
    PermissionDef("payments.read", "tenant", "Consultar pagos de prestamos y sus aplicaciones"),
    PermissionDef(
        "collections.read",
        "tenant",
        "Consultar la cartera vencida (worklist de cobranza); solo lectura, sin datos personales",
    ),
    PermissionDef(
        "collections.assign",
        "tenant",
        "Asignar, reasignar y terminar la responsabilidad de cobranza de un prestamo (no concede acceso)",
        True,
    ),
    PermissionDef(
        "collections.actions.create",
        "tenant",
        "Registrar gestiones de cobranza sobre prestamos (no concede acceso de lectura)",
        True,
    ),
    PermissionDef(
        "collections.promises.create",
        "tenant",
        "Registrar, reemplazar y cancelar promesas de pago de cobranza sobre prestamos (no concede acceso de lectura)",
        True,
    ),
    PermissionDef(
        "loans.delinquency.assess",
        "tenant",
        "Evaluar el vencimiento de un prestamo y proyectar su estado (no calcula cargos de mora)",
        True,
    ),
    PermissionDef(
        "payments.reverse",
        "tenant",
        "Revertir por completo un pago de prestamo (sale efectivo en cobros de ventanilla)",
        True,
    ),
    PermissionDef("payments.create", "tenant", "Registrar pagos de prestamos (cobro en ventanilla o campo)", True),
    PermissionDef(
        "cash.field_custody.read",
        "tenant",
        "Consultar la custodia de efectivo de campo y sus rendiciones (solo lectura)",
    ),
    PermissionDef(
        "cash.field_custody.render",
        "tenant",
        "Cobrar en campo como custodio del efectivo y declarar o cancelar su rendicion",
        True,
    ),
    PermissionDef(
        "cash.field_custody.accept",
        "tenant",
        "Aceptar o rechazar rendiciones de efectivo de campo en la propia jornada de caja",
        True,
    ),
    PermissionDef(
        "cash.field_custody.refund",
        "tenant",
        "Devolver al cliente el efectivo de un pago de campo revertido (custodio o jornada propia)",
        True,
    ),
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
