from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CompanySettingsUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=3, max_length=160)
    tax_id: str = Field(min_length=3, max_length=40)
    address: str = Field(min_length=5, max_length=255)
    phone: str = Field(min_length=7, max_length=30)
    currency_symbol: str = Field(min_length=1, max_length=8)


class CompanySettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # New and legacy companies may have an incomplete profile.
    name: str
    tax_id: str
    address: str
    phone: str
    currency_symbol: str
    id: int
    updated_at: datetime
