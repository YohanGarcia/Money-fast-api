from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class CompanySettings(Base):
    __tablename__ = "company_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160), default="Mi Empresa")
    tax_id: Mapped[str] = mapped_column(String(40), default="")
    address: Mapped[str] = mapped_column(String(255), default="")
    # Structured address parts (like the customer form). ``address`` above stays as the
    # composed one-line version used on receipts and reports.
    province: Mapped[str] = mapped_column(String(60), default="", server_default="")
    municipality: Mapped[str] = mapped_column(String(80), default="", server_default="")
    sector: Mapped[str] = mapped_column(String(120), default="", server_default="")
    street: Mapped[str] = mapped_column(String(160), default="", server_default="")
    house_number: Mapped[str] = mapped_column(String(40), default="", server_default="")
    address_reference: Mapped[str] = mapped_column(String(255), default="", server_default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    currency_symbol: Mapped[str] = mapped_column(String(8), default="$")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    company = relationship("Company")
