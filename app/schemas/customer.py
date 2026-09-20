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
    sector: str | None = Field(default=None, max_length=120)
    calle: str | None = Field(default=None, max_length=120)
    barrio: str | None = Field(default=None, max_length=120)
    province: str | None = Field(default=None, max_length=120)
    house_number: str | None = Field(default=None, max_length=60)
    building: str | None = Field(default=None, max_length=160)
    apartment: str | None = Field(default=None, max_length=60)
    reference_note: str | None = None
    notes: str | None = None
    email: EmailStr | None = None
    home_phone: str | None = Field(default=None, max_length=30)
    birth_date: str | None = None
    marital_status: str | None = Field(default=None, max_length=30)
    nationality: str | None = Field(default=None, max_length=160)
    city: str | None = Field(default=None, max_length=160)
    references: list[CustomerReference] = Field(default_factory=list, max_length=3)

    @field_validator("email", mode="before")
    @classmethod
    def clean_email(cls, value):
        """Normaliza el correo antes de validarlo con EmailStr.

        Quita espacios y comas/punto y coma sobrantes al inicio/final (typeo común
        al escribir o pegar el correo) y convierte "" en None. Si quedan varias
        direcciones separadas por coma, se rechaza con un mensaje claro en vez del
        mensaje técnico por defecto de pydantic.
        """
        if value is None:
            return value
        if isinstance(value, str):
            value = value.strip().strip(",;").strip()
            if value == "":
                return None
            if "," in value or ";" in value:
                raise ValueError("Ingresa un solo correo electrónico, sin comas.")
        return value

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
