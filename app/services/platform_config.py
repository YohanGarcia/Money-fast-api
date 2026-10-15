"""The platform owner's global configuration.

One JSON document (``platform_settings`` row 1). Some keys drive behaviour that the code reads from the global
``settings`` object (security thresholds, SMTP, PayPal, public registration): they are applied onto it at startup and
whenever the owner saves, and other worker processes re-read them every few seconds. Secrets are write-only: they are
never returned, only ``<key>_set`` flags."""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.platform import PlatformSetting

SECRET_KEYS = {"smtp_password", "paypal_secret"}

# key -> default. Anything not listed here is rejected by the API.
DEFAULTS: dict[str, Any] = {
    # access
    "registration_open": True,
    "default_plan_id": None,  # plan a new company starts with (None = the free plan)
    "trial_days": 0,  # days of the default plan before a new company falls back to the free plan
    # billing
    "annual_discount_percent": 0,  # applied to 12 x monthly when a plan has no explicit annual price
    "grace_days": 3,  # days after the expiry before the plan drops to the free one
    "reminder_days": [7, 3, 1],  # days before the expiry a reminder is sent
    "allow_monthly": True,
    "allow_annual": True,
    # communication
    "support_email": "",
    "support_phone": "",
    "maintenance_mode": False,
    "maintenance_message": "",
    "announcement_enabled": False,
    "announcement_level": "info",  # info | warning | critical
    "announcement_title": "",
    "announcement_message": "",
    "announcement_until": None,  # ISO date/datetime or None
    # security
    "password_min_length": 10,
    "account_lock_failures": 10,
    "account_lock_seconds": 900,
    "access_token_minutes": 30,
    "refresh_token_days": 7,
    # e-mail
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from_email": "noreply@moneyfast.com",
    "smtp_from_name": "MoneyFast",
    # PayPal
    "paypal_mode": "sandbox",
    "paypal_client_id": "",
    "paypal_secret": "",
    "paypal_webhook_id": "",
}

# values.key -> attribute of the global settings object it drives
RUNTIME_MAP = {
    "registration_open": "public_registration",
    "password_min_length": "password_min_length",
    "account_lock_failures": "auth_account_lock_failures",
    "account_lock_seconds": "auth_account_lock_seconds",
    "access_token_minutes": "access_token_expire_minutes",
    "refresh_token_days": "refresh_token_expire_days",
    "smtp_host": "smtp_host",
    "smtp_port": "smtp_port",
    "smtp_user": "smtp_user",
    "smtp_password": "smtp_password",
    "smtp_from_email": "smtp_from_email",
    "smtp_from_name": "smtp_from_name",
    "paypal_mode": "paypal_mode",
    "paypal_client_id": "paypal_client_id",
    "paypal_secret": "paypal_secret",
    "paypal_webhook_id": "paypal_webhook_id",
}

_ENV_DEFAULTS: dict[str, Any] = {}
_cache: dict[str, Any] = {"values": None, "at": 0.0, "stamp": None}
REFRESH_SECONDS = 15


def _capture_env_defaults() -> None:
    if not _ENV_DEFAULTS:
        for key, attr in RUNTIME_MAP.items():
            _ENV_DEFAULTS[key] = getattr(settings, attr)


def get_row(db: Session) -> PlatformSetting:
    row = db.get(PlatformSetting, 1)
    if row is None:
        row = PlatformSetting(id=1, values={})
        db.add(row)
        db.flush()
    return row


def _merged(stored: dict) -> dict:
    """Defaults < the process environment (for the keys that exist there) < what the owner saved."""
    _capture_env_defaults()
    out = dict(DEFAULTS)
    for key, value in _ENV_DEFAULTS.items():
        if key == "registration_open":
            out[key] = True if value is None else bool(value)
        elif value not in (None, ""):
            out[key] = value
    out.update({k: v for k, v in (stored or {}).items() if k in DEFAULTS})
    return out


def load(db: Session) -> dict:
    values = _merged(get_row(db).values)
    _cache.update(values=values, at=time.monotonic(), stamp=get_row(db).updated_at)
    return values


def apply_runtime(values: dict) -> None:
    """Push the values that drive existing code onto the global settings object."""
    for key, attr in RUNTIME_MAP.items():
        if key not in values:
            continue
        value = values[key]
        if key == "registration_open":
            setattr(settings, attr, bool(value))
        elif value is not None:
            try:
                setattr(settings, attr, value)
            except Exception:  # a frozen / typed attribute must never take the app down
                pass


def refresh_if_stale(db: Session) -> None:
    """Cheap call for hot paths: re-reads the stored document at most every REFRESH_SECONDS."""
    now = time.monotonic()
    if _cache["values"] is not None and now - _cache["at"] < REFRESH_SECONDS:
        return
    row = get_row(db)
    if _cache["stamp"] != row.updated_at or _cache["values"] is None:
        values = _merged(row.values)
        apply_runtime(values)
        _cache.update(values=values, stamp=row.updated_at)
    _cache["at"] = now


def current(db: Session) -> dict:
    refresh_if_stale(db)
    return _cache["values"] or load(db)


def save(db: Session, changes: dict, updated_by: int | None) -> dict:
    row = get_row(db)
    stored = dict(row.values or {})
    for key, value in changes.items():
        if key not in DEFAULTS:
            continue
        if key in SECRET_KEYS and value is None:
            continue  # secrets keep their current value unless explicitly replaced / cleared
        stored[key] = value
    row.values = stored
    row.updated_by = updated_by
    row.updated_at = datetime.now(UTC)
    db.flush()
    values = _merged(stored)
    apply_runtime(values)
    _cache.update(values=values, at=time.monotonic(), stamp=row.updated_at)
    return values


def public_view(values: dict) -> dict:
    """The document with every secret replaced by a ``<key>_set`` flag."""
    out = {k: v for k, v in values.items() if k not in SECRET_KEYS}
    for key in SECRET_KEYS:
        out[f"{key}_set"] = bool(values.get(key))
    return out


def announcement(values: dict) -> dict | None:
    """The announcement currently in force, or None."""
    if not values.get("announcement_enabled") or not (values.get("announcement_message") or "").strip():
        return None
    until = values.get("announcement_until")
    if until:
        try:
            end = datetime.fromisoformat(str(until))
            end = end if end.tzinfo else end.replace(tzinfo=UTC)
            if end < datetime.now(UTC):
                return None
        except ValueError:
            pass
    return {
        "level": values.get("announcement_level") or "info",
        "title": values.get("announcement_title") or "",
        "message": values.get("announcement_message") or "",
        "until": until,
    }
