from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CompanySettings(Base):
    __tablename__ = "company_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    name: Mapped[str] = mapped_column(String(160), default="Prestamos Martinez")
    tax_id: Mapped[str] = mapped_column(String(40), default="130-009298-2")
    address: Mapped[str] = mapped_column(String(255), default="Calle Los Girasoles #40")
    phone: Mapped[str] = mapped_column(String(30), default="809-525-4456")
    currency_symbol: Mapped[str] = mapped_column(String(8), default="$")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
