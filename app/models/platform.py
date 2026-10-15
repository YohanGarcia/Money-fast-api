"""Platform-owner data: global settings, payment methods offered to the companies, coupons and the platform staff.

Nothing here belongs to a tenant: it is configured by the platform owner (``role = superadmin``) and read by the
companies only through the narrow public/company endpoints."""
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _now() -> datetime:
    return datetime.now(UTC)


class PlatformSetting(Base):
    """One row (id = 1): the owner's global configuration as a JSON document (see services.platform_config)."""

    __tablename__ = "platform_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    values: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class PaymentMethod(Base):
    """A way for a company to pay its subscription: PayPal (account or card, one-off or recurring) or a bank
    transfer to the account the owner publishes here."""

    __tablename__ = "platform_payment_methods"
    __table_args__ = (CheckConstraint("kind IN ('paypal', 'bank_transfer')", name="kind_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    label: Mapped[str] = mapped_column(String(80))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # paypal: {"accept_account", "accept_card", "recurring"}; bank_transfer: {"bank_name", "account_type",
    # "account_number", "account_holder", "holder_document", "currency", "instructions"}
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Coupon(Base):
    __tablename__ = "coupons"
    __table_args__ = (CheckConstraint("kind IN ('percent', 'fixed_usd', 'free_months')", name="kind_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(200), default="")
    kind: Mapped[str] = mapped_column(String(20))
    value: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=Decimal("0.00"))
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), nullable=True)
    billing_cycle: Mapped[str | None] = mapped_column(String(10), nullable=True)  # monthly | annual | None = any
    max_redemptions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    redeemed_count: Mapped[int] = mapped_column(Integer, default=0)
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PlatformStaff(Base):
    """People who work for the platform owner. ``owner`` configures everything; ``support`` reads and assists."""

    __tablename__ = "platform_staff"
    __table_args__ = (CheckConstraint("level IN ('owner', 'support')", name="level_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    level: Mapped[str] = mapped_column(String(10), default="support")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    notes: Mapped[str] = mapped_column(Text, default="")
