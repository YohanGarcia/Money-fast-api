from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
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
