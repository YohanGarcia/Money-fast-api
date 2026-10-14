from datetime import UTC, datetime

from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class BankAccount(Base):
    """Company bank accounts customers can transfer to. Company-wide, admin managed."""

    __tablename__ = "bank_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    bank_name: Mapped[str] = mapped_column(String(120))
    account_number: Mapped[str] = mapped_column(String(60))
    account_holder: Mapped[str] = mapped_column(String(140))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    company = relationship("Company")

    @property
    def label(self) -> str:
        """Human string stored on transfers so history stays readable if the account is edited."""
        tail = self.account_number[-4:] if len(self.account_number) > 4 else self.account_number
        return f"{self.bank_name} ****{tail} · {self.account_holder}"


class BankAccountMovement(Base):
    """Minimal bank ledger (FM-FINANCIAL-CLOSURE-012): one row per company payment out of (or into) an account.

    It records WHAT the company did with the account; it never computes or stores a balance (the bank is the source of
    truth for balances). ``amount`` is signed: negative = outflow. A payroll payment owns exactly one row."""

    __tablename__ = "bank_account_movements"
    __table_args__ = (
        CheckConstraint("amount <> 0", name="amount_nonzero"),
        CheckConstraint("kind IN ('payroll_payment')", name="kind_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    bank_account_id: Mapped[int] = mapped_column(ForeignKey("bank_accounts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    reference: Mapped[str] = mapped_column(String(160), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    account_label: Mapped[str] = mapped_column(String(300), default="")  # snapshot: history stays readable if edited
    payroll_payment_id: Mapped[int | None] = mapped_column(ForeignKey("payroll_payments.id", name="fk_bank_movements_payroll_payment", use_alter=True), unique=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
