"""Employee payroll: a per-user salary/commission config and a ledger of payments.
Payments draw from Caja (cash expense) or Capital (reserve withdrawal). All RD$."""
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, JSON, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PayrollConfig(Base):
    __tablename__ = "payroll_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    salary_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    salary_frequency: Mapped[str] = mapped_column(String(20), default="monthly")  # weekly|biweekly|monthly
    # commissions: list of {"base": collections|interest|disbursements, "percent": "5.00"}
    commissions: Mapped[list] = mapped_column(JSON, default=list)


class PayrollPayment(Base):
    __tablename__ = "payroll_payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    source: Mapped[str] = mapped_column(String(20))  # cash|capital
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    salary_part: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    commission_part: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"))
    cash_movement_id: Mapped[int | None] = mapped_column(ForeignKey("cash_movements.id"))
    capital_movement_id: Mapped[int | None] = mapped_column(ForeignKey("capital_movements.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
