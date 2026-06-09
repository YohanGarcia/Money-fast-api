from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(160), index=True)
    document_id: Mapped[str | None] = mapped_column(String(30), unique=True, nullable=True)
    phone: Mapped[str] = mapped_column(String(30))
    address: Mapped[str] = mapped_column(Text())
    notes: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    created_by = relationship("User")
    loans = relationship("Loan", back_populates="customer")
