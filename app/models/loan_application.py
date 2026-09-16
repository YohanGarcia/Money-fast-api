from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, JSON, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class LoanApplication(Base):
    __tablename__ = "loan_applications"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    customer_version: Mapped[int | None] = mapped_column(nullable=True)
    loan_id: Mapped[int | None] = mapped_column(ForeignKey("loans.id"), unique=True, nullable=True)
    modality: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    terms: Mapped[dict] = mapped_column(JSON, default=dict)
    history: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    documents = relationship("ApplicationDocument", cascade="all, delete-orphan", lazy="selectin")
    __mapper_args__ = {"version_id_col": version}


class ApplicationDocument(Base):
    __tablename__ = "application_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("loan_applications.id"), index=True)
    category: Mapped[str] = mapped_column(String(40))
    filename: Mapped[str] = mapped_column(String(160))
    media_type: Mapped[str] = mapped_column(String(40))
    content: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    size: Mapped[int] = mapped_column()
    verified: Mapped[bool] = mapped_column(default=False)
