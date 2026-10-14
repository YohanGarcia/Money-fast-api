"""Server-side authorization: deny-by-default, permission + scope based (ADR-004, DR-008).

The principal is rebuilt from the database on every request, so a revoked role or disabled user loses
access immediately regardless of the age of the token it presents.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.identity.catalog import ADMIN_FORBIDDEN_OPERATIVE_PERMISSIONS, CATALOG_CODES
from app.modules.identity.errors import DelegationCeilingViolation, PermissionDenied
from app.modules.identity.models import Permission, Role, RolePermission, UserAccount, UserRoleAssignment


@dataclass(frozen=True, slots=True)
class Grant:
    permission: str
    scope_kind: str  # tenant | branch | own
    branch_id: int | None = None
    cash_point_id: int | None = None


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: int
    tenant_id: int | None  # None = platform principal
    person_id: int | None
    session_id: int
    grants: tuple[Grant, ...]

    @property
    def is_platform(self) -> bool:
        return self.tenant_id is None

    def allows(
        self,
        permission: str,
        *,
        tenant_id: int | None = None,
        branch_id: int | None = None,
        owner_id: int | None = None,
        cash_point_id: int | None = None,
    ) -> bool:
        """DENY unless an existing grant of a catalogued permission covers the target."""
        if permission not in CATALOG_CODES:
            return False
        if tenant_id is not None and tenant_id != self.tenant_id:
            return False
        for g in self.grants:
            if g.permission != permission:
                continue
            if g.scope_kind == "tenant":
                return True
            if g.scope_kind == "branch" and branch_id is not None and g.branch_id == branch_id:
                return True
            if g.scope_kind == "cash_point" and cash_point_id is not None and g.cash_point_id == cash_point_id:
                return True
            if g.scope_kind == "own" and owner_id is not None and owner_id == self.user_id:
                return True
        return False

    def holds_at_tenant_scope(self, permission: str) -> bool:
        return permission in CATALOG_CODES and any(
            g.permission == permission and g.scope_kind == "tenant" for g in self.grants
        )

    def holds_for_cash_point(self, permission: str, cash_point_id: int, branch_id: int | None) -> bool:
        return permission in CATALOG_CODES and any(
            g.permission == permission
            and (
                g.scope_kind == "tenant"
                or (g.scope_kind == "branch" and branch_id is not None and g.branch_id == branch_id)
                or (g.scope_kind == "cash_point" and g.cash_point_id == cash_point_id)
            )
            for g in self.grants
        )

    def holds_for_branch(self, permission: str, branch_id: int) -> bool:
        return permission in CATALOG_CODES and any(
            g.permission == permission
            and (g.scope_kind == "tenant" or (g.scope_kind == "branch" and g.branch_id == branch_id))
            for g in self.grants
        )


def effective_grants(db: Session, user: UserAccount) -> tuple[Grant, ...]:
    """Grants of the user's active assignments. Roles must belong to the user's own tenant (or be platform
    roles for platform users) and permissions must match that kind; anything else is ignored."""
    kind = "platform" if user.company_id is None else "tenant"
    role_tenant = Role.tenant_id.is_(None) if user.company_id is None else Role.tenant_id == user.company_id
    rows = db.execute(
        select(
            Permission.code,
            UserRoleAssignment.scope_kind,
            UserRoleAssignment.branch_id,
            UserRoleAssignment.cash_point_id,
        )
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, Role.id == RolePermission.role_id)
        .join(UserRoleAssignment, UserRoleAssignment.role_id == Role.id)
        .where(
            UserRoleAssignment.user_id == user.id,
            UserRoleAssignment.revoked_at.is_(None),
            Role.status == "active",
            role_tenant,
            Permission.scope_kind == kind,
        )
    ).all()
    # FM-FUNCTIONAL-018: a company administrator never operates Caja, even with inherited or legacy grants.
    is_admin = getattr(getattr(user, "role", None), "value", getattr(user, "role", None)) == "admin"
    return tuple(
        sorted(
            {
                Grant(code, scope, branch, cp)
                for code, scope, branch, cp in rows
                if not (is_admin and code in ADMIN_FORBIDDEN_OPERATIVE_PERMISSIONS)
            },
            key=lambda g: (g.permission, g.scope_kind, g.branch_id or 0, g.cash_point_id or 0),
        )
    )


def build_principal(db: Session, user: UserAccount, session_id: int) -> Principal:
    return Principal(
        user_id=user.id,
        tenant_id=user.company_id,
        person_id=user.person_id,
        session_id=session_id,
        grants=effective_grants(db, user),
    )


def require(principal: Principal, permission: str, **target) -> None:
    if not principal.allows(permission, **target):
        raise PermissionDenied()


def assert_within_ceiling(
    actor: Principal,
    permission_codes: set[str],
    *,
    scope_kind: str = "tenant",
    branch_id: int | None = None,
    cash_point_id: int | None = None,
    cash_point_branch_id: int | None = None,
    delegable_operative: bool = False,
) -> None:
    """Delegation ceiling: the actor may only hand out permissions they hold themselves, at least as widely.

    Conservative: tenant-wide and 'own' grants require the actor to hold the permission tenant-wide;
    branch grants require tenant-wide or the same branch; cash-point grants require tenant-wide, the cash
    point's branch, or that cash point.
    """
    # FM-FUNCTIONAL-018-R1: the operative cash permissions are exempt from the "hold it yourself" ceiling ONLY when the
    # caller proved (``may_delegate_operative``) that a company administrator is handing them to a cashier of its own
    # company; every other actor stays bound by the plain ceiling and can never delegate them.
    exempt = ADMIN_FORBIDDEN_OPERATIVE_PERMISSIONS if delegable_operative else frozenset()
    for code in permission_codes - exempt:
        if scope_kind == "branch" and branch_id is not None:
            ok = actor.holds_for_branch(code, branch_id)
        elif scope_kind == "cash_point" and cash_point_id is not None:
            ok = actor.holds_for_cash_point(code, cash_point_id, cash_point_branch_id)
        else:
            ok = actor.holds_at_tenant_scope(code)
        if not ok:
            raise DelegationCeilingViolation()


def is_company_administrator(db: Session, actor: Principal) -> bool:
    """The caller is an ACTIVE administrator of the tenant it acts in: it holds the tenant-wide system administrator
    role of THAT tenant (the role registration / bootstrap gives the owner). Platform principals never qualify."""
    if actor.is_platform:
        return False
    from app.modules.identity.catalog import TENANT_ADMIN_ROLE

    user = db.get(UserAccount, actor.user_id)
    if user is None or user.company_id != actor.tenant_id or not user.is_active:
        return False
    return (
        db.scalar(
            select(UserRoleAssignment.id)
            .join(Role, Role.id == UserRoleAssignment.role_id)
            .where(
                UserRoleAssignment.user_id == user.id,
                UserRoleAssignment.scope_kind == "tenant",
                UserRoleAssignment.revoked_at.is_(None),
                Role.tenant_id == actor.tenant_id,
                Role.name == TENANT_ADMIN_ROLE,
                Role.status == "active",
            )
            .limit(1)
        )
        is not None
    )


def may_delegate_operative(db: Session, actor: Principal, target: UserAccount | None, scope_kind: str = "branch") -> bool:
    """FM-FUNCTIONAL-018-R1: operative cash permissions are handed out only by a company administrator, only to an
    active CASHIER of the same company and only scoped to a branch or a cash point (never tenant-wide)."""
    if scope_kind not in ("branch", "cash_point") or target is None:
        return False
    return (
        is_company_administrator(db, actor)
        and target.company_id == actor.tenant_id
        and target.is_active
        and getattr(target.role, "value", target.role) == "cashier"
    )
