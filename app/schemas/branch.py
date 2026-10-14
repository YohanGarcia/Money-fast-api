from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BranchCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=140)
    address: str = Field(min_length=5, max_length=255)
    # Preferred: pick real users (they receive the branch-scoped role). The names are then derived from the user.
    manager_user_id: int | None = None
    notary_user_id: int | None = None
    manager_name: str = Field(default="", max_length=140)
    notary_name: str = Field(default="", max_length=140)
    phone: str = Field(min_length=7, max_length=30)


class BranchUpdate(BranchCreate):
    is_active: bool = True


class BranchRead(BranchUpdate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
