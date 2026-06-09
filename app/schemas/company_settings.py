from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CompanySettingsUpdate(BaseModel):
    name: str = Field(min_length=3, max_length=160)
    tax_id: str = Field(min_length=3, max_length=40)
    address: str = Field(min_length=5, max_length=255)
    phone: str = Field(min_length=7, max_length=30)
    currency_symbol: str = Field(min_length=1, max_length=8)


class CompanySettingsRead(CompanySettingsUpdate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    updated_at: datetime
