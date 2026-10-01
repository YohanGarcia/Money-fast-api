"""Typed, validated application settings (T-001).

Business rules must never branch on ``environment``; the environment only
selects infrastructure behaviour (strictness of secrets/hosts, log format).
"""

from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SECRET_KEY = "change-me-in-production"
STRICT_ENVIRONMENTS = {"staging", "production"}
LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
POSTGRES_PREFIXES = ("postgres://", "postgresql://", "postgresql+psycopg2://")


class Settings(BaseSettings):
    app_name: str = "MoneyFast API"
    app_version: str = "0.1.0"
    environment: Literal["development", "test", "staging", "production"] = "development"
    debug: bool = False
    log_level: str = "INFO"

    # Secrets are excluded from repr() so they never reach logs by accident.
    secret_key: str = Field(default=DEFAULT_SECRET_KEY, repr=False)
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    # PostgreSQL only (ADR-003). Required: there is deliberately no default.
    database_url: str = Field(repr=False)
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_connect_timeout_seconds: int = Field(default=5, ge=1)

    # Authentication (T-002). Argon2id parameters are configurable; defaults follow argon2-cffi.
    password_min_length: int = Field(default=10, ge=8)
    password_max_length: int = Field(default=128, ge=64)
    password_hash_time_cost: int = Field(default=3, ge=1)
    password_hash_memory_kib: int = Field(default=65536, ge=8192)
    password_hash_parallelism: int = Field(default=4, ge=1)
    recovery_token_ttl_minutes: int = Field(default=15, ge=1)
    activation_token_ttl_hours: int = Field(default=72, ge=1)
    # Anti-abuse: exponential backoff per counter (seconds), capped; failures decay after the window.
    auth_throttle_window_seconds: int = Field(default=900, ge=60)
    auth_throttle_base_seconds: int = Field(default=30, ge=1)
    auth_throttle_max_seconds: int = Field(default=900, ge=1)
    auth_max_failures_account_ip: int = Field(default=5, ge=1)
    auth_max_failures_ip: int = Field(default=50, ge=1)
    auth_account_lock_failures: int = Field(default=10, ge=1)
    auth_account_lock_seconds: int = Field(default=900, ge=1)
    recovery_max_requests_account: int = Field(default=3, ge=1)
    recovery_max_requests_ip: int = Field(default=20, ge=1)
    recovery_max_failures_ip: int = Field(default=10, ge=1)

    # IANA identifier of the platform default business timezone (ADR-006).
    default_timezone: str = "America/Santo_Domingo"

    # CORS / host protection — comma-separated lists.
    cors_origins: str = (
        "http://localhost:8081,http://127.0.0.1:8081,"
        "http://localhost:19006,http://127.0.0.1:19006,"
        "http://localhost:5173,http://127.0.0.1:5173"
    )
    trusted_hosts: str = "localhost,127.0.0.1,testserver"

    # Prepared for later packages (ADR-005); no behaviour attached in T-001.
    outbox_enabled: bool = False
    worker_enabled: bool = False

    # SMTP — empty means the code is printed to the console (development only)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = Field(default="", repr=False)
    smtp_from_email: str = "noreply@moneyfast.com"
    smtp_from_name: str = "MoneyFast"

    # PayPal
    paypal_client_id: str = ""
    paypal_secret: str = Field(default="", repr=False)
    paypal_mode: str = "sandbox"  # "sandbox" | "live"
    paypal_webhook_id: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_strict(self) -> bool:
        return self.environment in STRICT_ENVIRONMENTS

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def trusted_host_list(self) -> list[str]:
        return [host.strip() for host in self.trusted_hosts.split(",") if host.strip()]

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        for prefix in POSTGRES_PREFIXES:
            if self.database_url.startswith(prefix):
                self.database_url = "postgresql+psycopg://" + self.database_url[len(prefix) :]
                break
        if not self.database_url.startswith("postgresql+psycopg://"):
            raise ValueError(
                "DATABASE_URL debe ser PostgreSQL (postgresql+psycopg://...). Fast Money no admite otra base de datos."
            )

        self.log_level = self.log_level.upper()
        if self.log_level not in LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL debe ser uno de {sorted(LOG_LEVELS)}.")

        try:
            ZoneInfo(self.default_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("DEFAULT_TIMEZONE debe ser un identificador IANA valido.") from exc

        if self.is_strict:
            if self.secret_key == DEFAULT_SECRET_KEY:
                raise ValueError(
                    "SECRET_KEY no puede ser el valor por defecto en staging/produccion. "
                    "Define una clave segura en las variables de entorno."
                )
            if len(self.secret_key) < 32:
                raise ValueError("SECRET_KEY debe tener al menos 32 caracteres en staging/produccion.")
            if self.debug:
                raise ValueError("DEBUG no puede estar activo en staging/produccion.")
            if "trusted_hosts" not in self.model_fields_set:
                raise ValueError("TRUSTED_HOSTS debe definirse explicitamente en staging/produccion.")
            if "*" in self.trusted_host_list or not self.trusted_host_list:
                raise ValueError("TRUSTED_HOSTS debe listar hosts explicitos (sin '*') en staging/produccion.")
        return self


settings = Settings()
