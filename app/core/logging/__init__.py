"""Structured JSON logging with secret redaction."""

import json
import logging
import re
import sys
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.context import get_context

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime", "taskName"}
_SENSITIVE_KEY = re.compile(
    r"pass(word)?|secret|token|authorization|api[_-]?key|cookie|credential|dsn|database_url", re.I
)
_URL_CREDENTIALS = re.compile(r"(://[^/\s:@]+:)[^@\s]+@")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
REDACTED = "[REDACTED]"


def scrub(text: str) -> str:
    """Mask URL credentials and bearer/basic tokens embedded in free text."""
    return _BEARER.sub(r"\1 " + REDACTED, _URL_CREDENTIALS.sub(r"\1" + REDACTED + "@", text))


def _redact(value, key: str | None = None):
    if key is not None and _SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {k: _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        return scrub(value)
    return value if isinstance(value, (int, float, bool, type(None))) else scrub(str(value))


class JsonFormatter(logging.Formatter):
    def __init__(self, environment: str) -> None:
        super().__init__()
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        ctx = get_context()
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "message": scrub(record.getMessage()),
            "module": record.name,
            "environment": self.environment,
            "correlation_id": ctx.correlation_id if ctx else None,
            "tenant_id": ctx.tenant_id if ctx else None,
            "user_id": ctx.actor_id if ctx else None,
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload:
                payload[key] = _redact(value, key)
        if record.exc_info:
            payload["exception"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(settings.environment))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)
    # Uvicorn access lines are replaced by our request_completed log.
    logging.getLogger("uvicorn.access").disabled = True
