from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Route(Base):
    __tablename__ = "routes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    zone: Mapped[str] = mapped_column(String(120), default="")
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    assigned_collector_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    assigned_collector = relationship("User", foreign_keys=[assigned_collector_id])
    branch = relationship("Branch")
    company = relationship("Company")

    @property
    def collector_name(self) -> str | None:
        if self.assigned_collector is None:
            return None
        return self.assigned_collector.full_name

    @property
    def branch_name(self) -> str | None:
        return self.branch.name if self.branch is not None else None
