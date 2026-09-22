from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CapitalMovementCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    # Direct capital operations only. Transfers to/from Caja happen from the Caja screen.
    kind: Literal["injection", "withdrawal"]
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    notes: str = Field(default="", max_length=2000)


class CapitalMovementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    amount: Decimal
    notes: str
    actor_id: int
    created_at: datetime
    actor_name: str | None = None
