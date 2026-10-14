import secrets
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _default_code() -> str:
    return "SUC-" + secrets.token_hex(3).upper()


class Branch(Base):
    """Sucursal of exactly one tenant (``company_id``). Owns 0..N CashPoints (see organization module)."""

    __tablename__ = "branches"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'inactive')", name="status_valid"),
        CheckConstraint("code ~ '^[A-Z0-9][A-Z0-9_-]{0,19}$'", name="code_format"),
        UniqueConstraint("company_id", "code", name="uq_branches_tenant_code"),  # visible code is tenant-scoped
        UniqueConstraint("company_id", "id", name="uq_branches_tenant_id"),  # target of composite tenant-safe FKs
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), default=_default_code)
    name: Mapped[str] = mapped_column(String(140), index=True)
    address: Mapped[str] = mapped_column(String(255), default="")
    manager_name: Mapped[str] = mapped_column(String(140), default="")
    notary_name: Mapped[str] = mapped_column(String(140), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    # IANA zone overriding the tenant default; NULL = inherit (ADR-006)
    timezone_override: Mapped[str | None] = mapped_column(String(64), nullable=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    company = relationship("Company")

    @property
    def tenant_id(self) -> int:
        return self.company_id

    @hybrid_property
    def is_active(self) -> bool:
        return self.status == "active"

    @is_active.inplace.setter
    def _is_active_setter(self, value: bool) -> None:  # LEGACY writers; new code uses explicit transitions
        self.status = "active" if value else "inactive"

    @is_active.inplace.expression
    @classmethod
    def _is_active_expression(cls):
        return cls.status == "active"
