from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import DateTime, Enum as SqlEnum, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class PaymentType(str, Enum):
    installment = "installment"
    payoff = "payoff"
    custom = "custom"
    principal_only = "principal_only"
    interest_only = "interest_only"


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    loan_id: Mapped[int] = mapped_column(ForeignKey("loans.id"), index=True)
    collected_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    payment_type: Mapped[PaymentType] = mapped_column(SqlEnum(PaymentType))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    principal_applied: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    interest_applied: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    late_fee_applied: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    notes: Mapped[str | None] = mapped_column(Text(), nullable=True)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    reference_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    loan = relationship("Loan", back_populates="payments")
    collected_by = relationship("User")

    @property
    def collector_name(self) -> str | None:
        if self.collected_by is None:
            return None
        return self.collected_by.full_name
