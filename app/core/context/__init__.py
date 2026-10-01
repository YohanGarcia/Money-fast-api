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


class _Holder:
    """Mutable cell shared by every copy of the contextvar.

    FastAPI runs sync dependencies in a worker thread with a *copy* of the context;
    rebinding the variable there would be lost. Mutating this shared cell is visible
    to the whole request (handlers, logging, the middleware's access log).
    """

    __slots__ = ("ctx",)

    def __init__(self, ctx: RequestContext) -> None:
        self.ctx = ctx


_current: ContextVar[_Holder | None] = ContextVar("request_context", default=None)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def sanitize_correlation_id(value: str | None) -> str | None:
    """Return ``value`` if it is a safe identifier, else ``None``."""
    if value and _VALID_CORRELATION_ID.fullmatch(value):
        return value
    return None


def get_context() -> RequestContext | None:
    holder = _current.get()
    return holder.ctx if holder else None


def set_context(ctx: RequestContext) -> Token:
    return _current.set(_Holder(ctx))


def reset_context(token: Token) -> None:
    _current.reset(token)


def bind_context(**changes: str | None) -> RequestContext:
    """Replace fields of the active context (used by the authenticated resolver)."""
    holder = _current.get()
    if holder is None:
        raise RuntimeError("No hay contexto de request activo.")
    holder.ctx = replace(holder.ctx, **changes)
    return holder.ctx
