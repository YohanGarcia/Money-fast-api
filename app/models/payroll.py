"""Employee payroll: a per-user salary/commission config and a ledger of payments.
New payments draw from Caja (one cash expense in an open session) or a company bank account (one bank-ledger outflow).
``capital`` rows are historical only (FM-FINANCIAL-CLOSURE-012). All RD$."""
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, JSON, Numeric, String, Text, text
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
    __table_args__ = (
        CheckConstraint("source IN ('cash', 'capital', 'bank')", name="source_valid"),
        CheckConstraint("(source = 'bank') = (bank_account_id IS NOT NULL)", name="bank_source_has_account"),
        Index("uq_payroll_payments_idempotency", "company_id", "idempotency_key", unique=True,
              postgresql_where=text("idempotency_key IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    source: Mapped[str] = mapped_column(String(20))  # cash|bank (capital: historical rows only)
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
    # FM-FINANCIAL-CLOSURE-012
    bank_account_id: Mapped[int | None] = mapped_column(ForeignKey("bank_accounts.id"))
    reference: Mapped[str] = mapped_column(String(160), default="")
    proof: Mapped[dict | None] = mapped_column(JSON)
    idempotency_key: Mapped[str | None] = mapped_column(String(80))
    request_digest: Mapped[str | None] = mapped_column(String(80))
