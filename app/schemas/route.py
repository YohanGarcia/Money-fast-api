from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RouteBase(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    zone: str = Field(default="", max_length=120)
    description: str | None = None
    assigned_collector_id: int | None = None
    branch_id: int | None = None
    is_active: bool = True


class RouteCreate(RouteBase):
    pass


class RouteUpdate(RouteBase):
    pass


class RouteRead(RouteBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company_id: int
    collector_name: str | None = None
    branch_name: str | None = None
    customer_count: int = 0
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


class UnlocatedStop(BaseModel):
    id: int
    full_name: str
    address: str


class RouteStops(BaseModel):
    route: RouteStopSummary
    stops: list[Stop]
    unlocated: list[UnlocatedStop]
