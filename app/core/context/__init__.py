"""Request context carried through a request via ``contextvars``.

``tenant_id``, ``branch_id``, ``actor_id`` and ``timezone`` are filled only by
the authenticated resolver (T-002/T-003). They must never be copied from client
supplied headers or payloads.
"""

import re
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace

CORRELATION_HEADER = "X-Correlation-ID"
_VALID_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._-]{8,128}$")


@dataclass(frozen=True, slots=True)
class RequestContext:
    correlation_id: str
    actor_id: str | None = None
    tenant_id: str | None = None
    branch_id: str | None = None
    timezone: str | None = None  # effective IANA zone; None = platform default


_current: ContextVar[RequestContext | None] = ContextVar("request_context", default=None)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def sanitize_correlation_id(value: str | None) -> str | None:
    """Return ``value`` if it is a safe identifier, else ``None``."""
    if value and _VALID_CORRELATION_ID.fullmatch(value):
        return value
    return None


def get_context() -> RequestContext | None:
    return _current.get()


def set_context(ctx: RequestContext) -> Token:
    return _current.set(ctx)


def reset_context(token: Token) -> None:
    _current.reset(token)


def bind_context(**changes: str | None) -> RequestContext:
    """Replace fields of the active context (used by future auth resolvers)."""
    ctx = get_context()
    if ctx is None:
        raise RuntimeError("No hay contexto de request activo.")
    updated = replace(ctx, **changes)
    _current.set(updated)
    return updated
