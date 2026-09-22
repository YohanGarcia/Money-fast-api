from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SqlEnum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class TourStatus(str, Enum):
    in_progress = "in_progress"
    completed = "completed"
    skipped = "skipped"


class TourProgress(Base):
    """Onboarding tour progress for a user. Absence of a row means not_started."""

    __tablename__ = "tour_progress"
    __table_args__ = (UniqueConstraint("user_id", "tour_id", name="uq_tour_progress_user_tour"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    tour_id: Mapped[str] = mapped_column(String(60))
    status: Mapped[TourStatus] = mapped_column(SqlEnum(TourStatus), default=TourStatus.in_progress)
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    tour_version: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User")
