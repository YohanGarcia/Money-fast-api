"""Functional security audit. Secrets can never be stored: sensitive keys are dropped and values scrubbed."""

import re

from sqlalchemy import select
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


def actor_profile(db: Session, actor_id: int | None) -> tuple[str | None, int | None]:
    """(role, branch_id) of the acting user, for the activity log. A notary is a collector account that is the
    notary of a branch."""
    if not actor_id:
        return None, None
    from app.models.branch import Branch
    from app.models.user import User

    user = db.get(User, actor_id)
    if user is None:
        return None, None
    role = user.role.value if hasattr(user.role, "value") else str(user.role)
    if role == "collector" and db.scalar(select(Branch.id).where(Branch.notary_user_id == actor_id).limit(1)) is not None:
        role = "notary"
    return role, user.branch_id


def changed_fields(before: dict, after: dict) -> dict:
    """Only what changed: ``{campo: [antes, después]}`` (secrets are dropped by ``clean_details``)."""
    out = {}
    for key in set(before) | set(after):
        if before.get(key) != after.get(key):
            out[key] = [before.get(key), after.get(key)]
    return clean_details(out)


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
    category: str | None = None,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    summary: str | None = None,
    changes: dict | None = None,
) -> SecurityEvent:
    """Add an event to the caller's transaction (committed together with the change it describes)."""
    ctx = get_context()
    if actor_id is None and ctx is not None and ctx.actor_id:
        actor_id = int(ctx.actor_id)
    if tenant_id is None and ctx is not None and ctx.tenant_id:
        tenant_id = int(ctx.tenant_id)
    role, branch_id = actor_profile(db, actor_id if actor_id is not None else subject_id)
    if category is None:
        from app.core.audit_catalog import category_of_event

        category = category_of_event(event_type)
    if summary is None:
        from app.core.audit_catalog import label_for

        summary = label_for(event_type)
    merged = clean_details(details)
    if changes:
        merged["changes"] = clean_details(changes)
    event = SecurityEvent(
        event_type=event_type,
        outcome=outcome,
        tenant_id=tenant_id,
        actor_id=actor_id,
        subject_id=subject_id,
        correlation_id=ctx.correlation_id if ctx else None,
        client_ip=client_ip,
        details=merged,
        category=category,
        actor_role=role,
        branch_id=branch_id,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        summary=(summary or "")[:240] or None,
    )
    db.add(event)
    return event
