from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.cash import Proof


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
    # ``capital`` is accepted by the schema only to answer a clear refusal: new payments never draw from Capital
    source: Literal["cash", "bank", "capital"]
    idempotency_key: str | None = Field(default=None, min_length=12, max_length=80)
    bank_account_id: int | None = Field(default=None, gt=0)
    session_id: int | None = Field(default=None, gt=0)  # the open cash session to pay from (cash source)
    reference: str = Field(default="", max_length=160)  # bank operation / receipt number
    proof: Proof | None = None
    period_start: date | None = None
    period_end: date | None = None
    branch_id: int | None = None
    salary_part: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    commission_part: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    notes: str = Field(default="", max_length=2000)
