from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BankAccountCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    bank_name: str = Field(min_length=2, max_length=120)
    account_number: str = Field(min_length=3, max_length=60)
    account_holder: str = Field(min_length=2, max_length=140)


class BankAccountUpdate(BankAccountCreate):
    is_active: bool = True


class BankAccountRead(BankAccountUpdate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    created_at: datetime
