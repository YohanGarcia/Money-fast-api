"""Administration of the platform by its owner: global settings, payment methods, coupons, staff, company file with
usage and support actions, audit, pending bank transfers, renewals and revenue reports.

Everything requires a platform account (``role = superadmin``). Inside, ``owner`` may change anything; ``support`` may
read and assist (short extensions, admin password reset, session revocation)."""
from __future__ import annotations

import secrets
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_db, require_superadmin
from app.api.pagination import Page, page_params
from app.core.security import get_password_hash
from app.models.branch import Branch
from app.models.cash import CashBox, CashSession
from app.models.company import Company
from app.models.customer import Customer
from app.models.loan import Loan
from app.models.plan import Plan
from app.models.platform import Coupon, PaymentMethod, PlatformStaff
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.user import User, UserRole
from app.modules.identity.audit import record_event
from app.modules.identity.auth import revoke_user_sessions
from app.modules.identity.models import SecurityEvent
from app.services import platform_config
from app.services.billing import CYCLES, activate_subscription, plan_price
from app.services.email_service import EmailNotConfigured, send_html_email, smtp_configured
from app.services.plan_limits import effective_plan, get_free_plan, subscription_is_current
from app.services.xlsx import build_xlsx

router = APIRouter()
public_router = APIRouter()


# ───────────────────────────── access levels ─────────────────────────────
def platform_level(db: Session, user: User) -> str:
    row = db.scalar(select(PlatformStaff).where(PlatformStaff.user_id == user.id))
    return row.level if row is not None else "owner"  # an account without a row is a legacy owner


def require_owner(user: User = Depends(require_superadmin), db: Session = Depends(get_db)) -> User:
    if platform_level(db, user) != "owner":
        raise HTTPException(403, "Esta acción es solo para el dueño de la plataforma.")
    return user


def _now() -> datetime:
    return datetime.now(UTC)


def _tz(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


# ───────────────────────────── public status ─────────────────────────────
@public_router.get("/public")
def public_status(db: Session = Depends(get_db)) -> dict:
    """What any visitor may know: maintenance, the announcement in force and whether registration is open."""
    cfg = platform_config.current(db)
    return {
        "maintenance_mode": bool(cfg.get("maintenance_mode")),
        "maintenance_message": cfg.get("maintenance_message") or "",
        "announcement": platform_config.announcement(cfg),
        "registration_open": bool(cfg.get("registration_open", True)),
        "support_email": cfg.get("support_email") or "",
        "support_phone": cfg.get("support_phone") or "",
    }


# ───────────────────────────── settings ─────────────────────────────
class SettingsIn(BaseModel):
    registration_open: bool | None = None
    default_plan_id: int | None = Field(default=None, ge=1)
    trial_days: int | None = Field(default=None, ge=0, le=365)
    annual_discount_percent: Decimal | None = Field(default=None, ge=0, le=90)
    grace_days: int | None = Field(default=None, ge=0, le=90)
    reminder_days: list[int] | None = Field(default=None, max_length=6)
    allow_monthly: bool | None = None
    allow_annual: bool | None = None
    support_email: str | None = Field(default=None, max_length=160)
    support_phone: str | None = Field(default=None, max_length=40)
    maintenance_mode: bool | None = None
    maintenance_message: str | None = Field(default=None, max_length=500)
    announcement_enabled: bool | None = None
    announcement_level: Literal["info", "warning", "critical"] | None = None
    announcement_title: str | None = Field(default=None, max_length=120)
    announcement_message: str | None = Field(default=None, max_length=600)
    announcement_until: str | None = Field(default=None, max_length=40)
    password_min_length: int | None = Field(default=None, ge=8, le=64)
    account_lock_failures: int | None = Field(default=None, ge=3, le=100)
    account_lock_seconds: int | None = Field(default=None, ge=60, le=86400)
    access_token_minutes: int | None = Field(default=None, ge=5, le=1440)
    refresh_token_days: int | None = Field(default=None, ge=1, le=90)
    smtp_host: str | None = Field(default=None, max_length=160)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_user: str | None = Field(default=None, max_length=160)
    smtp_password: str | None = Field(default=None, max_length=200)
    smtp_from_email: str | None = Field(default=None, max_length=160)
    smtp_from_name: str | None = Field(default=None, max_length=80)
    paypal_mode: Literal["sandbox", "live"] | None = None
    paypal_client_id: str | None = Field(default=None, max_length=200)
    paypal_secret: str | None = Field(default=None, max_length=300)
    paypal_webhook_id: str | None = Field(default=None, max_length=100)

    @field_validator("reminder_days")
    @classmethod
    def _days(cls, value):
        if value is None:
            return value
        if any(d < 0 or d > 90 for d in value):
            raise ValueError("Los días de recordatorio deben estar entre 0 y 90.")
        return sorted(set(value), reverse=True)

    @field_validator("announcement_until")
    @classmethod
    def _until(cls, value):
        if value in (None, ""):
            return None
        try:
            datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("La fecha del aviso no es válida.") from exc
        return value


@router.get("/settings")
def get_settings(user: User = Depends(require_superadmin), db: Session = Depends(get_db)) -> dict:
    cfg = platform_config.load(db)
    return {"settings": platform_config.public_view(cfg), "level": platform_level(db, user),
            "smtp_configured": smtp_configured()}


@router.put("/settings")
def update_settings(payload: SettingsIn, user: User = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    changes = payload.model_dump(exclude_unset=True)
    if "default_plan_id" in changes and changes["default_plan_id"] is not None:
        plan = db.get(Plan, changes["default_plan_id"])
        if plan is None or not plan.is_active:
            raise HTTPException(422, "El plan inicial debe existir y estar activo.")
    cur = platform_config.load(db)
    plan_id = changes.get("default_plan_id", cur.get("default_plan_id"))
    trial = changes.get("trial_days", cur.get("trial_days"))
    if plan_id:
        chosen = db.get(Plan, plan_id)
        if chosen is not None and chosen.monthly_price_usd > 0 and not trial:
            raise HTTPException(422, "Un plan de pago como plan inicial necesita días de prueba.")
    if "annual_discount_percent" in changes and changes["annual_discount_percent"] is not None:
        changes["annual_discount_percent"] = float(changes["annual_discount_percent"])
    if changes.get("allow_monthly") is False and changes.get("allow_annual") is False:
        raise HTTPException(422, "Debe quedar al menos un ciclo de pago disponible.")
    platform_config.save(db, changes, user.id)
    record_event(db, "platform.settings.updated", actor_id=user.id, details={"keys": ",".join(sorted(changes))})
    db.commit()
    cfg = platform_config.load(db)
    return {"settings": platform_config.public_view(cfg), "smtp_configured": smtp_configured()}


class TestEmailIn(BaseModel):
    to: EmailStr


@router.post("/settings/test-email")
def test_email(payload: TestEmailIn, user: User = Depends(require_owner)) -> dict:
    try:
        send_html_email(str(payload.to), "Prueba de correo — MoneyFast",
                        "<p>Este es un correo de prueba de la configuración de la plataforma.</p>")
    except EmailNotConfigured:
        raise HTTPException(409, "El correo (SMTP) no está configurado.")
    except Exception as exc:
        raise HTTPException(502, f"No se pudo enviar el correo: {type(exc).__name__}")
    return {"ok": True}


# ───────────────────────────── payment methods ─────────────────────────────
class MethodIn(BaseModel):
    kind: Literal["paypal", "bank_transfer"]
    label: str = Field(min_length=2, max_length=80)
    is_enabled: bool = True
    sort_order: int = Field(default=0, ge=0, le=999)
    details: dict = Field(default_factory=dict)

    @field_validator("details")
    @classmethod
    def _details(cls, value):
        allowed = {"accept_account", "accept_card", "recurring", "bank_name", "account_type", "account_number",
                   "account_holder", "holder_document", "currency", "instructions"}
        if set(value) - allowed:
            raise ValueError("Datos de método de pago no reconocidos.")
        for k, v in value.items():
            if isinstance(v, str) and len(v) > 600:
                raise ValueError(f"El campo {k} es demasiado largo.")
        return value


def _method_dict(m: PaymentMethod) -> dict:
    return {"id": m.id, "kind": m.kind, "label": m.label, "is_enabled": m.is_enabled, "sort_order": m.sort_order,
            "details": m.details or {}}


def _validate_method(body: MethodIn) -> None:
    if body.kind == "bank_transfer" and body.is_enabled:
        d = body.details
        missing = [k for k in ("bank_name", "account_number", "account_holder") if not str(d.get(k, "")).strip()]
        if missing:
            raise HTTPException(422, "Para activar la transferencia indica banco, número de cuenta y titular.")
    if body.kind == "paypal" and body.is_enabled:
        d = body.details
        if d.get("accept_account", True) is False and d.get("accept_card", True) is False:
            raise HTTPException(422, "PayPal debe aceptar al menos pago con cuenta o con tarjeta.")


@router.get("/payment-methods")
def list_methods(_: User = Depends(require_superadmin), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(PaymentMethod).order_by(PaymentMethod.sort_order, PaymentMethod.id)).all()
    return [_method_dict(m) for m in rows]


@router.post("/payment-methods", status_code=201)
def create_method(body: MethodIn, user: User = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    _validate_method(body)
    if body.kind == "paypal" and db.scalar(select(PaymentMethod.id).where(PaymentMethod.kind == "paypal")):
        raise HTTPException(409, "Ya existe un método PayPal: edítalo.")
    m = PaymentMethod(**body.model_dump())
    db.add(m)
    record_event(db, "platform.payment_method.created", actor_id=user.id, details={"kind": body.kind})
    db.commit()
    db.refresh(m)
    return _method_dict(m)


@router.put("/payment-methods/{method_id}")
def update_method(method_id: int, body: MethodIn, user: User = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    m = db.get(PaymentMethod, method_id)
    if m is None:
        raise HTTPException(404, "Método de pago no encontrado.")
    if m.kind != body.kind:
        raise HTTPException(422, "No se puede cambiar el tipo de un método.")
    _validate_method(body)
    m.label, m.is_enabled, m.sort_order, m.details = body.label, body.is_enabled, body.sort_order, body.details
    record_event(db, "platform.payment_method.updated", actor_id=user.id, details={"kind": m.kind, "enabled": m.is_enabled})
    db.commit()
    return _method_dict(m)


@router.delete("/payment-methods/{method_id}", status_code=204)
def delete_method(method_id: int, user: User = Depends(require_owner), db: Session = Depends(get_db)) -> None:
    m = db.get(PaymentMethod, method_id)
    if m is None:
        raise HTTPException(404, "Método de pago no encontrado.")
    if m.kind == "paypal":
        raise HTTPException(409, "PayPal no se elimina: desactívalo.")
    db.delete(m)
    record_event(db, "platform.payment_method.deleted", actor_id=user.id, details={"kind": m.kind})
    db.commit()


# ───────────────────────────── coupons ─────────────────────────────
class CouponIn(BaseModel):
    code: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    description: str = Field(default="", max_length=200)
    kind: Literal["percent", "fixed_usd", "free_months"]
    value: Decimal = Field(gt=0, max_digits=8, decimal_places=2)
    plan_id: int | None = Field(default=None, ge=1)
    billing_cycle: Literal["monthly", "annual"] | None = None
    max_redemptions: int | None = Field(default=None, ge=1, le=1_000_000)
    valid_from: date | None = None
    valid_until: date | None = None
    is_active: bool = True


def _coupon_dict(c: Coupon) -> dict:
    return {"id": c.id, "code": c.code, "description": c.description, "kind": c.kind, "value": str(c.value),
            "plan_id": c.plan_id, "billing_cycle": c.billing_cycle, "max_redemptions": c.max_redemptions,
            "redeemed_count": c.redeemed_count, "valid_from": c.valid_from, "valid_until": c.valid_until,
            "is_active": c.is_active, "created_at": c.created_at}


def _check_coupon_body(body: CouponIn, db: Session) -> None:
    if body.kind == "percent" and body.value > 100:
        raise HTTPException(422, "El porcentaje no puede superar 100.")
    if body.kind == "free_months" and (body.value != body.value.to_integral_value() or body.value > 24):
        raise HTTPException(422, "Los meses gratis deben ser un número entero hasta 24.")
    if body.valid_from and body.valid_until and body.valid_from > body.valid_until:
        raise HTTPException(422, "El rango de vigencia es inválido.")
    if body.plan_id is not None and db.get(Plan, body.plan_id) is None:
        raise HTTPException(404, "El plan del cupón no existe.")


@router.get("/coupons")
def list_coupons(response: Response, q: str | None = None, active: bool | None = None,
                 page: Page = Depends(page_params), _: User = Depends(require_superadmin),
                 db: Session = Depends(get_db)) -> list[dict]:
    stmt = select(Coupon).order_by(Coupon.id.desc())
    if active is not None:
        stmt = stmt.where(Coupon.is_active.is_(active))
    if q and q.strip():
        term = f"%{q.strip()}%"
        stmt = stmt.where(or_(Coupon.code.ilike(term), Coupon.description.ilike(term)))
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    response.headers["X-Total-Count"] = str(total)
    if page.limit is not None:
        stmt = stmt.limit(page.limit).offset(page.offset)
    return [_coupon_dict(c) for c in db.scalars(stmt).all()]


@router.post("/coupons", status_code=201)
def create_coupon(body: CouponIn, user: User = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    _check_coupon_body(body, db)
    code = body.code.upper()
    if db.scalar(select(Coupon.id).where(Coupon.code == code)):
        raise HTTPException(409, "Ya existe un cupón con ese código.")
    c = Coupon(**{**body.model_dump(), "code": code})
    db.add(c)
    record_event(db, "platform.coupon.created", actor_id=user.id, details={"code": code, "kind": body.kind})
    db.commit()
    db.refresh(c)
    return _coupon_dict(c)


@router.put("/coupons/{coupon_id}")
def update_coupon(coupon_id: int, body: CouponIn, user: User = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    c = db.get(Coupon, coupon_id)
    if c is None:
        raise HTTPException(404, "Cupón no encontrado.")
    _check_coupon_body(body, db)
    if body.code.upper() != c.code:
        raise HTTPException(422, "El código de un cupón no se puede cambiar.")
    if body.max_redemptions is not None and body.max_redemptions < c.redeemed_count:
        raise HTTPException(422, "El límite no puede ser menor que los usos ya registrados.")
    for k, v in body.model_dump(exclude={"code"}).items():
        setattr(c, k, v)
    record_event(db, "platform.coupon.updated", actor_id=user.id, details={"code": c.code, "active": c.is_active})
    db.commit()
    return _coupon_dict(c)


@router.delete("/coupons/{coupon_id}", status_code=204)
def delete_coupon(coupon_id: int, user: User = Depends(require_owner), db: Session = Depends(get_db)) -> None:
    c = db.get(Coupon, coupon_id)
    if c is None:
        raise HTTPException(404, "Cupón no encontrado.")
    if c.redeemed_count > 0:
        raise HTTPException(409, "El cupón ya se usó: desactívalo en vez de borrarlo.")
    db.delete(c)
    record_event(db, "platform.coupon.deleted", actor_id=user.id, details={"code": c.code})
    db.commit()


# ───────────────────────────── platform staff ─────────────────────────────
class StaffIn(BaseModel):
    full_name: str = Field(min_length=3, max_length=140)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    level: Literal["owner", "support"] = "support"
    notes: str = Field(default="", max_length=300)


class StaffUpdate(BaseModel):
    level: Literal["owner", "support"] | None = None
    is_active: bool | None = None
    notes: str | None = Field(default=None, max_length=300)


def _staff_dict(u: User, s: PlatformStaff | None) -> dict:
    return {"user_id": u.id, "full_name": u.full_name, "email": u.email, "level": s.level if s else "owner",
            "is_active": u.status == "active", "last_login_at": u.last_login_at, "created_at": u.created_at,
            "notes": s.notes if s else ""}


@router.get("/staff")
def list_staff(_: User = Depends(require_superadmin), db: Session = Depends(get_db)) -> list[dict]:
    users = db.scalars(select(User).where(User.role == UserRole.superadmin).order_by(User.full_name)).all()
    staff = {s.user_id: s for s in db.scalars(select(PlatformStaff)).all()}
    return [_staff_dict(u, staff.get(u.id)) for u in users]


def _owners(db: Session) -> int:
    ids = db.scalars(select(User.id).where(User.role == UserRole.superadmin, User.status == "active")).all()
    levels = {s.user_id: s.level for s in db.scalars(select(PlatformStaff)).all()}
    return sum(1 for i in ids if levels.get(i, "owner") == "owner")


@router.post("/staff", status_code=201)
def create_staff(body: StaffIn, user: User = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    from app.core.security import password_policy_violation

    violation = password_policy_violation(body.password)
    if violation:
        raise HTTPException(422, violation)
    email = str(body.email).lower().strip()
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(409, "Ya existe un usuario con ese correo.")
    now = _now()
    account = User(full_name=body.full_name.strip(), email=email, password_hash=get_password_hash(body.password),
                   role=UserRole.superadmin, company_id=None, activated_at=now, password_changed_at=now)
    db.add(account)
    db.flush()
    staff = PlatformStaff(user_id=account.id, level=body.level, created_by=user.id, notes=body.notes)
    db.add(staff)
    record_event(db, "platform.staff.created", actor_id=user.id, subject_id=account.id, details={"level": body.level})
    db.commit()
    return _staff_dict(account, staff)


@router.put("/staff/{user_id}")
def update_staff(user_id: int, body: StaffUpdate, user: User = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    target = db.get(User, user_id)
    if target is None or target.role != UserRole.superadmin:
        raise HTTPException(404, "Miembro del equipo no encontrado.")
    staff = db.scalar(select(PlatformStaff).where(PlatformStaff.user_id == user_id))
    if staff is None:
        staff = PlatformStaff(user_id=user_id, level="owner", created_by=user.id)
        db.add(staff)
        db.flush()
    losing_owner = (body.level == "support" and staff.level == "owner") or (body.is_active is False and staff.level == "owner")
    if losing_owner and _owners(db) <= 1:
        raise HTTPException(409, "Debe quedar al menos un dueño activo de la plataforma.")
    if target.id == user.id and body.is_active is False:
        raise HTTPException(409, "No puedes desactivar tu propia cuenta.")
    if body.level is not None:
        staff.level = body.level
    if body.notes is not None:
        staff.notes = body.notes
    if body.is_active is not None:
        target.status = "active" if body.is_active else "disabled"
        if body.is_active is False:
            revoke_user_sessions(db, target.id, "platform_staff_disabled")
    record_event(db, "platform.staff.updated", actor_id=user.id, subject_id=target.id,
                 details={"level": staff.level, "active": target.status == "active"})
    db.commit()
    return _staff_dict(target, staff)


def _temp_password() -> str:
    return secrets.token_urlsafe(9) + "-" + secrets.token_hex(2).upper() + "a1"


def _reset_password(db: Session, target: User, actor: User) -> str:
    temp = _temp_password()
    target.password_hash = get_password_hash(temp)
    target.password_changed_at = _now()
    if target.status in ("locked",):
        target.status, target.locked_at, target.locked_until = "active", None, None
    revoke_user_sessions(db, target.id, "password_reset_by_platform")
    record_event(db, "platform.password.reset", actor_id=actor.id, subject_id=target.id, tenant_id=target.company_id)
    return temp


@router.post("/staff/{user_id}/reset-password")
def reset_staff_password(user_id: int, user: User = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    target = db.get(User, user_id)
    if target is None or target.role != UserRole.superadmin:
        raise HTTPException(404, "Miembro del equipo no encontrado.")
    temp = _reset_password(db, target, user)
    db.commit()
    return {"user_id": target.id, "temporary_password": temp}


# ───────────────────────────── company file ─────────────────────────────
def company_usage(db: Session, company: Company) -> dict:
    plan = effective_plan(db, company.id)
    users = db.scalar(select(func.count()).select_from(User).where(User.company_id == company.id)) or 0
    customers = db.scalar(select(func.count()).select_from(Customer).where(Customer.company_id == company.id)) or 0
    loans = db.scalar(select(func.count()).select_from(Loan).join(Loan.customer).where(Customer.company_id == company.id)) or 0
    branches = db.scalar(select(func.count()).select_from(Branch).where(Branch.company_id == company.id)) or 0
    open_shifts = db.scalar(
        select(func.count()).select_from(CashSession).join(CashBox, CashBox.id == CashSession.box_id)
        .where(CashBox.company_id == company.id, CashSession.state.in_(["open", "closing"]))
    ) or 0

    def limit(value: int | None) -> int | None:
        return None if not value else value  # 0 = unlimited

    return {
        "users": {"used": users, "limit": limit(plan.user_limit if plan else None)},
        "customers": {"used": customers, "limit": limit(plan.customer_limit if plan else None)},
        "loans": {"used": loans, "limit": limit(plan.loan_limit if plan else None)},
        "branches": branches,
        "open_shifts": open_shifts,
        "effective_plan": plan.name if plan else None,
    }


def _sub_dict(s: Subscription) -> dict:
    return {"id": s.id, "plan_id": s.plan_id, "plan_name": s.plan_name, "amount_usd": str(s.amount_usd),
            "discount_usd": str(s.discount_usd or 0), "status": s.status.value, "provider": s.provider,
            "payment_method": s.payment_method, "billing_cycle": s.billing_cycle, "coupon_code": s.coupon_code,
            "reference": s.reference or "", "period_start": s.period_start, "period_end": s.period_end,
            "created_at": s.created_at, "reviewed_at": s.reviewed_at, "review_note": s.review_note or "",
            "has_proof": bool(s.proof)}


@router.get("/companies/{company_id}")
def company_file(company_id: int, user: User = Depends(require_superadmin), db: Session = Depends(get_db)) -> dict:
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(404, "Empresa no encontrada.")
    settings_row = db.execute(
        select(func.max(User.last_login_at)).where(User.company_id == company_id)
    ).scalar()
    admins = db.scalars(
        select(User).where(User.company_id == company_id, User.role == UserRole.admin).order_by(User.id)
    ).all()
    subs = db.scalars(
        select(Subscription).where(Subscription.company_id == company_id)
        .options(selectinload(Subscription.plan)).order_by(Subscription.created_at.desc()).limit(25)
    ).all()
    last_event = db.scalar(select(func.max(SecurityEvent.occurred_at)).where(SecurityEvent.tenant_id == company_id))
    plan = db.get(Plan, company.plan_id) if company.plan_id else None
    expires = _tz(company.subscription_expires_at)
    return {
        "id": company.id, "name": company.name, "slug": company.slug, "tax_id": company.tax_id, "phone": company.phone,
        "address": company.address, "status": company.status, "created_at": company.created_at,
        "plan": {"id": plan.id, "name": plan.name, "monthly_price_usd": str(plan.monthly_price_usd)} if plan else None,
        "billing_cycle": company.billing_cycle,
        "subscription_expires_at": expires,
        "subscription_current": subscription_is_current(company),
        "days_remaining": (expires - _now()).days if expires else None,
        "recurring_paypal": bool(company.paypal_subscription_id),
        "usage": company_usage(db, company),
        "admins": [{"user_id": a.id, "full_name": a.full_name, "email": a.email, "status": a.status,
                    "last_login_at": a.last_login_at} for a in admins],
        "last_login_at": settings_row,
        "last_activity_at": last_event,
        "subscriptions": [_sub_dict(s) for s in subs],
        "level": platform_level(db, user),
    }


class ExtendIn(BaseModel):
    days: int = Field(ge=1, le=3650)
    note: str = Field(default="", max_length=300)


class ChangePlanIn(BaseModel):
    plan_id: int = Field(ge=1)
    cycle: Literal["monthly", "annual"] = "monthly"
    expires_at: datetime | None = None
    note: str = Field(default="", max_length=300)


class ReasonIn(BaseModel):
    reason: str = Field(default="", max_length=300)


class AdminResetIn(BaseModel):
    user_id: int | None = None


def _company_or_404(db: Session, company_id: int) -> Company:
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(404, "Empresa no encontrada.")
    return company


@router.post("/companies/{company_id}/extend")
def extend_subscription(company_id: int, body: ExtendIn, user: User = Depends(require_superadmin),
                        db: Session = Depends(get_db)) -> dict:
    level = platform_level(db, user)
    if level != "owner" and body.days > 15:
        raise HTTPException(403, "El soporte puede extender hasta 15 días; para más, el dueño de la plataforma.")
    company = _company_or_404(db, company_id)
    plan = db.get(Plan, company.plan_id) if company.plan_id else get_free_plan(db)
    if plan is None:
        raise HTTPException(409, "La empresa no tiene un plan.")
    now = _now()
    base = _tz(company.subscription_expires_at)
    start = base if (base is not None and base > now) else now
    company.plan_id = plan.id
    company.subscription_expires_at = start + timedelta(days=body.days)
    company.renewal_notice_for = None
    record_event(db, "platform.company.extended", actor_id=user.id, tenant_id=company.id,
                 details={"days": body.days, "note": body.note, "level": level})
    db.commit()
    return {"subscription_expires_at": company.subscription_expires_at, "plan_id": company.plan_id}


@router.post("/companies/{company_id}/plan")
def change_plan(company_id: int, body: ChangePlanIn, user: User = Depends(require_owner),
                db: Session = Depends(get_db)) -> dict:
    company = _company_or_404(db, company_id)
    plan = db.get(Plan, body.plan_id)
    if plan is None:
        raise HTTPException(404, "Plan no encontrado.")
    company.plan_id = plan.id
    company.billing_cycle = body.cycle if plan.monthly_price_usd > 0 else None
    if body.expires_at is not None:
        company.subscription_expires_at = _tz(body.expires_at)
    elif plan.monthly_price_usd == 0:
        company.subscription_expires_at = None
    elif company.subscription_expires_at is None or not subscription_is_current(company):
        # a paid plan always gets a real period: never open-ended, never already expired
        company.subscription_expires_at = datetime.now(UTC) + timedelta(days=CYCLES[body.cycle])
    company.renewal_notice_for = None
    record_event(db, "platform.company.plan_changed", actor_id=user.id, tenant_id=company.id,
                 details={"plan": plan.name, "cycle": body.cycle, "note": body.note})
    db.commit()
    return {"plan_id": plan.id, "subscription_expires_at": company.subscription_expires_at}


@router.post("/companies/{company_id}/suspend")
def suspend_company(company_id: int, body: ReasonIn, user: User = Depends(require_owner),
                    db: Session = Depends(get_db)) -> dict:
    company = _company_or_404(db, company_id)
    company.status = "inactive"
    record_event(db, "platform.company.suspended", actor_id=user.id, tenant_id=company.id, details={"reason": body.reason})
    db.commit()
    return {"status": company.status}


@router.post("/companies/{company_id}/reactivate")
def reactivate_company(company_id: int, body: ReasonIn, user: User = Depends(require_owner),
                       db: Session = Depends(get_db)) -> dict:
    company = _company_or_404(db, company_id)
    company.status = "active"
    record_event(db, "platform.company.reactivated", actor_id=user.id, tenant_id=company.id, details={"reason": body.reason})
    db.commit()
    return {"status": company.status}


@router.post("/companies/{company_id}/reset-admin-password")
def reset_admin_password(company_id: int, body: AdminResetIn, user: User = Depends(require_superadmin),
                         db: Session = Depends(get_db)) -> dict:
    company = _company_or_404(db, company_id)
    stmt = select(User).where(User.company_id == company_id, User.role == UserRole.admin)
    if body.user_id is not None:
        stmt = stmt.where(User.id == body.user_id)
    target = db.scalar(stmt.order_by(User.id))
    if target is None:
        raise HTTPException(404, "No se encontró el administrador de la empresa.")
    temp = _reset_password(db, target, user)
    db.commit()
    return {"user_id": target.id, "email": target.email, "temporary_password": temp, "company_id": company.id}


@router.post("/companies/{company_id}/revoke-sessions")
def revoke_company_sessions(company_id: int, user: User = Depends(require_superadmin),
                            db: Session = Depends(get_db)) -> dict:
    company = _company_or_404(db, company_id)
    total = 0
    for uid in db.scalars(select(User.id).where(User.company_id == company.id)).all():
        total += revoke_user_sessions(db, uid, "revoked_by_platform")
    record_event(db, "platform.company.sessions_revoked", actor_id=user.id, tenant_id=company.id, details={"sessions": total})
    db.commit()
    return {"revoked_sessions": total}


# ───────────────────────────── audit ─────────────────────────────
def _event_dict(e: SecurityEvent, names: dict, companies: dict) -> dict:
    return {"id": e.id, "occurred_at": e.occurred_at, "event_type": e.event_type, "outcome": e.outcome,
            "tenant_id": e.tenant_id, "company_name": companies.get(e.tenant_id),
            "actor_id": e.actor_id, "actor_name": names.get(e.actor_id), "subject_id": e.subject_id,
            "subject_name": names.get(e.subject_id), "client_ip": e.client_ip, "details": e.details or {}}


def _audit(db: Session, response: Response, page: Page, *, tenant_id: int | None, event_type: str | None,
           outcome: str | None, q: str | None, start: date | None, end: date | None) -> list[dict]:
    stmt = select(SecurityEvent).order_by(SecurityEvent.id.desc())
    if tenant_id is not None:
        stmt = stmt.where(SecurityEvent.tenant_id == tenant_id)
    if event_type:
        stmt = stmt.where(SecurityEvent.event_type.like(event_type.strip() + "%"))
    if outcome:
        stmt = stmt.where(SecurityEvent.outcome == outcome)
    if start:
        stmt = stmt.where(SecurityEvent.occurred_at >= datetime.combine(start, datetime.min.time(), tzinfo=UTC))
    if end:
        stmt = stmt.where(SecurityEvent.occurred_at < datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=UTC))
    if q and q.strip():
        stmt = stmt.where(or_(SecurityEvent.event_type.ilike(f"%{q.strip()}%"), SecurityEvent.client_ip.ilike(f"%{q.strip()}%")))
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    response.headers["X-Total-Count"] = str(total)
    if page.limit is not None:
        stmt = stmt.limit(page.limit).offset(page.offset)
    rows = db.scalars(stmt).all()
    ids = {i for e in rows for i in (e.actor_id, e.subject_id) if i}
    names = {u.id: u.full_name for u in db.scalars(select(User).where(User.id.in_(ids))).all()} if ids else {}
    cids = {e.tenant_id for e in rows if e.tenant_id}
    companies = {c.id: c.name for c in db.scalars(select(Company).where(Company.id.in_(cids))).all()} if cids else {}
    return [_event_dict(e, names, companies) for e in rows]


@router.get("/audit")
def global_audit(response: Response, tenant_id: int | None = None, event_type: str | None = Query(default=None, max_length=60),
                 outcome: str | None = Query(default=None, max_length=20), q: str | None = Query(default=None, max_length=60),
                 start: date | None = None, end: date | None = None, page: Page = Depends(page_params),
                 _: User = Depends(require_superadmin), db: Session = Depends(get_db)) -> list[dict]:
    return _audit(db, response, page, tenant_id=tenant_id, event_type=event_type, outcome=outcome, q=q, start=start, end=end)


@router.get("/companies/{company_id}/audit")
def company_audit(company_id: int, response: Response, event_type: str | None = Query(default=None, max_length=60),
                  outcome: str | None = Query(default=None, max_length=20), q: str | None = Query(default=None, max_length=60),
                  start: date | None = None, end: date | None = None, page: Page = Depends(page_params),
                  _: User = Depends(require_superadmin), db: Session = Depends(get_db)) -> list[dict]:
    _company_or_404(db, company_id)
    return _audit(db, response, page, tenant_id=company_id, event_type=event_type, outcome=outcome, q=q, start=start, end=end)


# ───────────────────────────── bank transfers awaiting review ─────────────────────────────
class ReviewIn(BaseModel):
    note: str = Field(default="", max_length=400)


@router.get("/transfers")
def list_transfers(response: Response, status: Literal["pending", "all"] = "pending", page: Page = Depends(page_params),
                   _: User = Depends(require_superadmin), db: Session = Depends(get_db)) -> list[dict]:
    stmt = (select(Subscription).where(Subscription.payment_method == "bank_transfer")
            .options(selectinload(Subscription.company), selectinload(Subscription.plan))
            .order_by(Subscription.created_at.desc()))
    if status == "pending":
        stmt = stmt.where(Subscription.status == SubscriptionStatus.pending)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    response.headers["X-Total-Count"] = str(total)
    if page.limit is not None:
        stmt = stmt.limit(page.limit).offset(page.offset)
    return [{**_sub_dict(s), "company_id": s.company_id, "company_name": s.company_name, "payer_note": s.payer_note or ""}
            for s in db.scalars(stmt).all()]


@router.get("/subscriptions/{subscription_id}/proof")
def transfer_proof(subscription_id: int, _: User = Depends(require_superadmin), db: Session = Depends(get_db)) -> Response:
    import base64

    s = db.get(Subscription, subscription_id)
    if s is None or not s.proof:
        raise HTTPException(404, "Comprobante no encontrado.")
    return Response(base64.b64decode(s.proof["content_base64"]), media_type=s.proof["media_type"],
                    headers={"Content-Disposition": 'attachment; filename="comprobante-suscripcion"',
                             "X-Content-Type-Options": "nosniff"})


@router.post("/subscriptions/{subscription_id}/confirm")
def confirm_transfer(subscription_id: int, body: ReviewIn, user: User = Depends(require_owner),
                     db: Session = Depends(get_db)) -> dict:
    s = db.get(Subscription, subscription_id)
    if s is None or s.payment_method != "bank_transfer":
        raise HTTPException(404, "Transferencia no encontrada.")
    if s.status != SubscriptionStatus.pending:
        raise HTTPException(409, "Esta transferencia ya fue revisada.")
    company = db.get(Company, s.company_id)
    plan = db.get(Plan, s.plan_id)
    extra = 0
    if s.coupon_code:
        from app.services.billing import find_coupon

        coupon = find_coupon(db, s.coupon_code)
        if coupon is not None and coupon.kind == "free_months":
            extra = int(coupon.value) * 30
    activate_subscription(db, company, plan, cycle=s.billing_cycle, method="bank_transfer", amount=s.amount_usd,
                          discount=s.discount_usd or Decimal("0.00"), coupon_code=s.coupon_code, extra_days=extra,
                          reference=s.reference, provider="bank_transfer", reviewed_by=user.id,
                          review_note=body.note, subscription=s)
    record_event(db, "platform.transfer.confirmed", actor_id=user.id, tenant_id=company.id,
                 details={"subscription_id": s.id, "plan": plan.name, "cycle": s.billing_cycle})
    db.commit()
    return _sub_dict(s)


@router.post("/subscriptions/{subscription_id}/reject")
def reject_transfer(subscription_id: int, body: ReviewIn, user: User = Depends(require_owner),
                    db: Session = Depends(get_db)) -> dict:
    s = db.get(Subscription, subscription_id)
    if s is None or s.payment_method != "bank_transfer":
        raise HTTPException(404, "Transferencia no encontrada.")
    if s.status != SubscriptionStatus.pending:
        raise HTTPException(409, "Esta transferencia ya fue revisada.")
    if not body.note.strip():
        raise HTTPException(422, "Indica el motivo del rechazo: la empresa lo verá.")
    s.status = SubscriptionStatus.failed
    s.reviewed_by, s.reviewed_at, s.review_note = user.id, _now(), body.note.strip()
    record_event(db, "platform.transfer.rejected", actor_id=user.id, tenant_id=s.company_id,
                 details={"subscription_id": s.id, "reason": body.note.strip()})
    db.commit()
    return _sub_dict(s)


# ───────────────────────────── renewals ─────────────────────────────
def _reminder_html(company: Company, days_left: int, expires: datetime, support: str) -> str:
    when = expires.strftime("%d/%m/%Y")
    extra = f"<p>Para ayuda escribe a {support}.</p>" if support else ""
    return (f"<p>Hola,</p><p>La suscripción de <b>{company.name}</b> vence en <b>{max(days_left, 0)} día(s)</b> "
            f"({when}). Renueva desde <b>Mi plan</b> para no perder tu plan actual.</p>{extra}")


def process_billing(db: Session, *, dry_run: bool = False) -> dict:
    """Send due renewal reminders and drop the plan of companies that stayed expired past the grace period."""
    cfg = platform_config.load(db)
    today = _now()
    reminders = sorted({int(d) for d in (cfg.get("reminder_days") or [])}, reverse=True)
    grace = int(cfg.get("grace_days") or 0)
    free = get_free_plan(db)
    notified, downgraded, skipped = [], [], []
    companies = db.scalars(
        select(Company).where(Company.subscription_expires_at.is_not(None), Company.status == "active")
        .options(selectinload(Company.plan))
    ).all()
    for company in companies:
        exp = _tz(company.subscription_expires_at)
        if exp is None or company.plan is None or company.plan.monthly_price_usd <= 0:
            continue
        days_left = (exp - today).days
        if days_left >= 0 and reminders and days_left <= max(reminders) and _tz(company.renewal_notice_for) != exp:
            admins = db.scalars(select(User).where(User.company_id == company.id, User.role == UserRole.admin,
                                                    User.status == "active")).all()
            sent = 0
            if smtp_configured() and not dry_run:
                for admin in admins:
                    try:
                        send_html_email(admin.email, f"Tu suscripción vence en {days_left} día(s)",
                                        _reminder_html(company, days_left, exp, cfg.get("support_email") or ""))
                        sent += 1
                    except Exception:
                        pass
            if dry_run or not smtp_configured():
                skipped.append({"company_id": company.id, "name": company.name, "days_left": days_left,
                                "reason": "dry_run" if dry_run else "smtp_not_configured"})
            else:
                company.renewal_notice_for = exp
                record_event(db, "billing.reminder.sent", tenant_id=company.id, details={"days_left": days_left, "to": sent})
                notified.append({"company_id": company.id, "name": company.name, "days_left": days_left, "sent": sent})
        if exp + timedelta(days=grace) < today and free is not None and company.plan_id != free.id:
            downgraded.append({"company_id": company.id, "name": company.name, "expired_on": exp.date()})
            if not dry_run:
                company.plan_id = free.id
                company.billing_cycle = None
                company.subscription_expires_at = None
                company.paypal_subscription_id = None if not company.paypal_subscription_id else company.paypal_subscription_id
                record_event(db, "billing.plan.downgraded", tenant_id=company.id, details={"to": free.name})
    if not dry_run:
        db.commit()
    return {"dry_run": dry_run, "notified": notified, "downgraded": downgraded, "skipped": skipped,
            "smtp_configured": smtp_configured()}


@router.post("/billing/process")
def run_billing(dry_run: bool = False, user: User = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    result = process_billing(db, dry_run=dry_run)
    if not dry_run:
        record_event(db, "platform.billing.processed", actor_id=user.id,
                     details={"notified": len(result["notified"]), "downgraded": len(result["downgraded"])})
        db.commit()
    return result


@router.get("/billing/upcoming")
def upcoming(days: int = Query(default=30, ge=1, le=365), _: User = Depends(require_superadmin),
             db: Session = Depends(get_db)) -> list[dict]:
    now = _now()
    horizon = now + timedelta(days=days)
    rows = db.scalars(
        select(Company).where(Company.subscription_expires_at.is_not(None), Company.status == "active",
                              Company.subscription_expires_at <= horizon).options(selectinload(Company.plan))
        .order_by(Company.subscription_expires_at)
    ).all()
    out = []
    for c in rows:
        exp = _tz(c.subscription_expires_at)
        out.append({"company_id": c.id, "name": c.name, "plan": c.plan.name if c.plan else None,
                    "billing_cycle": c.billing_cycle, "expires_at": exp, "days_left": (exp - now).days,
                    "expired": exp < now, "recurring_paypal": bool(c.paypal_subscription_id),
                    "reminded": _tz(c.renewal_notice_for) == exp})
    return out


# ───────────────────────────── revenue report ─────────────────────────────
@router.get("/reports/revenue")
def revenue_report(months: int = Query(default=12, ge=1, le=36), format: Literal["json", "xlsx"] = "json",
                   _: User = Depends(require_superadmin), db: Session = Depends(get_db)):
    today = _now().date()
    first = date(today.year, today.month, 1)
    year, month = first.year, first.month
    for _ in range(months - 1):
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    start_month = date(year, month, 1)
    subs = db.scalars(
        select(Subscription).where(Subscription.status == SubscriptionStatus.active,
                                   Subscription.created_at >= datetime.combine(start_month, datetime.min.time(), tzinfo=UTC))
        .options(selectinload(Subscription.plan))
    ).all()
    series: dict[str, dict] = {}
    cursor = start_month
    while cursor <= first:
        series[cursor.strftime("%Y-%m")] = {"month": cursor.strftime("%Y-%m"), "revenue": Decimal("0.00"), "payments": 0,
                                            "new_companies": 0}
        cursor = date(cursor.year + (cursor.month // 12), cursor.month % 12 + 1, 1)
    by_plan: dict[str, Decimal] = {}
    by_method: dict[str, Decimal] = {}
    for s in subs:
        key = s.created_at.strftime("%Y-%m")
        if key in series:
            series[key]["revenue"] += Decimal(s.amount_usd)
            series[key]["payments"] += 1
        by_plan[s.plan_name or "—"] = by_plan.get(s.plan_name or "—", Decimal("0")) + Decimal(s.amount_usd)
        m = s.payment_method or s.provider
        by_method[m] = by_method.get(m, Decimal("0")) + Decimal(s.amount_usd)
    for c in db.scalars(select(Company).where(Company.created_at >= datetime.combine(start_month, datetime.min.time(), tzinfo=UTC))).all():
        key = c.created_at.strftime("%Y-%m")
        if key in series:
            series[key]["new_companies"] += 1
    active = db.scalars(select(Company).where(Company.status == "active").options(selectinload(Company.plan))).all()
    mrr = Decimal("0.00")
    for c in active:
        if c.plan is None or c.plan.monthly_price_usd <= 0 or not subscription_is_current(c):
            continue
        if c.billing_cycle == "annual":
            mrr += plan_price(c.plan, "annual", platform_config.current(db)) / 12
        else:
            mrr += Decimal(c.plan.monthly_price_usd)
    paying = sum(1 for c in active if c.plan is not None and c.plan.monthly_price_usd > 0 and subscription_is_current(c))
    expired = sum(1 for c in active if c.plan is not None and c.plan.monthly_price_usd > 0 and not subscription_is_current(c))
    data = {
        "months": months,
        "series": [{**v, "revenue": str(v["revenue"])} for v in series.values()],
        "total_revenue": str(sum((v["revenue"] for v in series.values()), Decimal("0.00"))),
        "mrr_usd": str(mrr.quantize(Decimal("0.01"))),
        "arr_usd": str((mrr * 12).quantize(Decimal("0.01"))),
        "paying_companies": paying,
        "expired_companies": expired,
        "by_plan": [{"plan": k, "revenue": str(v)} for k, v in sorted(by_plan.items(), key=lambda kv: -kv[1])],
        "by_method": [{"method": k, "revenue": str(v)} for k, v in sorted(by_method.items(), key=lambda kv: -kv[1])],
    }
    if format == "json":
        return data
    rows = [["Mes", "Ingresos (US$)", "Pagos", "Empresas nuevas"]]
    rows += [[v["month"], v["revenue"], v["payments"], v["new_companies"]] for v in data["series"]]
    rows += [[], ["Total", data["total_revenue"]], ["MRR", data["mrr_usd"]], ["ARR", data["arr_usd"]], [],
             ["Por plan", "Ingresos (US$)"]] + [[p["plan"], p["revenue"]] for p in data["by_plan"]]
    rows += [[], ["Por método", "Ingresos (US$)"]] + [[m["method"], m["revenue"]] for m in data["by_method"]]
    return Response(build_xlsx("Ingresos", rows),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="reporte-ingresos.xlsx"'})
