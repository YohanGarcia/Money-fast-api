"""Records every mutating business request in the activity log (``security_events``).

Pure ASGI, after the response: by then the authenticated context holds the actor and the company. It never reads
request bodies or headers (no secrets), uses its own session and never breaks the request. When a handler
already wrote an explicit event for the same correlation id the generic one is skipped.
"""

import logging

from sqlalchemy import select
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import audit_changes
from app.core.audit_catalog import classify
from app.core.context import get_context
from app.core.db import SessionLocal
from app.modules.identity.audit import record_event
from app.modules.identity.models import SecurityEvent

log = logging.getLogger("app.audit")
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
_PREFIX = "/api/v1"
# Authentication is audited by the identity service itself (with the right outcome and actor).
_SKIP = ("/auth/",)


def _outcome(status: int) -> str:
    if status < 400:
        return "success"
    if status in (401, 403):
        return "denied"
    return "failed"


class AuditMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _MUTATING or not scope["path"].startswith(_PREFIX):
            await self.app(scope, receive, send)
            return
        status = 500
        token = audit_changes.start()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            changes = audit_changes.finish(token)
            try:
                self._record(scope, status, changes)
            except Exception:  # the log must never break a request
                log.exception("activity_log_failed")

    @staticmethod
    def _record(scope: Scope, status: int, changes: list) -> None:
        path = scope["path"][len(_PREFIX):]
        if path.startswith(_SKIP):
            return
        ctx = get_context()
        if ctx is None or not ctx.tenant_id or not ctx.actor_id:
            return  # unauthenticated or platform-level: not a company activity
        info = classify(scope["method"], path)
        merged = {}
        hints = [c["hint"] for c in changes if c.get("hint")]
        created = [c for c in changes if c.get("created")]
        changes = [c for c in changes if not c.get("created") and not c.get("hint")]
        for item in changes:  # one entity: plain field names; several: prefixed with the entity
            for field, pair in item["fields"].items():
                merged[field if len(changes) == 1 else f"{item['entity']}.{field}"] = pair
        if hints:
            info["label"] = hints[-1]["summary"] or info["label"]
            info["entity_type"] = hints[-1].get("entity_type") or info["entity_type"]
            info["entity_id"] = hints[-1].get("entity_id") or info["entity_id"]
        if info["entity_id"] is None:
            pick = created[0] if len(created) == 1 else (changes[0] if len(changes) == 1 else None)
            if pick is not None and pick["id"] is not None:
                info["entity_id"] = pick["id"]
        if info["category"] == "other" and info["action"] in ("tour", "location"):
            return  # noise: tutorial progress and location pings
        with SessionLocal() as db:
            already = db.scalar(
                select(SecurityEvent.id).where(SecurityEvent.correlation_id == ctx.correlation_id).limit(1)
            )
            if already is not None:
                return
            client = scope.get("client")
            record_event(
                db,
                f"{info['category']}.{info['action']}",
                outcome=_outcome(status),
                tenant_id=int(ctx.tenant_id),
                actor_id=int(ctx.actor_id),
                client_ip=client[0] if client else None,
                category=info["category"],
                entity_type=info["entity_type"],
                entity_id=info["entity_id"],
                summary=info["label"],
                details={"method": scope["method"], "path": path, "status": status},
                changes=merged or None,
            )
            db.commit()
