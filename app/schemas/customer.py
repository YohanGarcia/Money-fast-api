from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class CustomerBase(BaseModel):
    full_name: str = Field(min_length=3, max_length=160)
    document_id: str | None = Field(default=None, max_length=30)
    phone: str = Field(min_length=7, max_length=30)
    address: str = Field(min_length=5, max_length=255)
    notes: str | None = None
    latitude: Decimal | None = Field(default=None, ge=-90, le=90, max_digits=9, decimal_places=6)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180, max_digits=9, decimal_places=6)


class CustomerCreate(CustomerBase):
    route_id: int | None = None
    # Only used when the customer has no route (direct assignment fallback).
    assigned_collector_id: int | None = None


class CustomerUpdate(CustomerCreate):
    pass


class CustomerRead(CustomerBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_by_id: int
    route_id: int | None = None
    route_name: str | None = None
    assigned_collector_id: int | None = None
    collector_name: str | None = None
    created_at: datetime
