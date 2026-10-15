"""Before/after values of the rows a request changed, for the activity log.

A ``before_flush`` listener looks at the tracked models' attribute history and appends what changed to a list
owned by the current request (created by ``AuditMiddleware``). Outside a request nothing is collected.
"""

from contextvars import ContextVar

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

_changes: ContextVar[list | None] = ContextVar("activity_changes", default=None)
TRACKED = {
    "Customer", "Loan", "LoanApplication", "CompanySettings", "LoanSettings", "PrintSettings", "User", "Route",
    "Branch", "BankAccount", "PayrollConfig",
}
IGNORED = {"version", "updated_at", "created_at", "last_login_at", "last_seen_at", "latitude", "longitude", "failed_login_count"}


def start() -> object:
    return _changes.set([])


def finish(token: object) -> list:
    collected = _changes.get() or []
    _changes.reset(token)  # type: ignore[arg-type]
    return collected


def hint(summary: str, *, entity_type: str | None = None, entity_id=None) -> None:
    """Let a handler name what the request did (the middleware only knows the route)."""
    sink = _changes.get()
    if sink is not None:
        sink.append({"hint": {"summary": summary, "entity_type": entity_type, "entity_id": entity_id}, "fields": {}})


@event.listens_for(Session, "after_flush")
def _created(session: Session, _ctx) -> None:
    sink = _changes.get()
    if sink is None:
        return
    for obj in session.new:
        name = type(obj).__name__
        if name in TRACKED and getattr(obj, "id", None) is not None:
            sink.append({"entity": name, "id": obj.id, "fields": {}, "created": True})


@event.listens_for(Session, "before_flush")
def _collect(session: Session, _ctx, _instances) -> None:
    sink = _changes.get()
    if sink is None:
        return
    for obj in session.dirty:
        name = type(obj).__name__
        if name not in TRACKED:
            continue
        fields = {}
        state = inspect(obj)
        for attr in state.mapper.column_attrs:
            if attr.key in IGNORED:
                continue
            hist = state.attrs[attr.key].history
            if hist.has_changes() and hist.deleted:
                fields[attr.key] = [hist.deleted[0], hist.added[0] if hist.added else None]
        if fields:
            sink.append({"entity": name, "id": getattr(obj, "id", None), "fields": fields})
