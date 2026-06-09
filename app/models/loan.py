from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import Date, DateTime, Enum as SqlEnum, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class PaymentFrequency(str, Enum):
    daily = "daily"
    weekly = "weekly"
    biweekly = "biweekly"
    monthly = "monthly"


class LoanStatus(str, Enum):
    pending_approval = "pending_approval"
    active = "active"
    paid = "paid"
    late = "late"
    cancelled = "cancelled"


class Loan(Base):
    __tablename__ = "loans"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    principal_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    interest_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    late_fee_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0.00"))
    grace_days: Mapped[int] = mapped_column(Integer, default=0)
    installment_count: Mapped[int] = mapped_column(Integer)
    payment_frequency: Mapped[PaymentFrequency] = mapped_column(SqlEnum(PaymentFrequency))
    start_date: Mapped[date] = mapped_column(Date())
    end_date: Mapped[date] = mapped_column(Date())
    principal_balance: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    interest_balance: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    late_fee_balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    installment_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    status: Mapped[LoanStatus] = mapped_column(SqlEnum(LoanStatus), default=LoanStatus.active)
    route_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    requires_promissory_note: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    customer = relationship("Customer", back_populates="loans")
    created_by = relationship("User")
    installments = relationship(
        "LoanInstallment",
        back_populates="loan",
        cascade="all, delete-orphan",
        order_by="LoanInstallment.sequence_number",
    )
    payments = relationship("Payment", back_populates="loan", cascade="all, delete-orphan")


class LoanInstallment(Base):
    __tablename__ = "loan_installments"

    id: Mapped[int] = mapped_column(primary_key=True)
    loan_id: Mapped[int] = mapped_column(ForeignKey("loans.id"), index=True)
    sequence_number: Mapped[int] = mapped_column(Integer)
    due_date: Mapped[date] = mapped_column(Date())
    principal_due: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    interest_due: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    fee_due: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    principal_paid: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    interest_paid: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    fee_paid: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    status: Mapped[str] = mapped_column(String(20), default="pending")

    loan = relationship("Loan", back_populates="installments")
