"""Functional security audit. Secrets can never be stored: sensitive keys are dropped and values scrubbed."""

import re

from sqlalchemy.orm import Session

from app.core.context import get_context
from app.core.logging import scrub
from app.modules.identity.models import SecurityEvent

_FORBIDDEN_KEY = re.compile(r"pass|secret|token|hash|authorization|cookie|credential|api_?key|private_?key|otp", re.I)
MAX_VALUE = 200


def _clean_value(value):
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, dict):  # before/after snapshots
        return clean_details(value)
    if isinstance(value, (list, tuple)):
        return [_clean_value(v) for v in value][:50]
    return scrub(str(value))[:MAX_VALUE]


def clean_details(details: dict | None) -> dict:
    return {k: _clean_value(v) for k, v in (details or {}).items() if not _FORBIDDEN_KEY.search(k)}


def record_event(
    db: Session,
    event_type: str,
    *,
    outcome: str = "success",
    tenant_id: int | None = None,
    actor_id: int | None = None,
    subject_id: int | None = None,
    client_ip: str | None = None,
    details: dict | None = None,
) -> SecurityEvent:
    """Add an event to the caller's transaction (committed together with the change it describes)."""
    ctx = get_context()
    event = SecurityEvent(
        event_type=event_type,
        outcome=outcome,
        tenant_id=tenant_id,
        actor_id=actor_id,
        subject_id=subject_id,
        correlation_id=ctx.correlation_id if ctx else None,
        client_ip=client_ip,
        details=clean_details(details),
    )
    db.add(event)
    return event
