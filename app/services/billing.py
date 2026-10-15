"""Subscription pricing, coupons and activation (monthly / annual, PayPal / bank transfer / manual)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.plan import Plan
from app.models.platform import Coupon
from app.models.subscription import Subscription, SubscriptionStatus

CYCLES = {"monthly": 30, "annual": 365}
CENT = Decimal("0.01")


def q(value: Decimal) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def check_cycle(cycle: str, cfg: dict) -> str:
    if cycle not in CYCLES:
        raise HTTPException(422, "El ciclo de pago debe ser mensual o anual.")
    if cycle == "monthly" and not cfg.get("allow_monthly", True):
        raise HTTPException(409, "El pago mensual no está disponible.")
    if cycle == "annual" and not cfg.get("allow_annual", True):
        raise HTTPException(409, "El pago anual no está disponible.")
    return cycle


def plan_price(plan: Plan, cycle: str, cfg: dict) -> Decimal:
    """List price of a plan for a cycle. Annual = the explicit annual price, else 12 x monthly less the discount."""
    if cycle == "annual":
        if plan.annual_price_usd is not None:
            return q(plan.annual_price_usd)
        discount = Decimal(str(cfg.get("annual_discount_percent") or 0))
        return q(plan.monthly_price_usd * 12 * (Decimal(100) - discount) / Decimal(100))
    return q(plan.monthly_price_usd)


def _pending_uses(db: Session, code: str) -> int:
    return db.scalar(
        select(func.count()).select_from(Subscription).where(
            Subscription.coupon_code == code, Subscription.status == SubscriptionStatus.pending
        )
    ) or 0


def find_coupon(db: Session, code: str | None) -> Coupon | None:
    if not code or not code.strip():
        return None
    return db.scalar(select(Coupon).where(Coupon.code == code.strip().upper()))


def check_coupon(db: Session, coupon: Coupon | None, plan: Plan, cycle: str, today: date | None = None) -> Coupon:
    today = today or datetime.now(UTC).date()
    if coupon is None or not coupon.is_active:
        raise HTTPException(404, "El cupón no existe o no está activo.")
    if coupon.valid_from and today < coupon.valid_from:
        raise HTTPException(409, "El cupón todavía no está vigente.")
    if coupon.valid_until and today > coupon.valid_until:
        raise HTTPException(409, "El cupón venció.")
    if coupon.plan_id is not None and coupon.plan_id != plan.id:
        raise HTTPException(409, "El cupón no aplica a este plan.")
    if coupon.billing_cycle is not None and coupon.billing_cycle != cycle:
        raise HTTPException(409, f"El cupón solo aplica al pago {'anual' if coupon.billing_cycle == 'annual' else 'mensual'}.")
    if coupon.max_redemptions is not None and coupon.redeemed_count + _pending_uses(db, coupon.code) >= coupon.max_redemptions:
        raise HTTPException(409, "El cupón ya alcanzó su límite de usos.")
    return coupon


def quote(db: Session, plan: Plan, cycle: str, cfg: dict, coupon_code: str | None = None) -> dict:
    """What a company would pay for a plan and cycle, with the coupon applied (server side, never trusted from the client)."""
    check_cycle(cycle, cfg)
    if plan is None or not plan.is_active:
        raise HTTPException(404, "El plan no está disponible.")
    list_price = plan_price(plan, cycle, cfg)
    discount = Decimal("0.00")
    extra_days = 0
    applied = None
    coupon = find_coupon(db, coupon_code)
    if coupon_code and coupon_code.strip():
        coupon = check_coupon(db, coupon, plan, cycle)
        if coupon.kind == "percent":
            discount = q(list_price * min(Decimal(coupon.value), Decimal(100)) / Decimal(100))
        elif coupon.kind == "fixed_usd":
            discount = q(min(Decimal(coupon.value), list_price))
        elif coupon.kind == "free_months":
            extra_days = int(coupon.value) * 30
        applied = {"code": coupon.code, "kind": coupon.kind, "value": str(coupon.value), "description": coupon.description}
    total = q(max(list_price - discount, Decimal("0.00")))
    return {
        "plan_id": plan.id, "plan_name": plan.name, "cycle": cycle, "period_days": CYCLES[cycle] + extra_days,
        "list_price": str(list_price), "discount": str(discount), "total": str(total), "extra_days": extra_days,
        "coupon": applied, "currency": "USD",
    }


def activate_subscription(
    db: Session,
    company: Company,
    plan: Plan,
    *,
    cycle: str = "monthly",
    method: str = "manual",
    amount: Decimal | None = None,
    discount: Decimal = Decimal("0.00"),
    coupon_code: str | None = None,
    extra_days: int = 0,
    reference: str = "",
    order_id: str | None = None,
    provider: str = "paypal",
    reviewed_by: int | None = None,
    review_note: str = "",
    subscription: Subscription | None = None,
    days: int | None = None,
) -> Subscription:
    """Activate (or renew) a plan. A renewal of the same plan stacks on the current expiry; a plan change starts now."""
    now = datetime.now(UTC)
    current = company.subscription_expires_at
    if current is not None and current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    start_dt = current if (current is not None and current > now and company.plan_id == plan.id) else now
    length = (days if days is not None else CYCLES.get(cycle, 30)) + extra_days
    end_dt = start_dt + timedelta(days=length)
    list_price = plan.annual_price_usd if (cycle == "annual" and plan.annual_price_usd is not None) else plan.monthly_price_usd
    paid = amount if amount is not None else q(Decimal(list_price) - discount)
    sub = subscription or Subscription(company_id=company.id, plan_id=plan.id)
    sub.plan_id = plan.id
    sub.amount_usd = paid
    sub.currency = "USD"
    sub.status = SubscriptionStatus.active
    sub.provider = provider
    sub.provider_order_id = order_id if order_id is not None else sub.provider_order_id
    sub.period_start = start_dt.date()
    sub.period_end = end_dt.date()
    sub.billing_cycle = cycle
    sub.payment_method = method
    sub.coupon_code = coupon_code
    sub.discount_usd = discount
    sub.reference = reference or sub.reference or ""
    if reviewed_by is not None:
        sub.reviewed_by = reviewed_by
        sub.reviewed_at = now
        sub.review_note = review_note
    if subscription is None:
        db.add(sub)
    company.plan_id = plan.id
    company.subscription_expires_at = datetime.combine(end_dt.date(), datetime.min.time(), tzinfo=UTC)
    company.billing_cycle = cycle
    company.renewal_notice_for = None
    if coupon_code:
        coupon = find_coupon(db, coupon_code)
        if coupon is not None:
            coupon.redeemed_count = (coupon.redeemed_count or 0) + 1
    return sub
