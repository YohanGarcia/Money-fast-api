from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.models.subscription import SubscriptionStatus


class SubscriptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company_id: int
    plan_id: int
    company_name: str | None = None
    plan_name: str | None = None
    amount_usd: Decimal
    currency: str
    status: SubscriptionStatus
    provider: str
    provider_order_id: str | None
    period_start: date
    period_end: date
    created_at: datetime


class PlatformOverview(BaseModel):
    total_companies: int
    active_companies: int
    total_users: int
    mrr_usd: Decimal
    revenue_this_month_usd: Decimal
    expiring_soon: list["ExpiringCompany"]


class ExpiringCompany(BaseModel):
    id: int
    name: str
    subscription_expires_at: datetime | None


PlatformOverview.model_rebuild()
