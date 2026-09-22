from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TourProgressUpsert(BaseModel):
    status: Literal["in_progress", "completed", "skipped"]
    current_step: int = Field(default=0, ge=0)
    tour_version: int = Field(default=1, ge=1)


class TourProgressRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tour_id: str
    status: Literal["in_progress", "completed", "skipped"]
    current_step: int
    tour_version: int
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None
