from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class RouteAreaType(str, Enum):
    sector = "sector"
    calle = "calle"
    barrio = "barrio"
    provincia = "provincia"
    municipio = "municipio"


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
    # Zona dibujada en el mapa (lista de puntos [lat, lng] que forman un
    # polígono). Se usa como otra señal -- solo de sugerencia -- para
    # /routes/suggest: si el punto GPS del cliente cae dentro, se sugiere
    # esta ruta. Vacía por defecto (rutas definidas solo por áreas de texto).
    boundary: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    assigned_collector = relationship("User", foreign_keys=[assigned_collector_id])
    branch = relationship("Branch")
    company = relationship("Company")
    areas = relationship(
        "RouteArea", back_populates="route", cascade="all, delete-orphan", order_by="RouteArea.name"
    )

    @property
    def collector_name(self) -> str | None:
        if self.assigned_collector is None:
            return None
        return self.assigned_collector.full_name

    @property
    def branch_name(self) -> str | None:
        return self.branch.name if self.branch is not None else None


class RouteArea(Base):
    """A sector/calle/barrio covered by a route, used to suggest a route for a
    customer based on their address (never assigned automatically/silently)."""

    __tablename__ = "route_areas"

    id: Mapped[int] = mapped_column(primary_key=True)
    route_id: Mapped[int] = mapped_column(ForeignKey("routes.id"), index=True)
    area_type: Mapped[RouteAreaType] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(120))
    # Lowercased/trimmed copy of ``name`` used for case-insensitive matching.
    normalized_name: Mapped[str] = mapped_column(String(120), index=True)

    route = relationship("Route", back_populates="areas")
