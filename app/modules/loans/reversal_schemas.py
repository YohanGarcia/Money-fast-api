"""Payment reversal API schemas. FULL reversal only: there is no amount and no component selection (extra fields are a 422)."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReversalIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=12, max_length=120)
    reason: str = Field(min_length=3, max_length=500)  # free text: no reason codes exist
    reversal_branch_id: int  # explicit; must be the payment's receiving branch (never inferred)
    cash_session_id: int | None = Field(
        default=None, gt=0
    )  # required to reverse a counter payment, forbidden for field

    @model_validator(mode="after")
    def _reason_not_blank(self):
        if len(self.reason.strip()) < 3:
            raise ValueError("El motivo debe tener al menos 3 caracteres.")
        return self
