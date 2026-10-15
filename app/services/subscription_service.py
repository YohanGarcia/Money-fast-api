from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.plan import Plan
from app.models.subscription import Subscription
from app.services.billing import activate_subscription


def record_subscription(
    db: Session,
    company: Company,
    plan: Plan,
    order_id: str | None = None,
    provider: str = "paypal",
    *,
    cycle: str = "monthly",
    method: str | None = None,
    amount: Decimal | None = None,
    discount: Decimal = Decimal("0.00"),
    coupon_code: str | None = None,
    extra_days: int = 0,
) -> Subscription:
    """Persist a completed subscription payment and activate the plan.

    Creates an ``active`` Subscription for the paid period (a month or a year, plus the coupon's free days) and
    updates the company's plan and expiry: a renewal of the same plan stacks on the current expiry. Pure DB logic
    (no external calls), so it is reused by the PayPal flows, the bank-transfer confirmation and the tests.
    """
    return activate_subscription(
        db,
        company,
        plan,
        cycle=cycle,
        method=method or ("paypal_subscription" if (order_id or "").startswith("I-") else provider),
        amount=amount,
        discount=discount,
        coupon_code=coupon_code,
        extra_days=extra_days,
        order_id=order_id,
        provider=provider,
    )
