from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LoanSettings(Base):
    __tablename__ = "loan_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    minimum_principal: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("10000.00"))
    maximum_principal: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("50000.00"))
    default_interest_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("10.00"))
    minimum_term_count: Mapped[int] = mapped_column(default=2)
    minimum_term_unit: Mapped[str] = mapped_column(String(20), default="Semanas")
    default_late_fee_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("3.00"))
    default_grace_days: Mapped[int] = mapped_column(default=5)
    require_route_assignment: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_payment_date_change: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
