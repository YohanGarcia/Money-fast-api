"""Field custody API schemas (T-019). Inputs reject unknown fields: there is no client custodian, amount or item amount."""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.credit.rules import DecStr


class DeclareIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=12, max_length=120)
    receiving_branch_id: int = Field(gt=0)
    payment_ids: list[int] = Field(
        min_length=1, max_length=100
    )  # the custodian is the actor; amounts are the receipts'

    @field_validator("payment_ids")
    @classmethod
    def _unique_positive(cls, v: list[int]) -> list[int]:
        if any(i <= 0 for i in v) or len(set(v)) != len(v):
            raise ValueError("payment_ids deben ser positivos y sin repetir.")
        return v


class AcceptIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=12, max_length=120)
    cash_session_id: int = Field(gt=0)  # explicit: never the "latest open" session
    counted_amount: DecStr  # must equal the declared amount EXACTLY (v1: no discrepancy is ever accepted)


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=12, max_length=120)
    reason: str = Field(min_length=3, max_length=500)
    counted_amount: DecStr | None = None  # observation only: it books nothing

    @model_validator(mode="after")
    def _reason_not_blank(self):
        if len(self.reason.strip()) < 3:
            raise ValueError("El motivo debe tener al menos 3 caracteres.")
        return self


class CancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=12, max_length=120)
