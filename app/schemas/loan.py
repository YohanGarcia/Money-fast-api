from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.loan import LoanStatus, PaymentFrequency
from app.schemas.customer import CustomerRead


class LoanCreate(BaseModel):
    customer_id: int
    principal_amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    interest_rate: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)
    late_fee_rate: Decimal = Field(default=Decimal("0.00"), ge=0, le=100, max_digits=5, decimal_places=2)
    grace_days: int = Field(default=0, ge=0, le=60)
    installment_count: int = Field(ge=1, le=365)
    payment_frequency: PaymentFrequency
    start_date: date
    route_name: str | None = Field(default=None, max_length=40)
    requires_promissory_note: bool = False
    auto_approve: bool = True

    @model_validator(mode="after")
    def validate_loan(self) -> "LoanCreate":
        if self.payment_frequency == PaymentFrequency.daily and self.installment_count > 180:
            raise ValueError("Un prestamo diario no debe superar 180 cuotas.")
        return self


class InstallmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sequence_number: int
    due_date: date
    principal_due: Decimal
    interest_due: Decimal
    fee_due: Decimal
    principal_paid: Decimal
    interest_paid: Decimal
    fee_paid: Decimal
    status: str


class LoanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: int
    created_by_id: int
    principal_amount: Decimal
    interest_rate: Decimal
    late_fee_rate: Decimal
    grace_days: int
    installment_count: int
    payment_frequency: PaymentFrequency
    start_date: date
    end_date: date
    principal_balance: Decimal
    interest_balance: Decimal
    late_fee_balance: Decimal
    total_amount: Decimal
    installment_amount: Decimal
    status: LoanStatus
    route_name: str | None
    requires_promissory_note: bool
    created_at: datetime
    customer: CustomerRead | None = None
    installments: list[InstallmentRead] = []
