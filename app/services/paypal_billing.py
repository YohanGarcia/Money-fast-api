"""PayPal recurring billing: create the Product + monthly Plans once, and
resolve the PayPal billing-plan id for each of our paid plans.

Idempotent: the PayPal product/plan ids are cached (product in a tiny settings
row, each PayPal plan id on our Plan.paypal_plan_id). Free plans are skipped.
"""
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.app_setting import AppSetting
from app.models.plan import Plan
from app.services.paypal_client import base_url, get_access_token

_PRODUCT_KEY = "paypal_product_id"


async def _create_product(token: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{base_url()}/v1/catalogs/products",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "name": "MoneyFast SaaS",
                "description": "Suscripción mensual a MoneyFast",
                "type": "SERVICE",
                "category": "SOFTWARE",
            },
        )
        r.raise_for_status()
        return r.json()["id"]


async def _create_plan(token: str, product_id: str, plan: Plan) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{base_url()}/v1/billing/plans",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "product_id": product_id,
                "name": f"MoneyFast — Plan {plan.name}",
                "status": "ACTIVE",
                "billing_cycles": [{
                    "frequency": {"interval_unit": "MONTH", "interval_count": 1},
                    "tenure_type": "REGULAR",
                    "sequence": 1,
                    "total_cycles": 0,  # 0 = until cancelled
                    "pricing_scheme": {
                        "fixed_price": {"value": str(plan.monthly_price_usd), "currency_code": "USD"}
                    },
                }],
                "payment_preferences": {
                    "auto_bill_outstanding": True,
                    "payment_failure_threshold": 1,
                },
            },
        )
        r.raise_for_status()
        return r.json()["id"]


def _get_setting(db: Session, key: str) -> str | None:
    row = db.get(AppSetting, key)
    return row.value if row else None


def _set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value


async def ensure_billing(db: Session) -> dict:
    """Create the PayPal product and a monthly plan for each paid plan that
    doesn't have one yet. Returns a {plan_name: paypal_plan_id} map."""
    token = await get_access_token()

    product_id = _get_setting(db, _PRODUCT_KEY)
    if not product_id:
        product_id = await _create_product(token)
        _set_setting(db, _PRODUCT_KEY, product_id)
        db.commit()

    result: dict[str, str] = {}
    paid_plans = db.scalars(
        select(Plan).where(Plan.monthly_price_usd > 0, Plan.is_active.is_(True))
    ).all()
    for plan in paid_plans:
        if not plan.paypal_plan_id:
            plan.paypal_plan_id = await _create_plan(token, product_id, plan)
            db.commit()
        result[plan.name] = plan.paypal_plan_id
    return result


async def get_subscription(token: str, subscription_id: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{base_url()}/v1/billing/subscriptions/{subscription_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        return r.json()


async def cancel_subscription(token: str, subscription_id: str, reason: str = "User requested") -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{base_url()}/v1/billing/subscriptions/{subscription_id}/cancel",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"reason": reason},
        )
        # 204 = cancelled; 422 = already cancelled/inactive — treat as success.
        if r.status_code not in (204, 422):
            r.raise_for_status()
