"""Organization API schemas. Inputs reject unknown fields (a spoofed ``tenant_id`` is a 422)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TenantSettingsIn(_In):
    default_timezone: str | None = Field(default=None, min_length=3, max_length=64)
    base_currency_code: str | None = Field(default=None, min_length=3, max_length=3)


class BranchCreateIn(_In):
    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=140)
    address: str = Field(default="", max_length=255)
    phone: str = Field(default="", max_length=30)
    timezone_override: str | None = Field(default=None, min_length=3, max_length=64)


class BranchUpdateIn(_In):
    name: str | None = Field(default=None, min_length=1, max_length=140)
    address: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=30)
    timezone_override: str | None = Field(default=None, min_length=3, max_length=64)
    clear_timezone_override: bool = False


class CashPointCreateIn(_In):
    branch_id: int
    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=140)
    currencies: list[str] = Field(default_factory=list, max_length=20)


class CashPointCurrenciesIn(_In):
    currencies: list[str] = Field(max_length=20)


class CashPointSuspendIn(_In):
    reason: str = Field(min_length=3, max_length=500)


class CurrencyEnableIn(_In):
    code: str = Field(min_length=3, max_length=3)


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name: str
    status: str
    base_currency_code: str
    default_timezone: str
    created_at: datetime
    updated_at: datetime


class BranchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    tenant_id: int
    code: str
    name: str
    status: str
    address: str
    phone: str
    timezone_override: str | None
    created_at: datetime
    updated_at: datetime


class CashPointOut(BaseModel):
    id: int
    tenant_id: int
    branch_id: int
    code: str
    name: str
    status: str
    suspension_reason: str | None
    allowed_currencies: list[str]


class CurrencyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    name: str
    symbol: str | None
    exponent: int
    is_active: bool


class TenantCurrencyOut(BaseModel):
    code: str
    name: str
    exponent: int
    enabled: bool
    is_base: bool
    enabled_at: datetime
    disabled_at: datetime | None


class EffectiveConfigOut(BaseModel):
    tenant_id: int
    tenant_code: str
    branch_id: int | None
    timezone: str
    timezone_source: str
    base_currency: str
    enabled_currencies: list[str]
