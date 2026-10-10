"""Cash core session API schemas (T-021). Inputs reject unknown fields; the owner is always the authenticated actor."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.modules.credit.rules import DecStr


class OpenIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=12, max_length=120)
    cash_point_id: int = Field(gt=0)
    source: Literal["zero", "capital"]  # anonymous opening cash does not exist
    amount: DecStr = "0"  # the capital fund (0 for a zero opening)
    denominations: dict[str, StrictInt] = Field(default_factory=dict)  # physical count; required for a capital opening
    observation_note: str | None = Field(default=None, max_length=2000)


class CloseIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=12, max_length=120)
    denominations: dict[str, StrictInt] = Field(min_length=1)  # the physical count is always required
    observation_note: str | None = Field(default=None, max_length=2000)  # required when counted != expected
    receiver_user_id: int | None = Field(default=None, gt=0)  # required when there is cash to hand over
    # T-023A: ``capital`` (default, the T-021 flow) or ``next_session`` (a named cashier of the SAME CashPoint takes it)
    destination: Literal["capital", "next_session"] = "capital"


class AcceptHandoverIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=12, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)


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
    destination: Literal["next_session", "capital"]
    receiver_user_id: int = Field(gt=0)
    reason: str = Field(default="", max_length=2000)
