from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CompanySettingsUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=3, max_length=160)
    tax_id: str = Field(min_length=3, max_length=40)
    phone: str = Field(min_length=7, max_length=30)
    currency_symbol: str = Field(min_length=1, max_length=8)
    # Structured address. ``address`` is composed server-side from these parts.
    province: str = Field(default="", max_length=60)
    municipality: str = Field(default="", max_length=80)
    sector: str = Field(default="", max_length=120)
    street: str = Field(default="", max_length=160)
    house_number: str = Field(default="", max_length=40)
    address_reference: str = Field(default="", max_length=255)


class CompanySettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # New and legacy companies may have an incomplete profile.
    name: str
    tax_id: str
    address: str
    province: str = ""
    municipality: str = ""
    sector: str = ""
    street: str = ""
    house_number: str = ""
    address_reference: str = ""
    phone: str
    currency_symbol: str
    id: int
    updated_at: datetime
