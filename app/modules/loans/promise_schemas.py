"""Collection-promise API schemas (T-015). Inputs reject unknown fields: currency, status, actor, branch, assignment, note and
activity are a 422. ``promised_amount`` is a decimal string, ``promise_date`` an ISO calendar date."""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from app.modules.credit.rules import DecStr


class PromiseTermsIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    promised_amount: DecStr
    promise_date: date
    idempotency_key: str = Field(min_length=12, max_length=120)


class CreatePromiseIn(PromiseTermsIn):
    pass


class ReplacePromiseIn(PromiseTermsIn):
    pass


class CancelPromiseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=12, max_length=120)
