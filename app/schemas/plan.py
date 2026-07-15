from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class PlanCreate(BaseModel):
    name: str = Field(min_length=2, max_length=140)
    customer_limit: int = Field(ge=0, le=100000)
    loan_limit: int = Field(ge=0, le=100000)
    user_limit: int = Field(ge=0, le=10000)
    monthly_price_usd: Decimal = Field(ge=0, max_digits=8, decimal_places=2)
    is_active: bool = True


class PlanRead(PlanCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    paypal_plan_id: str | None = None
    created_at: datetime
