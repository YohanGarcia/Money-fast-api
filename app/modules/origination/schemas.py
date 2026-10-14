"""Origination API schemas. Inputs reject unknown fields (a client-supplied ``tenant_id`` is a 422).
Amounts are decimal STRINGS (a JSON number is a 422)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.credit.rules import CurrencyCode, DecStr

Frequency = Literal["daily", "weekly", "biweekly", "monthly"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ApplicationCreateIn(_In):
    customer_id: int
    product_id: int
    requested_amount: DecStr
    currency_code: CurrencyCode
    requested_term: int = Field(ge=1, le=3660)
    requested_frequency: Frequency
    origin_branch_id: int
    managing_branch_id: int | None = None


class ApplicationPatchIn(_In):
    """Draft only. Only the fields present are changed."""

    row_version: int = Field(ge=1)
    product_id: int | None = None  # re-pins the version currently in force
    requested_amount: DecStr | None = None
    currency_code: CurrencyCode | None = None
    requested_term: int | None = Field(default=None, ge=1, le=3660)
    requested_frequency: Frequency | None = None
    origin_branch_id: int | None = None
    managing_branch_id: int | None = None


class ReasonIn(_In):
    reason: str = Field(min_length=3, max_length=500)


class VerificationIn(_In):
    kind: str = Field(min_length=1, max_length=60)
    result: Literal["verified", "not_verified", "failed"]
    note: str | None = Field(default=None, max_length=500)


class EvaluationIn(_In):
    """Free-form structured evaluation. No scoring formula exists: nothing here is computed."""

    monthly_income: DecStr | None = None
    monthly_expenses: DecStr | None = None
    declared_payment_capacity: DecStr | None = None
    currency_code: CurrencyCode | None = None
    verifications: list[VerificationIn] = Field(default_factory=list, max_length=30)
    risks: list[str] = Field(default_factory=list, max_length=30)
    notes: str | None = Field(default=None, max_length=2000)
    recommendation: Literal["approve", "reject", "needs_more_information", "none"] | None = None

    @model_validator(mode="after")
    def _not_empty(self):
        if not self.model_dump(exclude_none=True, exclude_defaults=True):
            raise ValueError("La evaluacion no puede estar vacia.")
        return self


class ConditionIn(_In):
    kind: Literal["guarantee_required", "guarantor_required", "document_pending", "administrative", "other"]
    description: str = Field(min_length=3, max_length=500)
    blocks_formalization: bool  # chosen explicitly by the approver: no default


class ApproveIn(_In):
    row_version: int = Field(ge=1)  # the exact application content that was reviewed
    approved_amount: DecStr  # never inferred from requested_amount
    approved_term: int = Field(ge=1, le=3660)
    approved_frequency: Frequency
    conditions: list[ConditionIn] = Field(default_factory=list, max_length=20)
    reason: str | None = Field(default=None, max_length=500)


class RejectIn(_In):
    row_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=500)


class ConditionResolveIn(_In):
    status: Literal["fulfilled", "waived"]
    note: str | None = Field(default=None, max_length=500)


class DocumentLinkIn(_In):
    requirement: str = Field(min_length=1, max_length=80)
    reference: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=500)


class DocumentStatusIn(_In):
    status: Literal["pending", "provided", "verified", "rejected"]
    note: str | None = Field(default=None, max_length=500)


class PolicyIn(_In):
    product_id: int | None = None  # None = tenant-wide policy
    maker_checker_required: bool
    limits_enforced: bool
    approved_may_exceed_requested: bool
    evaluation_required: bool


class LimitIn(_In):
    user_id: int | None = None
    role_id: int | None = None
    currency_code: CurrencyCode
    max_amount: DecStr
    product_id: int | None = None
    branch_id: int | None = None

    @model_validator(mode="after")
    def _one_subject(self):
        if (self.user_id is None) == (self.role_id is None):
            raise ValueError("Indica `user_id` o `role_id`, no ambos.")
        return self


class ApplicationSummaryOut(BaseModel):
    id: int
    application_number: str
    customer_id: int
    product_id: int
    status: str
    requested_amount: str
    currency_code: str
    requested_term: int
    requested_frequency: str
    origin_branch_id: int
    managing_branch_id: int | None
    created_at: datetime
    submitted_at: datetime | None
