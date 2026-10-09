"""Organization persistence (T-003): Tenant (= ``companies``), Branch, CashPoint, Currency.

Tenant -> Branch -> CashPoint (Branch 1:N CashPoints; nothing forces one cash point per branch).
Composite foreign keys make cross-tenant references impossible at the database level.
No cash sessions, movements, balances, FX or payments live here.
"""

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


ORG_STATUSES = ("active", "inactive")
CASH_POINT_STATUSES = ("active", "inactive", "suspended")
# T-021: manual = created by the tenant; legacy_box = the base cash point of a cash box (one per box, created with it);
# legacy_session_split = created by migration 0020 for a concurrent legacy session (D8), born suspended (D12)
CASH_POINT_ORIGINS = ("manual", "legacy_box", "legacy_session_split")


def in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Currency(Base):
    """Platform catalogue (ISO 4217). ``exponent`` = decimal places: the precision rule of the currency."""

    __tablename__ = "currencies"
    __table_args__ = (
        CheckConstraint("code ~ '^[A-Z]{3}$'", name="code_format"),
        CheckConstraint("exponent BETWEEN 0 AND 4", name="exponent_range"),
    )

    code: Mapped[str] = mapped_column(String(3), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    symbol: Mapped[str | None] = mapped_column(String(8), nullable=True)
    exponent: Mapped[int] = mapped_column(SmallInteger)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class TenantCurrency(Base):
    """Currencies a tenant may use. Disabling keeps the row (history stays queryable)."""

    __tablename__ = "tenant_currencies"

    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), primary_key=True)
    currency_code: Mapped[str] = mapped_column(ForeignKey("currencies.code"), primary_key=True)
    enabled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    enabled_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_by: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CashPoint(Base):
    """Operational custody point of one branch. Not a synonym of branch; several per branch are allowed.

    ``suspended`` is an explicit administrative state (reason + audit). It is never set automatically — in
    particular a pending cash difference must not suspend a cash point (DR-007).
    """

    __tablename__ = "cash_points"
    __table_args__ = (
        CheckConstraint(in_list("status", CASH_POINT_STATUSES), name="status_valid"),
        CheckConstraint("(status = 'suspended') = (suspended_at IS NOT NULL)", name="suspension_consistent"),
        CheckConstraint(in_list("origin", CASH_POINT_ORIGINS), name="origin_valid"),
        CheckConstraint("(origin = 'legacy_box') = (box_id IS NOT NULL)", name="box_link_consistent"),
        ForeignKeyConstraint(
            ["tenant_id", "branch_id"], ["branches.company_id", "branches.id"], name="fk_cash_points_tenant_branch"
        ),
        UniqueConstraint("tenant_id", "code", name="uq_cash_points_tenant_code"),
        UniqueConstraint("tenant_id", "id", name="uq_cash_points_tenant_id"),
        UniqueConstraint("box_id", name="uq_cash_points_box_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    branch_id: Mapped[int] = mapped_column(Integer, index=True)
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(140))
    status: Mapped[str] = mapped_column(String(20), default="active")
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspension_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    origin: Mapped[str] = mapped_column(String(30), default="manual")
    box_id: Mapped[int | None] = mapped_column(ForeignKey("cash_boxes.id"), nullable=True)


class CashPointCurrency(Base):
    """Optional narrowing of the currencies a cash point admits (empty set = every enabled tenant currency)."""

    __tablename__ = "cash_point_currencies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "cash_point_id"],
            ["cash_points.tenant_id", "cash_points.id"],
            name="fk_cash_point_currencies_cash_point",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "currency_code"],
            ["tenant_currencies.tenant_id", "tenant_currencies.currency_code"],
            name="fk_cash_point_currencies_tenant_currency",
        ),
    )

    cash_point_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    currency_code: Mapped[str] = mapped_column(String(3), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer)
