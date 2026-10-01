"""Credit product persistence (T-005): CreditProduct -> CreditProductVersion -> CreditProductCurrency.

A product is a tenant-scoped *template* identified by ``code``. Its financial rules live in versions. A version is
a draft until it is published; from then on its rules, currencies, hash and snapshot are immutable (application
checks AND a database trigger). No loan, application, payment or installment exists here (those are T-006/T-007).

Compaction (T-005 §2 allows it): schedule policy, fee rules, allocation order, prepayment, payoff, delinquency and
restructure/refinance live in the versioned ``rules`` JSONB document, validated by typed models (``rules.py``) and
frozen with the version; only the currency relation is relational so the tenant-currency FK is enforced by the DB.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    DDL,
    CheckConstraint,
    Date,
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


def _now() -> datetime:
    return datetime.now(UTC)


PRODUCT_STATUSES = ("draft", "active", "inactive")  # 'archived' is reserved (DEFER), not representable yet
VERSION_STATUSES = ("draft", "published", "retired")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class CreditProduct(Base):
    __tablename__ = "credit_products"
    __table_args__ = (
        CheckConstraint(_in("status", PRODUCT_STATUSES), name="status_valid"),
        CheckConstraint("code ~ '^[A-Z0-9][A-Z0-9_-]{1,29}$'", name="code_format"),
        UniqueConstraint("tenant_id", "code", name="uq_credit_products_tenant_code"),
        UniqueConstraint("tenant_id", "id", name="uq_credit_products_tenant_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(140))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class CreditProductVersion(Base):
    __tablename__ = "credit_product_versions"
    __table_args__ = (
        CheckConstraint(_in("status", VERSION_STATUSES), name="status_valid"),
        CheckConstraint("version_number >= 1", name="version_number_positive"),
        CheckConstraint(
            "(status = 'draft') = (rules_hash IS NULL AND snapshot IS NULL AND effective_from IS NULL "
            "AND published_at IS NULL AND published_by IS NULL)",
            name="publication_consistent",
        ),
        CheckConstraint("(status = 'retired') = (retired_at IS NOT NULL)", name="retirement_consistent"),
        CheckConstraint(
            "effective_to IS NULL OR (effective_from IS NOT NULL AND effective_to >= effective_from)",
            name="effective_range_ordered",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["credit_products.tenant_id", "credit_products.id"],
            name="fk_credit_product_versions_tenant_product",
        ),
        UniqueConstraint("tenant_id", "product_id", "version_number", name="uq_credit_product_versions_number"),
        UniqueConstraint("tenant_id", "id", name="uq_credit_product_versions_tenant_id"),
        # at most one open-ended published version per product: the one currently offered
        Index(
            "uq_credit_product_versions_open",
            "product_id",
            unique=True,
            postgresql_where=text("status = 'published' AND effective_to IS NULL"),
        ),
        Index("ix_credit_product_versions_product", "product_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    product_id: Mapped[int] = mapped_column(Integer)
    version_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    rules: Mapped[dict] = mapped_column(JSONB)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)  # lifecycle: closed when superseded
    rules_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    validated_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    published_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CreditProductCurrency(Base):
    """Contractual currencies a version allows, with the amount limits of each. Tenant-safe via composite FKs."""

    __tablename__ = "credit_product_currencies"
    __table_args__ = (
        CheckConstraint("min_amount > 0 AND max_amount >= min_amount", name="amount_range_valid"),
        ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["credit_product_versions.tenant_id", "credit_product_versions.id"],
            name="fk_credit_product_currencies_tenant_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "currency_code"],
            ["tenant_currencies.tenant_id", "tenant_currencies.currency_code"],
            name="fk_credit_product_currencies_tenant_currency",
        ),
    )

    version_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    currency_code: Mapped[str] = mapped_column(String(3), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    min_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    max_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))


# --- immutability triggers (installed by create_all AND by migration 0007) --------------------------
VERSION_GUARD_FN = """
CREATE OR REPLACE FUNCTION credit_product_versions_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status <> 'draft' THEN
      RAISE EXCEPTION 'credit product version % is % and cannot be deleted', OLD.id, OLD.status;
    END IF;
    RETURN OLD;
  END IF;
  IF OLD.status <> 'draft' THEN
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.product_id IS DISTINCT FROM OLD.product_id OR NEW.version_number IS DISTINCT FROM OLD.version_number
       OR NEW.rules IS DISTINCT FROM OLD.rules OR NEW.rules_hash IS DISTINCT FROM OLD.rules_hash
       OR NEW.snapshot IS DISTINCT FROM OLD.snapshot OR NEW.effective_from IS DISTINCT FROM OLD.effective_from
       OR NEW.published_at IS DISTINCT FROM OLD.published_at OR NEW.published_by IS DISTINCT FROM OLD.published_by
       OR NEW.created_by IS DISTINCT FROM OLD.created_by OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
      RAISE EXCEPTION 'credit product version % is published: its rules are immutable', OLD.id;
    END IF;
    IF OLD.effective_to IS NOT NULL AND NEW.effective_to IS DISTINCT FROM OLD.effective_to THEN
      RAISE EXCEPTION 'credit product version % effective_to is already closed', OLD.id;
    END IF;
    IF NEW.status <> OLD.status AND NOT (OLD.status = 'published' AND NEW.status = 'retired') THEN
      RAISE EXCEPTION 'invalid credit product version status transition % -> %', OLD.status, NEW.status;
    END IF;
  ELSIF NEW.status NOT IN ('draft', 'published') THEN
    RAISE EXCEPTION 'a draft version can only become published';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""
VERSION_GUARD_TRIGGER = (
    "CREATE TRIGGER trg_credit_product_versions_guard BEFORE UPDATE OR DELETE ON credit_product_versions "
    "FOR EACH ROW EXECUTE FUNCTION credit_product_versions_guard()"
)
CURRENCY_GUARD_FN = """
CREATE OR REPLACE FUNCTION credit_product_currencies_guard() RETURNS trigger AS $$
DECLARE v_old text; v_new text; v_lo integer; v_hi integer;
BEGIN
  -- Both ends are checked: a row may not be written into, removed from or MOVED to/from a non-draft version.
  -- The version rows are locked FOR SHARE (conflicts with publish's FOR UPDATE and with any UPDATE of the
  -- version) so the draft cannot be published between this check and the commit of the change, and a publish in
  -- flight makes this write wait and then be rejected. Locks are taken in ascending id order: no lock-order cycles.
  IF TG_OP = 'INSERT' THEN
    v_lo := NEW.version_id; v_hi := NEW.version_id;
  ELSIF TG_OP = 'DELETE' THEN
    v_lo := OLD.version_id; v_hi := OLD.version_id;
  ELSE
    v_lo := LEAST(OLD.version_id, NEW.version_id); v_hi := GREATEST(OLD.version_id, NEW.version_id);
  END IF;
  PERFORM 1 FROM credit_product_versions WHERE id = v_lo FOR SHARE;
  IF v_hi <> v_lo THEN
    PERFORM 1 FROM credit_product_versions WHERE id = v_hi FOR SHARE;
  END IF;
  IF TG_OP IN ('UPDATE', 'DELETE') THEN
    SELECT status INTO v_old FROM credit_product_versions WHERE id = OLD.version_id;
  END IF;
  IF TG_OP IN ('INSERT', 'UPDATE') THEN
    SELECT status INTO v_new FROM credit_product_versions WHERE id = NEW.version_id;
  END IF;
  IF (v_old IS NOT NULL AND v_old <> 'draft') OR (v_new IS NOT NULL AND v_new <> 'draft') THEN
    RAISE EXCEPTION 'credit product version is published: its currencies are immutable (%)', TG_OP;
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$ LANGUAGE plpgsql
"""
CURRENCY_GUARD_TRIGGER = (
    "CREATE TRIGGER trg_credit_product_currencies_guard BEFORE INSERT OR UPDATE OR DELETE "
    "ON credit_product_currencies FOR EACH ROW EXECUTE FUNCTION credit_product_currencies_guard()"
)

for _table, _fn, _trigger in (
    (CreditProductVersion.__table__, VERSION_GUARD_FN, VERSION_GUARD_TRIGGER),
    (CreditProductCurrency.__table__, CURRENCY_GUARD_FN, CURRENCY_GUARD_TRIGGER),
):
    event.listen(_table, "after_create", DDL(_fn.replace("%", "%%")).execute_if(dialect="postgresql"))  # DDL % formats
    event.listen(_table, "after_create", DDL(_trigger).execute_if(dialect="postgresql"))
