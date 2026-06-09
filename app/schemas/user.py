from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user import UserRole


class UserCreate(BaseModel):
    full_name: str = Field(min_length=3, max_length=140)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    role: UserRole = UserRole.manager


class UserUpdate(BaseModel):
    full_name: str = Field(min_length=3, max_length=140)
    email: EmailStr
    password: str | None = Field(default=None, min_length=8, max_length=128)
    role: UserRole = UserRole.manager
    is_active: bool = True


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    email: EmailStr
    role: UserRole
    is_active: bool
    created_at: datetime
