from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class PrintSettings(Base):
    __tablename__ = "print_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), unique=True, index=True)
    enable_receipts: Mapped[bool] = mapped_column(Boolean, default=True)
    enable_promissory_note: Mapped[bool] = mapped_column(Boolean, default=True)
    show_tax_id: Mapped[bool] = mapped_column(Boolean, default=True)
    show_document_id: Mapped[bool] = mapped_column(Boolean, default=True)
    show_phone: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_share_receipt: Mapped[bool] = mapped_column(Boolean, default=True)
    enable_invoice_printing: Mapped[bool] = mapped_column(Boolean, default=True)
    receipt_footer_text: Mapped[str] = mapped_column(String(255), default="Gracias por su pago!")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    company = relationship("Company")
