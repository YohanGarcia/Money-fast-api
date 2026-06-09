from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LoanTermUnit = Literal["Semanas", "Meses", "Dias"]


class LoanSettingsUpdate(BaseModel):
    minimum_principal: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    maximum_principal: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    default_interest_rate: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)
    minimum_term_count: int = Field(ge=1, le=365)
    minimum_term_unit: LoanTermUnit
    default_late_fee_rate: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)
    default_grace_days: int = Field(ge=0, le=60)
    require_route_assignment: bool
    allow_payment_date_change: bool

    @model_validator(mode="after")
    def validate_amounts(self) -> "LoanSettingsUpdate":
        if self.maximum_principal < self.minimum_principal:
            raise ValueError("El balance maximo no puede ser menor que el minimo.")
        return self


class LoanSettingsRead(LoanSettingsUpdate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    updated_at: datetime
