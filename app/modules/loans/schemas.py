"""Loan API schemas. Inputs reject unknown fields (a client-supplied ``tenant_id`` is a 422): there is no amount field,
the disbursed amount comes from the confirmed movement."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FundingSourceIn(_In):
    """The money comes from ONE explicit source. Bank/provider sources are BLOCKED_BY_EVIDENCE (no bank ledger or
    provider integration exists), so the only accepted type today is an open cash custody session."""

    type: Literal["cash_session"]
    session_id: int


class DisburseIn(_In):
    idempotency_key: str = Field(min_length=12, max_length=120)
    disbursement_branch_id: int  # explicit: never inferred from the origin or managing branch
    funding_source: FundingSourceIn
