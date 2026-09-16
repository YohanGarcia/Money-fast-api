from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("company_id", "document_key", name="uq_customer_company_document"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(160), index=True)
    document_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    phone: Mapped[str] = mapped_column(String(30))
    address: Mapped[str] = mapped_column(Text())
    notes: Mapped[str | None] = mapped_column(Text(), nullable=True)
    document_key: Mapped[str | None] = mapped_column(String(30), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    home_phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    birth_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    marital_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    nationality: Mapped[str | None] = mapped_column(String(160), nullable=True)
    city: Mapped[str | None] = mapped_column(String(160), nullable=True)
    references: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    version: Mapped[int] = mapped_column(default=1, server_default="1")
    __mapper_args__ = {"version_id_col": version}
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
    cash_branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True)
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
