from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CustomerBase(BaseModel):
    full_name: str = Field(min_length=3, max_length=160)
    document_id: str | None = Field(default=None, max_length=30)
    phone: str = Field(min_length=7, max_length=30)
    address: str = Field(min_length=5, max_length=255)
    notes: str | None = None


class CustomerCreate(CustomerBase):
    pass


class CustomerRead(CustomerBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_by_id: int
    created_at: datetime
