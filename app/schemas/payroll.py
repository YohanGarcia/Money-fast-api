from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CommissionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base: Literal["collections", "interest", "disbursements"]
    percent: Decimal = Field(gt=0, le=100, max_digits=5, decimal_places=2)


class PayrollConfigUpdate(BaseModel):
    salary_amount: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    salary_frequency: Literal["weekly", "biweekly", "monthly"] = "monthly"
    commissions: list[CommissionRule] = Field(default_factory=list, max_length=10)


class PayrollPaymentCreate(BaseModel):
    user_id: int
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    source: Literal["cash", "capital"]
    period_start: date | None = None
    period_end: date | None = None
    branch_id: int | None = None
    salary_part: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    commission_part: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    notes: str = Field(default="", max_length=2000)
