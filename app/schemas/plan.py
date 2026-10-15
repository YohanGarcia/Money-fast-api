from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PlanCreate(BaseModel):
    name: str = Field(min_length=2, max_length=140)
    customer_limit: int = Field(ge=0, le=100000)
    loan_limit: int = Field(ge=0, le=100000)
    user_limit: int = Field(ge=0, le=10000)
    monthly_price_usd: Decimal = Field(ge=0, max_digits=8, decimal_places=2)
    # Yearly price; leave empty to charge 12 months minus the platform's annual discount.
    annual_price_usd: Decimal | None = Field(default=None, ge=0, max_digits=8, decimal_places=2)
    is_active: bool = True
    # None = every feature when the plan is paid, none when it is free.
    features: list[str] | None = None

    @field_validator("features")
    @classmethod
    def known_features(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        from app.services.plan_limits import FEATURES

        unknown = [v for v in value if v not in FEATURES]
        if unknown:
            raise ValueError(f"Característica desconocida: {unknown[0]}")
        return list(dict.fromkeys(value))


class PlanRead(PlanCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    paypal_plan_id: str | None = None
    paypal_annual_plan_id: str | None = None
    features: list[str] = []
    created_at: datetime
