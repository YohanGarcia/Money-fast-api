from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BranchCreate(BaseModel):
    name: str = Field(min_length=2, max_length=140)
    address: str = Field(min_length=5, max_length=255)
    manager_name: str = Field(min_length=3, max_length=140)
    notary_name: str = Field(min_length=3, max_length=140)
    phone: str = Field(min_length=7, max_length=30)


class BranchUpdate(BranchCreate):
    is_active: bool = True


class BranchRead(BranchUpdate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
