from datetime import datetime
from typing import Literal
from app.schemas.cash import Proof
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.payment import PaymentType


class PaymentCreate(BaseModel):
    method: Literal['cash','transfer'] | None = None
    origin: Literal['field','counter'] | None = None
    branch_id: int | None = None
    idempotency_key: str | None = Field(default=None, min_length=12, max_length=80)
    bank_destination: str | None = Field(default=None, max_length=160)
    proof: Proof | None = None
    loan_id: int
    payment_type: PaymentType | None = None
    amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    installment_amount: Decimal = Field(default=Decimal("0.00"), ge=0, max_digits=12, decimal_places=2)
    principal_amount: Decimal = Field(default=Decimal("0.00"), ge=0, max_digits=12, decimal_places=2)
    interest_amount: Decimal = Field(default=Decimal("0.00"), ge=0, max_digits=12, decimal_places=2)
    custom_amount: Decimal = Field(default=Decimal("0.00"), ge=0, max_digits=12, decimal_places=2)
    notes: str | None = None
    reference_code: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_shape(self) -> "PaymentCreate":
        split_total = (
            self.installment_amount
            + self.principal_amount
            + self.interest_amount
            + self.custom_amount
        )

        if self.amount is not None:
            if self.payment_type is None:
                raise ValueError("payment_type es requerido cuando se envia amount.")
            if split_total > 0:
                raise ValueError("No mezcles amount con componentes de pago.")
            return self

        if split_total <= 0:
            raise ValueError("Debes enviar amount o al menos un componente de pago.")

        return self


class PaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    loan_id: int
    collected_by_id: int
    payment_type: PaymentType
    amount: Decimal
    principal_applied: Decimal
    interest_applied: Decimal
    late_fee_applied: Decimal
    notes: str | None
    paid_at: datetime
    reference_code: str | None
    collector_name: str | None = None
    method: str | None = None
    origin: str | None = None
    branch_id: int | None = None
    cash_state: str = 'historical'
    status: str = 'confirmed'
