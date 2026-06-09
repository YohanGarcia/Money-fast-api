from pydantic import BaseModel, EmailStr, Field

from app.schemas.user import UserRead


class LoginInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    device_name: str | None = Field(default=None, max_length=120)


class RefreshInput(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserRead


class PasswordResetRequestInput(BaseModel):
    email: EmailStr


class PasswordResetRequestOutput(BaseModel):
    message: str
    email: str
    debug_code: str | None = None


class VerifyResetCodeInput(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6)


class VerifyResetCodeOutput(BaseModel):
    reset_token: str
    message: str


class ResetPasswordInput(BaseModel):
    reset_token: str
    new_password: str = Field(min_length=8, max_length=128)
