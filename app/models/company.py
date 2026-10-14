from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, String, event, text
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.core.database import Base
from app.modules.identity.tenant import SLUG_SQL_REGEX, default_slug


class Company(Base):
    """The Tenant (agencia). The table keeps its legacy name so existing foreign keys stay valid."""

    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint(f"slug ~ '{SLUG_SQL_REGEX}'", name="slug_format"),
        CheckConstraint("status IN ('active', 'inactive')", name="status_valid"),
        # The base currency must exist in the tenant's currency list (deferred: both rows are written together).
        ForeignKeyConstraint(
            ["id", "base_currency_code"],
            ["tenant_currencies.tenant_id", "tenant_currencies.currency_code"],
            name="fk_companies_base_currency_enabled",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
    )

    # autoincrement is explicit: the composite FK below would otherwise make SQLAlchemy drop SERIAL for this PK
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    # Public tenant identifier used to resolve the tenant before authenticating (login/recovery/Google).
    slug: Mapped[str] = mapped_column(String(63), unique=True, default=default_slug)
    tax_id: Mapped[str] = mapped_column(String(40), default="")
    address: Mapped[str] = mapped_column(String(255), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    # Tenant lifecycle. An inactive tenant accepts no new authenticated operations; history is never deleted.
    status: Mapped[str] = mapped_column(String(20), default="active")
    # integrity comes from the deferred composite FK to tenant_currencies (which itself references currencies)
    base_currency_code: Mapped[str] = mapped_column(String(3), default="DOP")
    default_timezone: Mapped[str] = mapped_column(String(64), default=lambda: settings.default_timezone)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), nullable=True)
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Active PayPal recurring subscription id (I-XXXX); None for one-time or free.
    paypal_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    plan = relationship("Plan")
    users = relationship("UserAccount", back_populates="company")

    @property
    def code(self) -> str:
        """Visible tenant code == its public slug (unique across the platform)."""
        return self.slug

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


@event.listens_for(Company, "after_insert")
def _provision_base_currency(mapper, connection, target) -> None:
    """Every tenant, whatever path created it, starts with its base currency enabled (and the catalogue present)."""
    from app.modules.organization.catalog import CURRENCY_CATALOG

    for code, name, symbol, exponent in CURRENCY_CATALOG:
        connection.execute(
            text(
                "INSERT INTO currencies (code, name, symbol, exponent, is_active) "
                "VALUES (:c, :n, :s, :e, true) ON CONFLICT (code) DO NOTHING"
            ),
            {"c": code, "n": name, "s": symbol, "e": exponent},
        )
    connection.execute(
        text(
            "INSERT INTO tenant_currencies (tenant_id, currency_code, enabled_at) "
            "VALUES (:t, :c, now()) ON CONFLICT DO NOTHING"
        ),
        {"t": target.id, "c": target.base_currency_code or "DOP"},
    )
