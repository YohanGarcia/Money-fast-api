from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, EmailStr, field_validator
from datetime import date


class CustomerReference(BaseModel):
    nombre: str = Field(max_length=160)
    telefono: str = Field(default="", max_length=30)
    cedula: str = Field(default="", max_length=30)
    direccion: str = Field(default="", max_length=255)


class CustomerBase(BaseModel):
    full_name: str = Field(min_length=3, max_length=160)
    document_id: str | None = Field(default=None, max_length=30)
    phone: str = Field(min_length=7, max_length=30)
    address: str = Field(min_length=5, max_length=255)
    notes: str | None = None
    email: EmailStr | None = None
    home_phone: str | None = Field(default=None, max_length=30)
    birth_date: str | None = None
    marital_status: str | None = Field(default=None, max_length=30)
    nationality: str | None = Field(default=None, max_length=160)
    city: str | None = Field(default=None, max_length=160)
    references: list[CustomerReference] = Field(default_factory=list, max_length=3)

    @field_validator("birth_date")
    @classmethod
    def valid_birth_date(cls, value):
        if value and date.fromisoformat(value) > date.today():
            raise ValueError("Fecha de nacimiento futura.")
        return value
    latitude: Decimal | None = Field(default=None, ge=-90, le=90, max_digits=9, decimal_places=6)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180, max_digits=9, decimal_places=6)


class CustomerCreate(CustomerBase):
    route_id: int | None = None
    # Only used when the customer has no route (direct assignment fallback).
    assigned_collector_id: int | None = None


class CustomerUpdate(CustomerCreate):
    version: int = Field(ge=1)


class CustomerRead(CustomerBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version: int
    created_by_id: int
    route_id: int | None = None
    route_name: str | None = None
    assigned_collector_id: int | None = None
    collector_name: str | None = None
    cash_branch_id: int | None = None
    created_at: datetime
