"""What a company sees and does about its own subscription: plans with monthly / annual prices, the payment methods
the platform owner enabled, coupon quotes, bank-transfer notices and its payment history."""
from __future__ import annotations

import base64
import binascii
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_db, require_admin
from app.core.config import settings
from app.models.company import Company
from app.models.plan import Plan
from app.models.platform import PaymentMethod
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.user import User
from app.modules.identity.audit import record_event
from app.schemas.cash import Proof
from app.services import platform_config
from app.services.billing import check_cycle, plan_price, quote
from app.services.plan_limits import subscription_is_current

router = APIRouter()

TRANSFER_TOLERANCE = Decimal("0.01")


def _company(db: Session, user: User) -> Company:
    if user.company_id is None:
        raise HTTPException(400, "El usuario no pertenece a una empresa.")
    company = db.get(Company, user.company_id)
    if company is None:
        raise HTTPException(404, "Empresa no encontrada.")
    return company


def _methods(db: Session) -> list[dict]:
    out = []
    for m in db.scalars(select(PaymentMethod).where(PaymentMethod.is_enabled.is_(True))
                        .order_by(PaymentMethod.sort_order, PaymentMethod.id)).all():
        item = {"id": m.id, "kind": m.kind, "label": m.label}
        d = m.details or {}
        if m.kind == "paypal":
            item.update(accept_account=bool(d.get("accept_account", True)), accept_card=bool(d.get("accept_card", True)),
                        recurring=bool(d.get("recurring", True)), configured=bool(settings.paypal_client_id and settings.paypal_secret),
                        mode=settings.paypal_mode, client_id=settings.paypal_client_id)
        else:
            item["bank"] = {k: d.get(k, "") for k in ("bank_name", "account_type", "account_number", "account_holder",
                                                       "holder_document", "currency", "instructions")}
        out.append(item)
    return out


def _sub_view(s: Subscription) -> dict:
    return {"id": s.id, "plan_name": s.plan_name, "amount_usd": str(s.amount_usd), "discount_usd": str(s.discount_usd or 0),
            "status": s.status.value, "payment_method": s.payment_method or s.provider, "billing_cycle": s.billing_cycle,
            "coupon_code": s.coupon_code, "reference": s.reference or "", "period_start": s.period_start,
            "period_end": s.period_end, "created_at": s.created_at, "review_note": s.review_note or ""}


@router.get("/options")
def options(user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    company = _company(db, user)
    cfg = platform_config.current(db)
    plans = db.scalars(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.monthly_price_usd, Plan.name)).all()
    out_plans = []
    for p in plans:
        monthly, annual = plan_price(p, "monthly", cfg), plan_price(p, "annual", cfg)
        saving = None
        if monthly > 0 and annual > 0:
            saving = float(round((Decimal(1) - annual / (monthly * 12)) * 100, 1))
        out_plans.append({"id": p.id, "name": p.name, "customer_limit": p.customer_limit, "loan_limit": p.loan_limit,
                          "user_limit": p.user_limit, "monthly_price_usd": str(monthly), "annual_price_usd": str(annual),
                          "annual_saving_percent": saving, "is_current": p.id == company.plan_id,
                          "recurring_monthly": bool(p.paypal_plan_id), "recurring_annual": bool(p.paypal_annual_plan_id),
                          "features": list(p.features or [])})
    exp = company.subscription_expires_at
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    pending = db.scalar(select(Subscription).where(
        Subscription.company_id == company.id, Subscription.status == SubscriptionStatus.pending,
        Subscription.payment_method == "bank_transfer").options(selectinload(Subscription.plan)))
    return {
        "current": {"plan_id": company.plan_id, "cycle": company.billing_cycle, "expires_at": exp,
                    "current": subscription_is_current(company), "recurring_paypal": bool(company.paypal_subscription_id),
                    "days_remaining": (exp - datetime.now(UTC)).days if exp else None},
        "cycles": {"monthly": bool(cfg.get("allow_monthly", True)), "annual": bool(cfg.get("allow_annual", True))},
        "plans": out_plans,
        "methods": _methods(db),
        "pending_transfer": _sub_view(pending) if pending is not None else None,
        "support": {"email": cfg.get("support_email") or "", "phone": cfg.get("support_phone") or ""},
    }


@router.get("/usage")
def usage(user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    """Use of the plan limits and the features the company's current plan includes."""
    from app.services.plan_limits import FEATURES, usage_summary

    company = _company(db, user)
    out = usage_summary(db, company.id)
    out["catalog"] = FEATURES
    return out


class QuoteIn(BaseModel):
    plan_id: int = Field(ge=1)
    cycle: Literal["monthly", "annual"] = "monthly"
    coupon_code: str | None = Field(default=None, max_length=40)


@router.post("/quote")
def get_quote(body: QuoteIn, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    _company(db, user)
    cfg = platform_config.current(db)
    plan = db.get(Plan, body.plan_id)
    if plan is None:
        raise HTTPException(404, "Plan no encontrado.")
    return quote(db, plan, check_cycle(body.cycle, cfg), cfg, body.coupon_code)


class TransferIn(QuoteIn):
    reference: str = Field(min_length=3, max_length=160)
    amount: Decimal = Field(gt=0, max_digits=8, decimal_places=2)
    paid_on: date | None = None
    note: str = Field(default="", max_length=500)
    proof: Proof | None = None


@router.post("/transfer", status_code=201)
def notify_transfer(body: TransferIn, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    company = _company(db, user)
    cfg = platform_config.current(db)
    method = db.scalar(select(PaymentMethod).where(PaymentMethod.kind == "bank_transfer", PaymentMethod.is_enabled.is_(True)))
    if method is None:
        raise HTTPException(409, "El pago por transferencia no está disponible.")
    if db.scalar(select(Subscription.id).where(Subscription.company_id == company.id, Subscription.status == SubscriptionStatus.pending,
                                               Subscription.payment_method == "bank_transfer")):
        raise HTTPException(409, "Ya tienes una transferencia en revisión. Espera su confirmación.")
    plan = db.get(Plan, body.plan_id)
    if plan is None:
        raise HTTPException(404, "Plan no encontrado.")
    q = quote(db, plan, check_cycle(body.cycle, cfg), cfg, body.coupon_code)
    total = Decimal(q["total"])
    if total <= 0 and not q["extra_days"]:
        raise HTTPException(409, "Este plan no requiere pago.")
    if abs(body.amount - total) > TRANSFER_TOLERANCE:
        raise HTTPException(422, f"El monto transferido debe ser US${total} (plan {plan.name}, {q['period_days']} días).")
    proof = None
    if body.proof is not None:
        try:
            base64.b64decode(body.proof.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(422, "El comprobante no es un archivo válido.")
        proof = body.proof.model_dump()
    today = datetime.now(UTC).date()
    sub = Subscription(
        company_id=company.id, plan_id=plan.id, amount_usd=total, currency="USD", status=SubscriptionStatus.pending,
        provider="bank_transfer", payment_method="bank_transfer", billing_cycle=body.cycle, coupon_code=(q["coupon"] or {}).get("code"),
        discount_usd=Decimal(q["discount"]), reference=body.reference.strip(),
        payer_note=(body.note.strip() + (f" · Pagado el {body.paid_on}" if body.paid_on else "")).strip(),
        proof=proof, period_start=today, period_end=today,
    )
    db.add(sub)
    record_event(db, "billing.transfer.notified", actor_id=user.id, tenant_id=company.id,
                 details={"plan": plan.name, "cycle": body.cycle, "total": q["total"], "reference": body.reference.strip()[:60]})
    db.commit()
    db.refresh(sub)
    return _sub_view(sub)


@router.post("/transfer/{subscription_id}/cancel")
def cancel_transfer(subscription_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    company = _company(db, user)
    sub = db.get(Subscription, subscription_id)
    if sub is None or sub.company_id != company.id or sub.payment_method != "bank_transfer":
        raise HTTPException(404, "Transferencia no encontrada.")
    if sub.status != SubscriptionStatus.pending:
        raise HTTPException(409, "Solo se puede cancelar una transferencia en revisión.")
    sub.status = SubscriptionStatus.cancelled
    record_event(db, "billing.transfer.cancelled", actor_id=user.id, tenant_id=company.id, details={"subscription_id": sub.id})
    db.commit()
    return _sub_view(sub)


@router.get("/history")
def history(user: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict]:
    company = _company(db, user)
    rows = db.scalars(select(Subscription).where(Subscription.company_id == company.id)
                      .options(selectinload(Subscription.plan)).order_by(Subscription.created_at.desc()).limit(60)).all()
    return [_sub_view(s) for s in rows]
