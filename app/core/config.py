from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parents[2]

DEFAULT_SECRET_KEY = "change-me-in-production"


class Settings(BaseSettings):
    app_name: str = "MoneyFast API"
    environment: str = "development"
    secret_key: str = DEFAULT_SECRET_KEY
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    database_url: str = f"sqlite:///{(BASE_DIR / 'moneyfast.db').as_posix()}"

    # CORS — comma-separated list of allowed origins for the web/mobile clients.
    cors_origins: str = (
        "http://localhost:8081,http://127.0.0.1:8081,"
        "http://localhost:19006,http://127.0.0.1:19006,"
        "http://localhost:5173,http://127.0.0.1:5173"
    )

    # SMTP — dejar vacío para que el código se imprima en consola (solo desarrollo)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_email: str = "noreply@moneyfast.com"
    smtp_from_name: str = "MoneyFast"

    # PayPal
    paypal_client_id: str = ""
    paypal_secret: str = ""
    paypal_mode: str = "sandbox"  # "sandbox" | "live"
    paypal_webhook_id: str = ""   # for verifying incoming webhook signatures

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @field_validator("secret_key")
    @classmethod
    def _validate_secret_key(cls, value: str, info) -> str:
        environment = (info.data.get("environment") or "development").lower()
        if environment in {"production", "prod"}:
            if value == DEFAULT_SECRET_KEY:
                raise ValueError(
                    "SECRET_KEY no puede ser el valor por defecto en produccion. "
                    "Define una clave segura en las variables de entorno."
                )
            if len(value) < 32:
                raise ValueError("SECRET_KEY debe tener al menos 32 caracteres en produccion.")
        return value


settings = Settings()
