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
    # FM-FINANCIAL-INTEGRITY-005 (F-I01). Optional on purpose (legacy clients keep working, unprotected): when
    # present the operation is replay-safe per (tenant, actor, key). 12..72 chars; the stored key is prefixed so it
    # cannot collide with a Cash command key of the same actor.
    idempotency_key: str | None = Field(default=None, min_length=12, max_length=72)


class CapitalMovementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    amount: Decimal
    notes: str
    actor_id: int
    created_at: datetime
    actor_name: str | None = None
