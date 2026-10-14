"""Cash core session API schemas (T-021). Inputs reject unknown fields; the owner is always the authenticated actor."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from app.modules.credit.rules import DecStr


class OpenIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=12, max_length=120)
    cash_point_id: int = Field(gt=0)
    # anonymous opening cash does not exist; ``initial`` (FM-SIMPLE-CASH-010) is a declared amount with no capital
    source: Literal["zero", "initial"]
    amount: DecStr = "0"  # the declared initial amount (0 for a zero opening)
    denominations: dict[str, StrictInt] = Field(default_factory=dict)  # not used by the openings that remain
    observation_note: str | None = Field(default=None, max_length=2000)


class CloseIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=12, max_length=120)
    # the physical count is required for the legacy flow; a SIMPLE session (opened with ``initial``) sends ``counted`` instead
    denominations: dict[str, StrictInt] = Field(default_factory=dict)
    counted: DecStr | None = None  # FM-SIMPLE-CASH-010: money counted at a simple close (any difference is allowed)
    observation_note: str | None = Field(default=None, max_length=2000)  # required when counted != expected
    receiver_user_id: int | None = Field(default=None, gt=0)  # required when there is cash to hand over
    # the counted cash always goes to ``next_session`` (a named cashier of the SAME CashPoint takes it)
    destination: Literal["next_session"] = "next_session"

    @model_validator(mode="after")
    def _one_way_to_count(self):
        if self.counted is None and not self.denominations:
            raise ValueError("el conteo por denominaciones es obligatorio")
        if self.counted is not None and (self.denominations or self.receiver_user_id is not None):
            raise ValueError("el cierre simple solo recibe el dinero contado")
        return self


class ResolveDifferenceIn(BaseModel):
    """T-022A. ``reason`` and ``reference`` are validated by the service so their errors carry stable codes."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=12, max_length=120)
    resolution_type: Literal["no_further_action", "accepted_loss", "accepted_surplus"]
    reason: str = Field(default="", max_length=2000)
    reference: str | None = Field(default=None, max_length=160)


class AcceptSessionHandoverIn(BaseModel):
    """T-023A. The receiver recounts the cash: a fresh, full denomination map whose total must equal the handover."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=12, max_length=120)
    denominations: dict[str, StrictInt] = Field(min_length=1)


class DeclineSessionHandoverIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=12, max_length=120)
    reason: str = Field(default="", max_length=2000)


class RedirectSessionHandoverIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=12, max_length=120)
    destination: Literal["next_session"]
    receiver_user_id: int = Field(gt=0)
    reason: str = Field(default="", max_length=2000)
