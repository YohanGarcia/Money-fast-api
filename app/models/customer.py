from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(160), index=True)
    document_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    phone: Mapped[str] = mapped_column(String(30))
    address: Mapped[str] = mapped_column(Text())
    notes: Mapped[str | None] = mapped_column(Text(), nullable=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    # Cobrador (asesor) responsible for this customer's route/portfolio.
    assigned_collector_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    # Collection route the customer belongs to; drives the assigned collector.
    route_id: Mapped[int | None] = mapped_column(ForeignKey("routes.id"), nullable=True, index=True)
    # GPS location for navigation ("Cómo llegar").
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    created_by = relationship("User", foreign_keys=[created_by_id])
    assigned_collector = relationship("User", foreign_keys=[assigned_collector_id])
    route = relationship("Route", foreign_keys=[route_id])
    loans = relationship("Loan", back_populates="customer")
    company = relationship("Company")

    @property
    def route_name(self) -> str | None:
        return self.route.name if self.route is not None else None

    @property
    def collector_name(self) -> str | None:
        if self.assigned_collector is None:
            return None
        return self.assigned_collector.full_name
