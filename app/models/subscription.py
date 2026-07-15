from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import Date, DateTime, Enum as SqlEnum, ForeignKey, Numeric, String
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
