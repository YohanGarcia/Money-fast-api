from pydantic import BaseModel, EmailStr, Field

from app.schemas.user import UserRead


class RegisterInput(BaseModel):
    """Self-service onboarding: creates a company and its admin owner.

    The role is always forced to admin on the server — it is never taken
    from the request body, to prevent privilege escalation.
    """

    full_name: str = Field(min_length=3, max_length=140)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    company_name: str | None = Field(default=None, max_length=160)


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
    # Only populated in development (when SMTP is not configured) to ease testing.
    # Always null in production.
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
