from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(140), unique=True, index=True)
    customer_limit: Mapped[int] = mapped_column(default=0)
    loan_limit: Mapped[int] = mapped_column(default=0)
    user_limit: Mapped[int] = mapped_column(default=0)
    monthly_price_usd: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=Decimal("0.00"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # PayPal billing plan id (P-XXXX) for recurring subscriptions; None until set up.
    paypal_plan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
