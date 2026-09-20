from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.route import RouteAreaType


class RouteAreaIn(BaseModel):
    area_type: RouteAreaType
    name: str = Field(min_length=1, max_length=120)


class RouteAreaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    area_type: RouteAreaType
    name: str


class RouteBase(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    zone: str = Field(default="", max_length=120)
    description: str | None = None
    assigned_collector_id: int | None = None
    branch_id: int | None = None
    is_active: bool = True
    # Polígono (lista de puntos [lat, lng]) que un admin dibuja en el mapa
    # para marcar la zona que cubre la ruta. Opcional -- vacío si la ruta
    # solo se define por áreas de texto (sector/calle/barrio/provincia/
    # municipio). Usado únicamente como una señal más de sugerencia.
    boundary: list[list[float]] = Field(default_factory=list, max_length=300)

    @field_validator("boundary")
    @classmethod
    def valid_boundary(cls, value: list[list[float]]) -> list[list[float]]:
        if not value:
            return value
        if len(value) < 3:
            raise ValueError("La zona dibujada necesita al menos 3 puntos.")
        for point in value:
            if len(point) != 2:
                raise ValueError("Cada punto de la zona debe tener latitud y longitud.")
            lat, lng = point
            if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
                raise ValueError("Uno de los puntos de la zona tiene coordenadas inválidas.")
        return value


class RouteCreate(RouteBase):
    areas: list[RouteAreaIn] = Field(default_factory=list, max_length=50)


class RouteUpdate(RouteBase):
    areas: list[RouteAreaIn] = Field(default_factory=list, max_length=50)


class RouteRead(RouteBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company_id: int
    collector_name: str | None = None
    branch_name: str | None = None
    customer_count: int = 0
    areas: list[RouteAreaOut] = Field(default_factory=list)
    created_at: datetime


class RouteStopSummary(BaseModel):
    id: int
    name: str
    zone: str
    collector_name: str | None = None


class Stop(BaseModel):
    id: int
    full_name: str
    address: str
    latitude: float
    longitude: float
    sequence: int
    # Status of the customer's most relevant loan ("active", "late", "paid",
    # "pending_approval") or null when they have no loan at all. Only
    # "active"/"late" count as an approved loan currently being collected.
    loan_status: str | None = None


class UnlocatedStop(BaseModel):
    id: int
    full_name: str
    address: str
    loan_status: str | None = None


class RouteStops(BaseModel):
    route: RouteStopSummary
    stops: list[Stop]
    unlocated: list[UnlocatedStop]


class RouteSuggestion(BaseModel):
    route_id: int
    route_name: str
    zone: str
    matched_on: list[str]
    score: int
