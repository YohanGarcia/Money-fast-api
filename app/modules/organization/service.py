"""Organization use cases (T-003): tenant settings, branches, cash points, tenant currencies.

Every function receives the authenticated ``Principal``; the tenant is always the principal's. Other tenants'
ids behave like missing ones (404). Branch-scoped grants only reach their own branch. Nothing here opens or
closes a cash point, moves money or converts currencies. Reads never write.
"""

import re
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import BusinessRuleViolation, Conflict, ResourceNotFound, ValidationFailed
from app.core.time import get_zone, now_utc
from app.models.branch import Branch
from app.models.company import Company
from app.modules.identity.audit import record_event
from app.modules.identity.authorization import Principal, require
from app.modules.identity.errors import InvalidStateTransition, PermissionDenied, TenantMismatch
from app.modules.organization.catalog import sync_currency_catalog
from app.modules.organization.models import CashPoint, CashPointCurrency, Currency, TenantCurrency

CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,19}$")
CASH_POINT_EVENTS = {
    "disable": "org.cash_point.disabled",
    "enable": "org.cash_point.enabled",
    "suspend": "org.cash_point.suspended",
    "resume": "org.cash_point.resumed",
}


def normalize_code(value: str) -> str:
    code = value.strip().upper()
    if not CODE_PATTERN.fullmatch(code):
        raise ValidationFailed("El codigo debe tener 1-20 caracteres: letras, numeros, guion o guion bajo.")
    return code


def validate_timezone(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        get_zone(value.strip())
    except ValueError:
        raise ValidationFailed("La zona horaria debe ser un identificador IANA valido.") from None
    return value.strip()


def _tenant(db: Session, actor: Principal) -> Company:
    tenant = db.get(Company, actor.tenant_id) if actor.tenant_id is not None else None
    if tenant is None:
        raise TenantMismatch()
    return tenant


def _enabled_currency_codes(db: Session, tenant_id: int) -> set[str]:
    return set(
        db.scalars(
            select(TenantCurrency.currency_code).where(
                TenantCurrency.tenant_id == tenant_id, TenantCurrency.disabled_at.is_(None)
            )
        )
    )


def visible_branch_ids(actor: Principal, permission: str) -> set[int] | None:
    """None = every branch of the tenant; otherwise only the branches the actor's grants reach."""
    if actor.holds_at_tenant_scope(permission):
        return None
    ids = {g.branch_id for g in actor.grants if g.permission == permission and g.scope_kind == "branch" and g.branch_id}
    if not ids:
        raise PermissionDenied()
    return ids


# --- tenant ----------------------------------------------------------------------------------------
def get_current_tenant(db: Session, actor: Principal) -> Company:
    require(actor, "tenant.settings.read", tenant_id=actor.tenant_id)
    return _tenant(db, actor)


def update_tenant_settings(
    db: Session,
    actor: Principal,
    *,
    default_timezone: str | None,
    base_currency_code: str | None,
    client_ip: str | None,
) -> Company:
    """Sensitive: timezone / base currency changes are audited with before/after and never reinterpret history."""
    require(actor, "tenant.settings.manage", tenant_id=actor.tenant_id)
    tenant = _tenant(db, actor)
    before = {"default_timezone": tenant.default_timezone, "base_currency_code": tenant.base_currency_code}
    if default_timezone is not None:
        tenant.default_timezone = validate_timezone(default_timezone)
    if base_currency_code is not None:
        code = base_currency_code.strip().upper()
        if code not in _enabled_currency_codes(db, tenant.id):
            raise BusinessRuleViolation("La moneda base debe estar habilitada para la agencia.")
        tenant.base_currency_code = code
    after = {"default_timezone": tenant.default_timezone, "base_currency_code": tenant.base_currency_code}
    if after != before:
        event = "org.tenant.settings_changed"
        record_event(
            db,
            event,
            tenant_id=tenant.id,
            actor_id=actor.user_id,
            client_ip=client_ip,
            details={"before": before, "after": after},
        )
    db.commit()
    return tenant


def require_read_scope(db: Session, actor: Principal, branch_id: int | None) -> None:
    """Read access to effective config: tenant-wide for the tenant view, or the branch's own scope."""
    if branch_id is None:
        require(actor, "tenant.settings.read", tenant_id=actor.tenant_id)
        return
    _branch(db, actor, branch_id)  # another tenant's branch id is a 404 before any permission is evaluated
    require(actor, "tenant.settings.read", tenant_id=actor.tenant_id, branch_id=branch_id)


def get_current_tenant_base(db: Session, actor: Principal) -> str:
    return _tenant(db, actor).base_currency_code


# --- branches --------------------------------------------------------------------------------------
def _branch(db: Session, actor: Principal, branch_id: int) -> Branch:
    branch = db.get(Branch, branch_id)
    if branch is None or branch.company_id != actor.tenant_id:
        raise TenantMismatch()
    return branch


def _branch_snapshot(b: Branch) -> dict:
    return {
        "code": b.code,
        "name": b.name,
        "status": b.status,
        "timezone_override": b.timezone_override,
        "address": b.address,
        "phone": b.phone,
    }


def list_branches(db: Session, actor: Principal, status: str | None = None) -> list[Branch]:
    ids = visible_branch_ids(actor, "organization.branches.read")
    stmt = select(Branch).where(Branch.company_id == actor.tenant_id).order_by(Branch.id)
    if ids is not None:
        stmt = stmt.where(Branch.id.in_(ids))
    if status:
        stmt = stmt.where(Branch.status == status)
    return list(db.scalars(stmt))


def get_branch(db: Session, actor: Principal, branch_id: int) -> Branch:
    branch = _branch(db, actor, branch_id)
    require(actor, "organization.branches.read", tenant_id=actor.tenant_id, branch_id=branch.id)
    return branch


def create_branch(
    db: Session,
    actor: Principal,
    *,
    code: str,
    name: str,
    address: str,
    phone: str,
    timezone_override: str | None,
    client_ip: str | None,
) -> Branch:
    """Creating a branch is tenant-wide authority (a branch-scoped grant cannot mint new branches)."""
    if not actor.holds_at_tenant_scope("organization.branches.manage"):
        raise PermissionDenied()
    tenant = _tenant(db, actor)
    if tenant.status != "active":
        raise BusinessRuleViolation("La agencia esta inactiva.")
    branch = Branch(
        company_id=tenant.id,
        code=normalize_code(code),
        name=name.strip(),
        address=address.strip(),
        phone=phone.strip(),
        timezone_override=validate_timezone(timezone_override),
    )
    db.add(branch)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise Conflict("Ya existe una sucursal con ese codigo.") from None
    record_event(
        db,
        "org.branch.created",
        tenant_id=tenant.id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={"branch_id": branch.id, "after": _branch_snapshot(branch)},
    )
    db.commit()
    return branch


def update_branch(
    db: Session,
    actor: Principal,
    branch_id: int,
    *,
    name: str | None,
    address: str | None,
    phone: str | None,
    timezone_override: str | None,
    clear_timezone_override: bool,
    client_ip: str | None,
) -> Branch:
    branch = _branch(db, actor, branch_id)
    require(actor, "organization.branches.manage", tenant_id=actor.tenant_id, branch_id=branch.id)
    before = _branch_snapshot(branch)
    if name is not None:
        branch.name = name.strip()
    if address is not None:
        branch.address = address.strip()
    if phone is not None:
        branch.phone = phone.strip()
    if clear_timezone_override:
        branch.timezone_override = None
    elif timezone_override is not None:
        branch.timezone_override = validate_timezone(timezone_override)
    after = _branch_snapshot(branch)
    if after != before:
        etype = (
            "org.branch.timezone_changed"
            if after["timezone_override"] != before["timezone_override"]
            else "org.branch.updated"
        )
        record_event(
            db,
            etype,
            tenant_id=actor.tenant_id,
            actor_id=actor.user_id,
            client_ip=client_ip,
            details={"branch_id": branch.id, "before": before, "after": after},
        )
    db.commit()
    return branch


def set_branch_status(db: Session, actor: Principal, branch_id: int, new_status: str, client_ip: str | None) -> Branch:
    branch = _branch(db, actor, branch_id)
    require(actor, "organization.branches.manage", tenant_id=actor.tenant_id, branch_id=branch.id)
    if branch.status == new_status:
        raise InvalidStateTransition()
    before = branch.status
    branch.status = new_status
    record_event(
        db,
        "org.branch.disabled" if new_status == "inactive" else "org.branch.enabled",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={"branch_id": branch.id, "before": {"status": before}, "after": {"status": new_status}},
    )
    db.commit()
    return branch


# --- cash points -------------------------------------------------------------------------------------
def _cash_point(db: Session, actor: Principal, cash_point_id: int) -> CashPoint:
    cp = db.get(CashPoint, cash_point_id)
    if cp is None or cp.tenant_id != actor.tenant_id:
        raise TenantMismatch()
    return cp


def _cp_require(actor: Principal, permission: str, cp: CashPoint) -> None:
    require(actor, permission, tenant_id=actor.tenant_id, branch_id=cp.branch_id, cash_point_id=cp.id)


def cash_point_currencies(db: Session, cash_point_id: int) -> list[str]:
    return sorted(
        db.scalars(select(CashPointCurrency.currency_code).where(CashPointCurrency.cash_point_id == cash_point_id))
    )


def _cp_snapshot(db: Session, cp: CashPoint) -> dict:
    return {
        "code": cp.code,
        "name": cp.name,
        "status": cp.status,
        "branch_id": cp.branch_id,
        "currencies": cash_point_currencies(db, cp.id),
    }


def list_cash_points(db: Session, actor: Principal, branch_id: int | None = None) -> list[CashPoint]:
    if not any(g.permission == "cash.points.read" for g in actor.grants):
        raise PermissionDenied()  # deny-by-default: no grant at all is a 403, not an empty list
    stmt = select(CashPoint).where(CashPoint.tenant_id == actor.tenant_id).order_by(CashPoint.id)
    if branch_id is not None:
        stmt = stmt.where(CashPoint.branch_id == branch_id)
    rows = list(db.scalars(stmt))
    return [
        cp
        for cp in rows
        if actor.allows("cash.points.read", tenant_id=actor.tenant_id, branch_id=cp.branch_id, cash_point_id=cp.id)
    ]


def get_cash_point(db: Session, actor: Principal, cash_point_id: int) -> CashPoint:
    cp = _cash_point(db, actor, cash_point_id)
    _cp_require(actor, "cash.points.read", cp)
    return cp


def _validated_currencies(db: Session, tenant_id: int, codes: list[str]) -> set[str]:
    wanted = {c.strip().upper() for c in codes}
    if not wanted <= _enabled_currency_codes(db, tenant_id):
        raise BusinessRuleViolation("Las monedas de la caja deben estar habilitadas para la agencia.")
    return wanted


def create_cash_point(
    db: Session, actor: Principal, *, branch_id: int, code: str, name: str, currencies: list[str], client_ip: str | None
) -> CashPoint:
    branch = _branch(db, actor, branch_id)  # a branch of another tenant is a 404, never a mismatch error
    require(actor, "cash.points.manage", tenant_id=actor.tenant_id, branch_id=branch.id)
    if branch.status != "active" or _tenant(db, actor).status != "active":
        raise BusinessRuleViolation("No se pueden crear cajas en una sucursal o agencia inactiva.")
    wanted = _validated_currencies(db, actor.tenant_id, currencies)
    cp = CashPoint(tenant_id=actor.tenant_id, branch_id=branch.id, code=normalize_code(code), name=name.strip())
    db.add(cp)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise Conflict("Ya existe una caja con ese codigo.") from None
    for c in sorted(wanted):
        db.add(CashPointCurrency(cash_point_id=cp.id, currency_code=c, tenant_id=actor.tenant_id))
    db.flush()
    record_event(
        db,
        "org.cash_point.created",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={"cash_point_id": cp.id, "after": _cp_snapshot(db, cp)},
    )
    db.commit()
    return cp


def set_cash_point_currencies(
    db: Session, actor: Principal, cash_point_id: int, currencies: list[str], client_ip: str | None
) -> CashPoint:
    cp = _cash_point(db, actor, cash_point_id)
    _cp_require(actor, "cash.points.manage", cp)
    wanted = _validated_currencies(db, actor.tenant_id, currencies)
    before = _cp_snapshot(db, cp)
    db.execute(delete(CashPointCurrency).where(CashPointCurrency.cash_point_id == cp.id))
    for c in sorted(wanted):
        db.add(CashPointCurrency(cash_point_id=cp.id, currency_code=c, tenant_id=actor.tenant_id))
    db.flush()
    record_event(
        db,
        "org.cash_point.currencies_changed",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={"cash_point_id": cp.id, "before": before, "after": _cp_snapshot(db, cp)},
    )
    db.commit()
    return cp


def set_cash_point_status(
    db: Session, actor: Principal, cash_point_id: int, action: str, *, reason: str | None, client_ip: str | None
) -> CashPoint:
    """Explicit transitions only: disable | enable | suspend | resume. Nothing here is ever triggered by cash
    differences; suspension needs its own permission and a recorded reason."""
    cp = _cash_point(db, actor, cash_point_id)
    transitions = {
        "disable": (("active", "suspended"), "inactive", "cash.points.manage"),
        "enable": (("inactive",), "active", "cash.points.manage"),
        "suspend": (("active",), "suspended", "cash.points.suspend"),
        "resume": (("suspended",), "active", "cash.points.suspend"),
    }
    allowed_from, target, permission = transitions[action]
    _cp_require(actor, permission, cp)
    if cp.status not in allowed_from:
        raise InvalidStateTransition()
    if action == "suspend" and not (reason and reason.strip()):
        raise ValidationFailed("La suspension requiere un motivo.")
    before = {"status": cp.status, "suspension_reason": cp.suspension_reason}
    cp.status = target
    now: datetime = now_utc()
    cp.suspended_at = now if target == "suspended" else None
    cp.suspension_reason = reason.strip() if action == "suspend" else None
    record_event(
        db,
        CASH_POINT_EVENTS[action],
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={
            "cash_point_id": cp.id,
            "before": before,
            "after": {"status": cp.status, "suspension_reason": cp.suspension_reason},
        },
    )
    db.commit()
    return cp


def ensure_cash_point_usable(db: Session, tenant_id: int, cash_point_id: int, currency: str | None = None) -> CashPoint:
    """Gate for NEW sessions/operations (used by the future Caja package): tenant, branch and cash point must be
    active, and the currency enabled for the tenant and admitted by the cash point. Historical rows are unaffected."""
    cp = db.get(CashPoint, cash_point_id)
    if cp is None or cp.tenant_id != tenant_id:
        raise ResourceNotFound("Caja no encontrada.")
    tenant, branch = db.get(Company, tenant_id), db.get(Branch, cp.branch_id)
    if tenant.status != "active" or branch.status != "active" or cp.status != "active":
        raise BusinessRuleViolation("La caja no esta disponible para nuevas operaciones.")
    if currency is not None:
        code = currency.upper()
        if code not in _enabled_currency_codes(db, tenant_id):
            raise BusinessRuleViolation("La moneda no esta habilitada para la agencia.")
        allowed = cash_point_currencies(db, cp.id)
        if allowed and code not in allowed:
            raise BusinessRuleViolation("La caja no admite esa moneda.")
    return cp


# --- currencies ----------------------------------------------------------------------------------------
def list_catalog(db: Session, actor: Principal) -> list[Currency]:
    require(actor, "currencies.read", tenant_id=actor.tenant_id)
    sync_currency_catalog(db)
    return list(db.scalars(select(Currency).order_by(Currency.code)))


def list_tenant_currencies(db: Session, actor: Principal) -> list[tuple[TenantCurrency, Currency]]:
    require(actor, "currencies.read", tenant_id=actor.tenant_id)
    return list(
        db.execute(
            select(TenantCurrency, Currency)
            .join(Currency, Currency.code == TenantCurrency.currency_code)
            .where(TenantCurrency.tenant_id == actor.tenant_id)
            .order_by(TenantCurrency.currency_code)
        ).all()
    )


def enable_currency(db: Session, actor: Principal, code: str, client_ip: str | None) -> TenantCurrency:
    require(actor, "currencies.manage", tenant_id=actor.tenant_id)
    code = code.strip().upper()
    currency = db.get(Currency, code)
    if currency is None or not currency.is_active:
        raise ResourceNotFound("Moneda no encontrada.")
    row = db.get(TenantCurrency, (actor.tenant_id, code))
    if row is not None and row.disabled_at is None:
        raise InvalidStateTransition("La moneda ya esta habilitada.")
    if row is None:
        row = TenantCurrency(tenant_id=actor.tenant_id, currency_code=code, enabled_by=actor.user_id)
        db.add(row)
    else:
        row.enabled_at, row.enabled_by, row.disabled_at, row.disabled_by = now_utc(), actor.user_id, None, None
    record_event(
        db,
        "org.currency.enabled",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={"currency": code},
    )
    db.commit()
    return row


def disable_currency(db: Session, actor: Principal, code: str, client_ip: str | None) -> TenantCurrency:
    require(actor, "currencies.manage", tenant_id=actor.tenant_id)
    code = code.strip().upper()
    tenant = _tenant(db, actor)
    row = db.get(TenantCurrency, (actor.tenant_id, code))
    if row is None:
        raise ResourceNotFound("Moneda no habilitada para la agencia.")
    if row.disabled_at is not None:
        raise InvalidStateTransition("La moneda ya esta deshabilitada.")
    if code == tenant.base_currency_code:
        raise BusinessRuleViolation("No se puede deshabilitar la moneda base; cambia primero la moneda base.")
    row.disabled_at, row.disabled_by = now_utc(), actor.user_id  # row kept: history stays queryable
    record_event(
        db,
        "org.currency.disabled",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={"currency": code},
    )
    db.commit()
    return row
