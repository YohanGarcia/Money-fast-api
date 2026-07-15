from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user import UserRole


class UserCreate(BaseModel):
    full_name: str = Field(min_length=3, max_length=140)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    role: UserRole = UserRole.manager
    branch_id: int | None = None


class UserUpdate(BaseModel):
    full_name: str = Field(min_length=3, max_length=140)
    email: EmailStr
    password: str | None = Field(default=None, min_length=8, max_length=128)
    role: UserRole = UserRole.manager
    is_active: bool = True
    branch_id: int | None = None


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    email: EmailStr
    role: UserRole
    is_active: bool
    company_id: int | None = None
    branch_id: int | None = None
    branch_name: str | None = None
    created_at: datetime
