"""Customer file persistence (T-004). Person != CustomerProfile.

* ``Person`` (identity module) holds common identity; ``CustomerProfile`` is only the *customer role* of a person
  inside one tenant: no credentials, no permissions, no debt, no employment data.
* Contacts / addresses / references hang off the profile and are never deleted: a change deactivates the old
  row and adds a new one, so evidence of earlier operations survives (DF-02 §5, §15).
* Composite foreign keys keep every reference inside the tenant at database level.
"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.shared.normalization import collapse_spaces, normalize_email, normalize_phone


def _now() -> datetime:
    return datetime.now(UTC)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


CUSTOMER_STATUSES = ("pending", "active", "inactive")  # 'restricted' is BLOCKED_BY_SPEC (DF-02 §13)
CONTACT_TYPES = ("phone", "mobile", "email", "other")
ADDRESS_TYPES = ("residence", "work", "business", "mailing", "other")
REFERENCE_KINDS = ("personal", "family", "commercial", "employer", "other")
ROW_STATUSES = ("active", "inactive")
FLAG_STATUSES = ("pending_review", "dismissed", "confirmed_duplicate")


class TenantSequence(Base):
    """Per-tenant monotonic counters (customer codes). Values are never reused."""

    __tablename__ = "tenant_sequences"

    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), primary_key=True)
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_value: Mapped[int] = mapped_column(BigInteger, default=0)


class CustomerProfile(Base):
    __tablename__ = "customer_profiles"
    __table_args__ = (
        CheckConstraint(_in("status", CUSTOMER_STATUSES), name="status_valid"),
        UniqueConstraint("tenant_id", "customer_code", name="uq_customer_profiles_tenant_code"),
        UniqueConstraint("tenant_id", "person_id", name="uq_customer_profiles_tenant_person"),
        UniqueConstraint("tenant_id", "id", name="uq_customer_profiles_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "person_id"], ["persons.tenant_id", "persons.id"], name="fk_customer_profiles_tenant_person"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "origin_branch_id"],
            ["branches.company_id", "branches.id"],
            name="fk_customer_profiles_tenant_origin_branch",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "management_branch_id"],
            ["branches.company_id", "branches.id"],
            name="fk_customer_profiles_tenant_management_branch",
        ),
        Index("ix_customer_profiles_tenant_status", "tenant_id", "status"),
        Index("ix_customer_profiles_tenant_management_branch", "tenant_id", "management_branch_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    person_id: Mapped[int] = mapped_column(Integer)
    customer_code: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    origin_branch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # where the customer was registered
    management_branch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # branch managing them now
    marital_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    internal_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Mapping to the legacy flat `customers` row when imported (TRANSFORM); NULL for new customers.
    legacy_customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True, unique=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    __mapper_args__ = {"version_id_col": version}


class _ChildMixin:
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    customer_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    inactivated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CustomerContact(_ChildMixin, Base):
    __tablename__ = "customer_contacts"
    __table_args__ = (
        CheckConstraint(_in("type", CONTACT_TYPES), name="type_valid"),
        CheckConstraint(_in("status", ROW_STATUSES), name="status_valid"),
        CheckConstraint("(status = 'inactive') = (inactivated_at IS NOT NULL)", name="inactivation_consistent"),
        CheckConstraint("NOT is_primary OR status = 'active'", name="primary_is_active"),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customer_profiles.tenant_id", "customer_profiles.id"],
            name="fk_customer_contacts_tenant_customer",
        ),
        # At most one active primary contact per type.
        Index(
            "uq_customer_contacts_primary",
            "customer_id",
            "type",
            unique=True,
            postgresql_where=text("is_primary AND status = 'active'"),
        ),
        Index("ix_customer_contacts_tenant_normalized", "tenant_id", "type", "normalized_value"),
        Index("ix_customer_contacts_customer", "customer_id"),
    )

    type: Mapped[str] = mapped_column(String(10))
    value: Mapped[str] = mapped_column(String(255))  # as entered (trimmed)
    normalized_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    label: Mapped[str | None] = mapped_column(String(60), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CustomerAddress(_ChildMixin, Base):
    """``sector`` and ``barrio`` are separate concepts and separate columns. Coordinates complement, never
    replace, the textual address."""

    __tablename__ = "customer_addresses"
    __table_args__ = (
        CheckConstraint(_in("type", ADDRESS_TYPES), name="type_valid"),
        CheckConstraint(_in("status", ROW_STATUSES), name="status_valid"),
        CheckConstraint("(status = 'inactive') = (inactivated_at IS NOT NULL)", name="inactivation_consistent"),
        CheckConstraint("NOT is_primary OR status = 'active'", name="primary_is_active"),
        CheckConstraint("(latitude IS NULL) = (longitude IS NULL)", name="coordinates_together"),
        CheckConstraint("latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180", name="coordinates_range"),
        CheckConstraint(
            "latitude IS NULL OR coalesce(street, '') <> '' OR coalesce(sector, '') <> '' "
            "OR coalesce(barrio, '') <> '' OR coalesce(municipality, '') <> '' OR coalesce(reference_note, '') <> ''",
            name="textual_address_required_with_coordinates",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customer_profiles.tenant_id", "customer_profiles.id"],
            name="fk_customer_addresses_tenant_customer",
        ),
        Index(
            "uq_customer_addresses_primary",
            "customer_id",
            unique=True,
            postgresql_where=text("is_primary AND status = 'active'"),
        ),
        Index("ix_customer_addresses_customer", "customer_id"),
    )

    type: Mapped[str] = mapped_column(String(10), default="residence")
    country: Mapped[str | None] = mapped_column(String(80), nullable=True)
    province: Mapped[str | None] = mapped_column(String(120), nullable=True)
    municipality: Mapped[str | None] = mapped_column(String(120), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(120), nullable=True)
    barrio: Mapped[str | None] = mapped_column(String(120), nullable=True)
    street: Mapped[str | None] = mapped_column(String(160), nullable=True)
    number: Mapped[str | None] = mapped_column(String(60), nullable=True)
    building: Mapped[str | None] = mapped_column(String(160), nullable=True)
    apartment: Mapped[str | None] = mapped_column(String(60), nullable=True)
    reference_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)


class CustomerReference(_ChildMixin, Base):
    """A personal/commercial reference. NOT a Person, NOT a guarantor, creates no financial responsibility."""

    __tablename__ = "customer_references"
    __table_args__ = (
        CheckConstraint(_in("kind", REFERENCE_KINDS), name="kind_valid"),
        CheckConstraint(_in("status", ROW_STATUSES), name="status_valid"),
        CheckConstraint("(status = 'inactive') = (inactivated_at IS NOT NULL)", name="inactivation_consistent"),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customer_profiles.tenant_id", "customer_profiles.id"],
            name="fk_customer_references_tenant_customer",
        ),
        Index("ix_customer_references_customer", "customer_id"),
    )

    kind: Mapped[str] = mapped_column(String(15), default="personal")
    name: Mapped[str] = mapped_column(String(160))
    relation: Mapped[str | None] = mapped_column(String(80), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    normalized_phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)


class CustomerDuplicateFlag(Base):
    """A probable duplicate surfaced for human review. Never resolved automatically and never merges anything."""

    __tablename__ = "customer_duplicate_flags"
    __table_args__ = (
        CheckConstraint(_in("status", FLAG_STATUSES), name="status_valid"),
        CheckConstraint("customer_id <> candidate_customer_id", name="distinct_customers"),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customer_profiles.tenant_id", "customer_profiles.id"],
            name="fk_customer_duplicate_flags_tenant_customer",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "candidate_customer_id"],
            ["customer_profiles.tenant_id", "customer_profiles.id"],
            name="fk_customer_duplicate_flags_tenant_candidate",
        ),
        UniqueConstraint("customer_id", "candidate_customer_id", name="uq_customer_duplicate_flags_pair"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    customer_id: Mapped[int] = mapped_column(Integer, index=True)
    candidate_customer_id: Mapped[int] = mapped_column(Integer)
    signals: Mapped[list] = mapped_column(JSONB, default=list)  # e.g. ["phone", "name_birth_date"]
    status: Mapped[str] = mapped_column(String(20), default="pending_review")
    reviewed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PersonIdentityRevision(Base):
    """Real before/after values of identity changes (the audit trail keeps only masked values).
    Distinguishes an error correction from a real change (DF-02 §16)."""

    __tablename__ = "person_identity_revisions"
    __table_args__ = (
        CheckConstraint("kind IN ('correction', 'change')", name="kind_valid"),
        ForeignKeyConstraint(
            ["tenant_id", "person_id"],
            ["persons.tenant_id", "persons.id"],
            name="fk_person_identity_revisions_tenant_person",
        ),
        Index("ix_person_identity_revisions_person", "person_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    person_id: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(12))
    reason: Mapped[str] = mapped_column(String(500))
    before: Mapped[dict] = mapped_column(JSONB)
    after: Mapped[dict] = mapped_column(JSONB)
    changed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


@event.listens_for(CustomerContact, "before_insert")
@event.listens_for(CustomerContact, "before_update")
def _derive_contact_normalized(mapper, connection, target: CustomerContact) -> None:
    target.value = collapse_spaces(target.value)
    if target.type in ("phone", "mobile"):
        target.normalized_value = normalize_phone(target.value)
    elif target.type == "email":
        target.normalized_value = normalize_email(target.value)
    else:
        target.normalized_value = collapse_spaces(target.value).lower() or None


@event.listens_for(CustomerReference, "before_insert")
@event.listens_for(CustomerReference, "before_update")
def _derive_reference_normalized(mapper, connection, target: CustomerReference) -> None:
    target.normalized_phone = normalize_phone(target.phone)
