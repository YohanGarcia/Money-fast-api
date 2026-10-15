from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import Date, DateTime, Enum as SqlEnum, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class SubscriptionStatus(str, Enum):
    active = "active"
    pending = "pending"
    failed = "failed"
    cancelled = "cancelled"


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=Decimal("0.00"))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    status: Mapped[SubscriptionStatus] = mapped_column(
        SqlEnum(SubscriptionStatus), default=SubscriptionStatus.active
    )
    provider: Mapped[str] = mapped_column(String(20), default="paypal")
    provider_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    period_start: Mapped[date] = mapped_column(Date())
    period_end: Mapped[date] = mapped_column(Date())
    # How it was paid: cycle, method (paypal | paypal_subscription | bank_transfer | manual), coupon and, for a
    # bank transfer, the payer's reference/proof until the owner confirms or rejects it.
    billing_cycle: Mapped[str] = mapped_column(String(10), default="monthly")
    payment_method: Mapped[str | None] = mapped_column(String(24), nullable=True)
    coupon_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    discount_usd: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=Decimal("0.00"))
    reference: Mapped[str] = mapped_column(String(160), default="")
    payer_note: Mapped[str] = mapped_column(Text, default="")
    proof: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    company = relationship("Company")
    plan = relationship("Plan")

    @property
    def company_name(self) -> str | None:
        return self.company.name if self.company is not None else None

    @property
    def plan_name(self) -> str | None:
        return self.plan.name if self.plan is not None else None
