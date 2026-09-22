"""Owner capital held outside the cash boxes. Ledger of injections, withdrawals
and transfers to/from Caja. All amounts RD$."""
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CapitalMovement(Base):
    __tablename__ = "capital_movements"

    # injection / from_cash add to the reserve; withdrawal / to_cash subtract from it.
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    notes: Mapped[str] = mapped_column(Text, default="")
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    cash_movement_id: Mapped[int | None] = mapped_column(ForeignKey("cash_movements.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
