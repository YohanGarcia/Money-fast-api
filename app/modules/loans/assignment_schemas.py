"""Collection-assignment API schemas. Inputs reject unknown fields: a client ``tenant_id``, reason, date or branch is a 422."""

from pydantic import BaseModel, ConfigDict, Field


class AssignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignee_user_id: int = Field(gt=0)
    idempotency_key: str = Field(min_length=12, max_length=120)


class EndAssignmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=12, max_length=120)
