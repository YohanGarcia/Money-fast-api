"""Credit product API schemas. Inputs reject unknown fields (a client-supplied ``tenant_id`` is a 422)."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.credit.rules import CurrencyCode, CurrencyLimitIn, DecStr, RulesIn


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProductCreateIn(_In):
    code: str = Field(min_length=2, max_length=30)
    name: str = Field(min_length=1, max_length=140)
    description: str | None = Field(default=None, max_length=1000)


class VersionCreateIn(_In):
    rules: RulesIn | None = None
    currencies: list[CurrencyLimitIn] | None = Field(default=None, max_length=10)
    based_on_version_id: int | None = None  # copy rules + currencies from an existing version of the product

    @model_validator(mode="after")
    def _one_source(self):
        if (self.rules is None) == (self.based_on_version_id is None):
            raise ValueError("Indica `rules` (version nueva) o `based_on_version_id` (copia), no ambos.")
        if self.based_on_version_id is not None and self.currencies is not None:
            raise ValueError("Una copia hereda las monedas de la version base.")
        return self


class VersionUpdateIn(_In):
    row_version: int = Field(ge=1)
    rules: RulesIn | None = None
    currencies: list[CurrencyLimitIn] | None = Field(default=None, max_length=10)


class PublishIn(_In):
    row_version: int = Field(ge=1)
    effective_from: date


class ReasonIn(_In):
    reason: str = Field(min_length=3, max_length=500)


class SimulateIn(_In):
    currency: CurrencyCode
    principal: DecStr
    term_periods: int = Field(ge=1, le=3660)
    start_date: date | None = None
    start_at: datetime | None = None  # an aware instant: the business date is derived in the product's timezone

    @field_validator("start_at")
    @classmethod
    def _aware(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("start_at requiere zona horaria (offset).")
        return value

    @model_validator(mode="after")
    def _one_start(self):
        if (self.start_date is None) == (self.start_at is None):
            raise ValueError("Indica `start_date` o `start_at`, no ambos.")
        return self


class CurrencyLimitOut(BaseModel):
    code: str
    min_amount: str
    max_amount: str


class VersionSummaryOut(BaseModel):
    id: int
    version_number: int
    status: str
    effective_from: date | None
    effective_to: date | None
    rules_hash: str | None
    published_at: datetime | None
    validated: bool
    row_version: int


class VersionOut(VersionSummaryOut):
    product_id: int
    tenant_id: int
    rules: dict
    currencies: list[CurrencyLimitOut]
    created_at: datetime
    updated_at: datetime


class ProductSummaryOut(BaseModel):
    id: int
    code: str
    name: str
    status: str
    current_version: VersionSummaryOut | None


class ProductOut(BaseModel):
    id: int
    tenant_id: int
    code: str
    name: str
    description: str | None
    status: str
    row_version: int
    created_at: datetime
    updated_at: datetime
    versions: list[VersionSummaryOut]


class IssueOut(BaseModel):
    path: str
    code: str
    message: str


class ValidationOut(BaseModel):
    valid: bool
    issues: list[IssueOut]
    warnings: list[str]
    rules_hash: str | None


class SnapshotOut(BaseModel):
    snapshot: dict
    rules_hash: str
    hash_verified: bool
