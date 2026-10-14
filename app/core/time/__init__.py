"""Timezone-aware time utilities (ADR-006).

Instants are stored/handled in UTC; business dates are always derived through an
IANA timezone, never from the UTC calendar date.
"""

from datetime import UTC, date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import settings


@lru_cache(maxsize=64)
def get_zone(name: str | None = None) -> ZoneInfo:
    """Return the IANA zone ``name`` (platform default when omitted)."""
    key = name or settings.default_timezone
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Zona horaria IANA invalida: {key!r}") from exc


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Se requiere un datetime con zona horaria (aware).")
    return value


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_zone(value: datetime, tz: str | None = None) -> datetime:
    return ensure_aware(value).astimezone(get_zone(tz))


def business_date(at: datetime | None = None, tz: str | None = None) -> date:
    """Calendar date of ``at`` (default: now) in the effective business timezone."""
    return to_zone(at or now_utc(), tz).date()


def parse_aware(value: str, assume_tz: str | None = None) -> datetime:
    """Parse an ISO-8601 timestamp and return it in UTC.

    A value without offset is rejected unless ``assume_tz`` names the IANA zone
    in which it must be interpreted.
    """
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        if assume_tz is None:
            raise ValueError("Timestamp sin zona horaria: indique offset o assume_tz.")
        parsed = parsed.replace(tzinfo=get_zone(assume_tz))
    return parsed.astimezone(UTC)
