"""Customer API schemas. Inputs reject unknown fields (an external ``tenant_id`` is a 422)."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.shared.normalization import normalize_document, normalize_document_type


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IdentityIn(_In):
    given_names: str = Field(min_length=1, max_length=120)
    family_names: str = Field(default="", max_length=120)
    alias: str | None = Field(default=None, max_length=80)
    document_type: str | None = Field(default=None, max_length=30)
    document_number: str | None = Field(default=None, max_length=40)
    document_country: str | None = Field(default=None, max_length=80)
    document_issue_date: date | None = None
    document_expiry_date: date | None = None
    birth_date: date | None = None
    nationality: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def _document_rules(self):
        if self.document_number is not None and normalize_document(self.document_number) is None:
            raise ValueError("El numero de documento no contiene caracteres validos.")
        if self.document_number is not None and normalize_document_type(self.document_type) is None:
            raise ValueError("El tipo de documento es obligatorio cuando se indica el numero.")
        if (
            self.document_issue_date
            and self.document_expiry_date
            and self.document_expiry_date < self.document_issue_date
        ):
            raise ValueError("La fecha de vencimiento no puede ser anterior a la de emision.")
        return self


class ContactIn(_In):
    type: Literal["phone", "mobile", "email", "other"]
    value: str = Field(min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=60)
    is_primary: bool = False


class AddressIn(_In):
    type: Literal["residence", "work", "business", "mailing", "other"] = "residence"
    country: str | None = Field(default=None, max_length=80)
    province: str | None = Field(default=None, max_length=120)
    municipality: str | None = Field(default=None, max_length=120)
    sector: str | None = Field(default=None, max_length=120)
    barrio: str | None = Field(default=None, max_length=120)
    street: str | None = Field(default=None, max_length=160)
    number: str | None = Field(default=None, max_length=60)
    building: str | None = Field(default=None, max_length=160)
    apartment: str | None = Field(default=None, max_length=60)
    reference_note: str | None = Field(default=None, max_length=1000)
    postal_code: str | None = Field(default=None, max_length=20)
    latitude: Decimal | None = Field(default=None, ge=-90, le=90, max_digits=9, decimal_places=6)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180, max_digits=9, decimal_places=6)
    is_primary: bool = False


class ReferenceIn(_In):
    kind: Literal["personal", "family", "commercial", "employer", "other"] = "personal"
    name: str = Field(min_length=1, max_length=160)
    relation: str | None = Field(default=None, max_length=80)
    phone: str | None = Field(default=None, max_length=30)
    notes: str | None = Field(default=None, max_length=500)


class CustomerCreateIn(_In):
    identity: IdentityIn | None = None
    person_id: int | None = None  # reuse an existing Person of the tenant that is not a customer yet
    customer_code: str | None = Field(default=None, min_length=1, max_length=20)
    origin_branch_id: int | None = None
    management_branch_id: int | None = None
    marital_status: str | None = Field(default=None, max_length=30)
    internal_note: str | None = Field(default=None, max_length=1000)
    contacts: list[ContactIn] = Field(default_factory=list, max_length=10)
    addresses: list[AddressIn] = Field(default_factory=list, max_length=5)
    references: list[ReferenceIn] = Field(default_factory=list, max_length=10)
    acknowledge_possible_duplicates: bool = False

    @model_validator(mode="after")
    def _exactly_one_identity_source(self):
        if (self.identity is None) == (self.person_id is None):
            raise ValueError("Indica `identity` (persona nueva) o `person_id` (persona existente), no ambos.")
        return self


class CustomerPatchIn(_In):
    version: int = Field(ge=1)
    marital_status: str | None = Field(default=None, max_length=30)
    internal_note: str | None = Field(default=None, max_length=1000)
    alias: str | None = Field(default=None, max_length=80)


class IdentityCorrectionIn(_In):
    version: int = Field(ge=1)
    kind: Literal["correction", "change"]  # error fix vs real change (DF-02 §16)
    reason: str = Field(min_length=3, max_length=500)
    given_names: str | None = Field(default=None, min_length=1, max_length=120)
    family_names: str | None = Field(default=None, max_length=120)
    document_type: str | None = Field(default=None, max_length=30)
    document_number: str | None = Field(default=None, max_length=40)
    document_country: str | None = Field(default=None, max_length=80)
    document_issue_date: date | None = None
    document_expiry_date: date | None = None
    birth_date: date | None = None
    nationality: str | None = Field(default=None, max_length=80)


class BranchAssignIn(_In):
    version: int = Field(ge=1)
    management_branch_id: int | None


class DuplicateCheckIn(_In):
    document_type: str | None = Field(default=None, max_length=30)
    document_number: str | None = Field(default=None, max_length=40)
    given_names: str | None = Field(default=None, max_length=120)
    family_names: str | None = Field(default=None, max_length=120)
    birth_date: date | None = None
    phones: list[str] = Field(default_factory=list, max_length=5)
    emails: list[str] = Field(default_factory=list, max_length=5)


class FlagReviewIn(_In):
    resolution: Literal["dismissed", "confirmed_duplicate"]
    note: str = Field(min_length=3, max_length=500)


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    type: str
    value: str
    label: str | None
    is_primary: bool
    status: str
    verified_at: datetime | None
    created_at: datetime


class AddressOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    type: str
    country: str | None
    province: str | None
    municipality: str | None
    sector: str | None
    barrio: str | None
    street: str | None
    number: str | None
    building: str | None
    apartment: str | None
    reference_note: str | None
    postal_code: str | None
    latitude: Decimal | None
    longitude: Decimal | None
    is_primary: bool
    status: str
    created_at: datetime


class ReferenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    name: str
    relation: str | None
    phone: str | None
    notes: str | None
    status: str
    created_at: datetime


class PersonOut(BaseModel):
    id: int
    given_names: str
    family_names: str
    alias: str | None
    nationality: str | None
    document_type: str | None
    document_number: str | None  # masked unless the caller holds customers.read_sensitive
    document_masked: bool
    document_country: str | None = None
    document_issue_date: date | None = None
    document_expiry_date: date | None = None
    birth_date: date | None = None  # sensitive


class CustomerSummaryOut(BaseModel):
    id: int
    customer_code: str
    display_name: str
    status: str
    origin_branch_id: int | None
    management_branch_id: int | None
    document_masked: str | None


class CustomerDetailOut(BaseModel):
    id: int
    tenant_id: int
    customer_code: str
    status: str
    origin_branch_id: int | None
    management_branch_id: int | None
    marital_status: str | None
    internal_note: str | None
    version: int
    person: PersonOut
    contacts: list[ContactOut]
    addresses: list[AddressOut]
    references: list[ReferenceOut]
    duplicate_flags_pending: int
    created_at: datetime
    updated_at: datetime


class CandidateOut(BaseModel):
    person_id: int | None
    customer_id: int | None
    customer_code: str | None
    signals: list[str]
    restricted: bool = False  # the caller may not see this customer (outside their branch scope)


class DuplicateResultOut(BaseModel):
    classification: str
    exact: list[CandidateOut]
    possible: list[CandidateOut]


class FlagOut(BaseModel):
    id: int
    customer_id: int
    candidate: CandidateOut
    signals: list[str]
    status: str
    created_at: datetime
    reviewed_at: datetime | None
    review_note: str | None
