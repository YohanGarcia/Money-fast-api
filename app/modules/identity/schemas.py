"""v2 identity API schemas. Inputs reject unknown fields (so a spoofed ``tenant_id``/``user_id`` is a 422);
outputs never include credential material."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginIn(_In):
    # Tenant context resolved BEFORE the account. Omitted = platform namespace (never a tenant account).
    tenant_slug: str | None = Field(default=None, min_length=1, max_length=63)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=128)
    device_name: str | None = Field(default=None, max_length=120)


class RefreshIn(_In):
    refresh_token: str = Field(min_length=10, max_length=2048)


class RecoveryRequestIn(_In):
    tenant_slug: str | None = Field(default=None, min_length=1, max_length=63)
    email: str = Field(min_length=3, max_length=255)


class RecoveryCompleteIn(_In):
    token: str = Field(min_length=20, max_length=200)
    new_password: str = Field(min_length=1, max_length=128)


class PasswordChangeIn(_In):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


class UserCreateIn(_In):
    email: str = Field(min_length=3, max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    given_names: str = Field(min_length=1, max_length=120)
    family_names: str = Field(default="", max_length=120)
    branch_id: int | None = None


class RoleCreateIn(_In):
    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default="", max_length=255)
    permissions: list[str] = Field(default_factory=list, max_length=100)


class RoleAssignIn(_In):
    role_id: int
    scope: Literal["tenant", "branch", "own"] = "tenant"
    branch_id: int | None = None


class PersonOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    given_names: str
    family_names: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    status: str
    tenant_id: int | None
    person_id: int | None
    branch_id: int | None
    last_login_at: datetime | None
    created_at: datetime


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class GrantOut(BaseModel):
    permission: str
    scope: str
    branch_id: int | None


class MeOut(BaseModel):
    user: UserOut
    person: PersonOut | None
    permissions: list[GrantOut]


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str
    status: str
    system_defined: bool
    permissions: list[str]


class PermissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    description: str
    scope_kind: str
    is_sensitive: bool


class AssignmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    role_id: int
    scope_kind: str
    branch_id: int | None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    occurred_at: datetime
    event_type: str
    outcome: str
    actor_id: int | None
    subject_id: int | None
    correlation_id: str | None
    details: dict


class GoogleChallengeIn(_In):
    tenant_slug: str = Field(min_length=1, max_length=63)


class GoogleLoginIn(_In):
    tenant_slug: str = Field(min_length=1, max_length=63)
    id_token: str = Field(min_length=20, max_length=8192)
    nonce: str = Field(min_length=20, max_length=200)
    device_name: str | None = Field(default=None, max_length=120)


class GoogleLinkIn(_In):
    id_token: str = Field(min_length=20, max_length=8192)
    nonce: str = Field(min_length=20, max_length=200)


class ChallengeOut(BaseModel):
    nonce: str
    expires_in: int


class ExternalIdentityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    provider: str
    linked_at: datetime
