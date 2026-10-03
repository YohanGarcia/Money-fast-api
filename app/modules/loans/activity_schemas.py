"""Collection-activity API schemas (T-014). Inputs reject unknown fields: outcome, notes, dates, tenant, actor, branch and
assignment are a 422. No free text, no contact data."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ActivityType = Literal[
    "phone_call", "whatsapp", "sms", "email", "in_person_visit", "office_visit", "no_contact", "other"
]


class CreateActivityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activity_type: ActivityType
    idempotency_key: str = Field(min_length=12, max_length=120)
