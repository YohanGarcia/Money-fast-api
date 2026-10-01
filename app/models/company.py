from datetime import UTC, datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.modules.identity.tenant import SLUG_SQL_REGEX, default_slug


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (CheckConstraint(f"slug ~ '{SLUG_SQL_REGEX}'", name="slug_format"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    # Public tenant identifier used to resolve the tenant before authenticating (login/recovery/Google).
    slug: Mapped[str] = mapped_column(String(63), unique=True, default=default_slug)
    tax_id: Mapped[str] = mapped_column(String(40), default="")
    address: Mapped[str] = mapped_column(String(255), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), nullable=True)
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Active PayPal recurring subscription id (I-XXXX); None for one-time or free.
    paypal_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    plan = relationship("Plan")
    users = relationship("UserAccount", back_populates="company")
