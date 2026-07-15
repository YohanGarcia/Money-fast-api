from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.plan import Plan
from app.models.subscription import Subscription, SubscriptionStatus


def record_subscription(
    db: Session,
    company: Company,
    plan: Plan,
    order_id: str | None = None,
    provider: str = "paypal",
) -> Subscription:
    """Persist a completed subscription payment and activate the plan.

    Creates an ``active`` Subscription for a one-month period and updates the
    company's current plan and expiration. Pure DB logic (no external calls),
    so it can be reused by the PayPal confirm flow and unit-tested directly.
    """
    now = datetime.now(UTC)
    period_start = now.date()
    period_end = period_start + timedelta(days=30)

    subscription = Subscription(
        company_id=company.id,
        plan_id=plan.id,
        amount_usd=plan.monthly_price_usd,
        currency="USD",
        status=SubscriptionStatus.active,
        provider=provider,
        provider_order_id=order_id,
        period_start=period_start,
        period_end=period_end,
    )
    db.add(subscription)

    company.plan_id = plan.id
    company.subscription_expires_at = datetime.combine(period_end, datetime.min.time(), tzinfo=UTC)

    return subscription
