"""Payment API schemas. Inputs reject unknown fields (a client ``tenant_id`` is a 422); amounts are decimal STRINGS."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.credit.rules import CurrencyCode, DecStr


class PaymentIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=12, max_length=120)
    amount: DecStr
    currency_code: CurrencyCode  # must be the loan's currency
    method: Literal["cash"] = "cash"  # v1: the only method
    origin: Literal["counter", "field"]
    receiving_branch_id: int  # explicit: never inferred from the loan's branches
    cash_session_id: int | None = Field(default=None, gt=0)  # required for counter, forbidden for field
    external_reference: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def _session_matches_origin(self):
        if (self.origin == "counter") != (self.cash_session_id is not None):
            raise ValueError(
                "cash_session_id es obligatorio en pagos de ventanilla (counter) y no aplica al cobro de campo (field)."
            )
        return self
