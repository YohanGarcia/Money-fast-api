from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.user import UserRead


class RegisterInput(BaseModel):
    """Self-service onboarding: creates a company and its admin owner.

    The role is always forced to admin on the server — it is never taken
    from the request body, to prevent privilege escalation.
    """

    full_name: str = Field(min_length=3, max_length=140)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    company_name: str = Field(min_length=1, max_length=160)

    @field_validator("company_name", mode="before")
    @classmethod
    def validate_company_name(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("El nombre de la empresa es obligatorio.")
        return value


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
