"""Identity & authorization persistence (T-002, ADR-004).

Person != UserAccount != Role != Permission:

* ``Person``       — common identity only (no credentials, permissions or debt);
* ``UserAccount``  — credentials + lifecycle (table ``users``, kept for FK compatibility);
* ``Role``         — administrable bundle of permissions (tenant roles or platform roles);
* ``Permission``   — stable ``resource.action`` code from a code-defined catalog;
* ``UserRoleAssignment`` — who holds which role, at which scope, with revocation history.

The tenant is the ``companies`` row until T-003 formalises Tenant/Branch.
"""

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


# --- statuses ---------------------------------------------------------------
USER_STATUSES = ("pending", "active", "locked", "disabled")
PERSON_STATUSES = ("active", "inactive")
ROLE_STATUSES = ("active", "archived")
ASSIGNMENT_SCOPES = ("tenant", "branch", "cash_point", "own")
TOKEN_PURPOSES = ("recovery", "activation")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class UserRole(str, Enum):  # noqa: UP042 - persisted enum values must stay plain strings
    """LEGACY coarse role. Kept only for legacy routes until their modules are rebuilt;
    authorization in the new architecture never relies on it."""

    superadmin = "superadmin"
    admin = "admin"
    manager = "manager"
    collector = "collector"
    cashier = "cashier"


class Person(Base):
    __tablename__ = "persons"
    __table_args__ = (
        CheckConstraint(_in("status", PERSON_STATUSES), name="status_valid"),
        Index("ix_persons_tenant_id", "tenant_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    given_names: Mapped[str] = mapped_column(String(120))
    family_names: Mapped[str] = mapped_column(String(120), default="")
    document_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    document_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    @property
    def full_name(self) -> str:
        return f"{self.given_names} {self.family_names}".strip()


class UserAccount(Base):
    """Credentials and lifecycle. ``status`` is authoritative; ``is_active`` is a legacy view of it."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(_in("status", USER_STATUSES), name="status_valid"),
        CheckConstraint("email = lower(btrim(email))", name="email_normalized"),
        # A user's home branch must belong to the user's own tenant.
        ForeignKeyConstraint(
            ["company_id", "branch_id"], ["branches.company_id", "branches.id"], name="fk_users_tenant_branch"
        ),
        # Login identity is tenant-aware: the same normalised email may exist in different tenants.
        # Platform accounts (company_id NULL) have their own, separate uniqueness.
        Index(
            "uq_users_tenant_email", "company_id", "email", unique=True, postgresql_where=text("company_id IS NOT NULL")
        ),
        Index("uq_users_platform_email", "email", unique=True, postgresql_where=text("company_id IS NULL")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id"), nullable=True, index=True)
    full_name: Mapped[str] = mapped_column(String(140))
    email: Mapped[str] = mapped_column(String(255), index=True)  # normalised login identifier (lower, trimmed)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole | None] = mapped_column(
        SqlEnum(UserRole), nullable=True
    )  # LEGACY; NULL = no legacy privileges
    status: Mapped[str] = mapped_column(String(20), default="active")
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)  # tenant; NULL = platform
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True, index=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Last known GPS position (live tracking, opt-in from the mobile app). LEGACY, owned by routes.
    last_lat: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    last_lng: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    last_location_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    sessions = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")
    company = relationship("Company", back_populates="users")
    branch = relationship("Branch", foreign_keys="UserAccount.branch_id")
    person = relationship("Person")

    @property
    def tenant_id(self) -> int | None:
        return self.company_id

    @property
    def is_platform(self) -> bool:
        return self.company_id is None

    @property
    def branch_name(self) -> str | None:
        return self.branch.name if self.branch is not None else None

    @hybrid_property
    def is_active(self) -> bool:
        return self.status == "active"

    @is_active.inplace.setter
    def _is_active_setter(self, value: bool) -> None:
        # LEGACY writers only (user edit screens). New code uses the explicit state machine.
        if value and self.status != "active":
            self.status = "active"
            self.disabled_at = None
            self.locked_at = self.locked_until = None
            self.activated_at = self.activated_at or _now()
        elif not value and self.status != "disabled":
            self.status = "disabled"
            self.disabled_at = _now()

    @is_active.inplace.expression
    @classmethod
    def _is_active_expression(cls):
        return cls.status == "active"


class Permission(Base):
    __tablename__ = "permissions"
    __table_args__ = (CheckConstraint(_in("scope_kind", ("platform", "tenant")), name="scope_kind_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    scope_kind: Mapped[str] = mapped_column(String(10))  # platform | tenant administration capability
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = (
        CheckConstraint(_in("status", ROLE_STATUSES), name="status_valid"),
        # Tenant-aware uniqueness; platform roles (tenant_id NULL) are unique among themselves.
        Index("uq_roles_tenant_name", "tenant_id", "name", unique=True, postgresql_where=text("tenant_id IS NOT NULL")),
        Index("uq_roles_platform_name", "name", unique=True, postgresql_where=text("tenant_id IS NULL")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    system_defined: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    permissions = relationship("Permission", secondary="role_permissions", lazy="selectin")


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), primary_key=True)
    permission_id: Mapped[int] = mapped_column(ForeignKey("permissions.id"), primary_key=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class UserRoleAssignment(Base):
    """Role held by a user at a scope. Revocation keeps the row (history is never deleted)."""

    __tablename__ = "user_role_assignments"
    __table_args__ = (
        CheckConstraint(_in("scope_kind", ASSIGNMENT_SCOPES), name="scope_kind_valid"),
        CheckConstraint("(scope_kind = 'branch') = (branch_id IS NOT NULL)", name="branch_scope_consistent"),
        CheckConstraint(
            "(scope_kind = 'cash_point') = (cash_point_id IS NOT NULL)", name="cash_point_scope_consistent"
        ),
        # A scope can never reference a branch / cash point of another tenant (composite, tenant-safe keys).
        ForeignKeyConstraint(
            ["tenant_id", "branch_id"], ["branches.company_id", "branches.id"], name="fk_assignments_tenant_branch"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "cash_point_id"],
            ["cash_points.tenant_id", "cash_points.id"],
            name="fk_assignments_tenant_cash_point",
        ),
        Index(
            "uq_assignment_active_nonbranch",
            "user_id",
            "role_id",
            "scope_kind",
            unique=True,
            postgresql_where=text("revoked_at IS NULL AND branch_id IS NULL AND cash_point_id IS NULL"),
        ),
        Index(
            "uq_assignment_active_cash_point",
            "user_id",
            "role_id",
            "cash_point_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL AND cash_point_id IS NOT NULL"),
        ),
        Index(
            "uq_assignment_active_branch",
            "user_id",
            "role_id",
            "branch_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL AND branch_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), index=True)
    scope_kind: Mapped[str] = mapped_column(String(10), default="tenant")
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), nullable=True)
    cash_point_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assigned_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    revoked_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    role = relationship("Role")


class RecoveryToken(Base):
    """Single-use, expiring, revocable secret. Only its SHA-256 is stored."""

    __tablename__ = "recovery_tokens"
    __table_args__ = (CheckConstraint(_in("purpose", TOKEN_PURPOSES), name="purpose_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    purpose: Mapped[str] = mapped_column(String(20))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuthThrottle(Base):
    """Exponential-backoff counters keyed by an HMAC of the identifier (no raw emails/IPs)."""

    __tablename__ = "auth_throttle"
    __table_args__ = (UniqueConstraint("scope", "key_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(40))
    key_hash: Mapped[str] = mapped_column(String(64))
    failures: Mapped[int] = mapped_column(Integer, default=0)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SecurityEvent(Base):
    """Append-only functional security audit (never edited or deleted; enforced by a DB trigger).

    ``actor_id`` / ``subject_id`` are deliberately not foreign keys: they are historical evidence
    that must outlive any row. ``details`` is whitelisted/redacted and never holds secrets.
    """

    __tablename__ = "security_events"
    __table_args__ = (
        Index("ix_security_events_tenant_occurred", "tenant_id", "occurred_at"),
        Index("ix_security_events_subject", "subject_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    event_type: Mapped[str] = mapped_column(String(60))
    outcome: Mapped[str] = mapped_column(String(20), default="success")
    tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subject_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)


class ExternalIdentity(Base):
    """A provider identity (OIDC ``iss`` + ``sub``) explicitly linked to one UserAccount of one tenant.

    Fast Money stays authoritative for tenant, account, status, roles and sessions: the provider only
    proves who is at the keyboard. No provider tokens are stored; ``email`` is informational metadata
    captured at link time and is never used to find or link accounts. Unlinking keeps the row (revoked).
    """

    __tablename__ = "external_identities"
    __table_args__ = (
        CheckConstraint(_in("provider", ("google",)), name="provider_valid"),
        # One provider identity <-> one account per tenant; one identity per provider per account.
        Index(
            "uq_external_identity_active_subject",
            "tenant_id",
            "provider",
            "issuer",
            "subject",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index(
            "uq_external_identity_active_user",
            "user_id",
            "provider",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(20))
    issuer: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OidcChallenge(Base):
    """Single-use nonce handed to the client before it talks to the provider (replay/CSRF protection).

    Bound to a tenant and a purpose (``login`` or ``link``); ``link`` challenges are also bound to the user.
    Only the SHA-256 of the nonce is stored.
    """

    __tablename__ = "oidc_challenges"
    __table_args__ = (CheckConstraint(_in("purpose", ("login", "link")), name="purpose_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    nonce_hash: Mapped[str] = mapped_column(String(64), unique=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    purpose: Mapped[str] = mapped_column(String(10))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
