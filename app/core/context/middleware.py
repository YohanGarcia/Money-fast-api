"""Pure-ASGI middleware: correlation id, request context, access log, 500 safety net."""

import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import (
    CORRELATION_HEADER,
    RequestContext,
    new_correlation_id,
    reset_context,
    sanitize_correlation_id,
    set_context,
)
from app.core.errors.handlers import internal_error_response

log = logging.getLogger("app.request")
_HEADER = CORRELATION_HEADER.lower().encode()


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied = next((v.decode("latin-1") for k, v in scope["headers"] if k == _HEADER), None)
        correlation_id = sanitize_correlation_id(supplied) or new_correlation_id()
        token = set_context(RequestContext(correlation_id=correlation_id))
        started = time.perf_counter()
        status = 500
        response_started = False

        async def send_with_id(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != _HEADER]
                headers.append((CORRELATION_HEADER.encode(), correlation_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            log.exception("unhandled_exception")
            if not response_started:
                await internal_error_response()(scope, receive, send_with_id)
            # Like Starlette's own 500 handler: answer the client first, then re-raise so
            # the server/test client still see the failure.
            raise
        finally:
            log.info(
                "request_completed",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            reset_context(token)
