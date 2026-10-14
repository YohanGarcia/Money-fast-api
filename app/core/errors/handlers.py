"""Uniform error envelope.

Body: ``{"error": {"code", "message", "correlation_id", "details"?}, "detail": ...}``.
``detail`` is kept only as a transitional alias for clients that still read the
legacy FastAPI shape; it never carries internal information for 5xx errors.
Stack traces, exception text and request input are never returned.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import get_context
from app.core.errors.exceptions import AppError

log = logging.getLogger("app.errors")

INTERNAL_MESSAGE = "Error interno."
_HTTP_CODES = {
    400: "bad_request",
    401: "authentication_failed",
    403: "authorization_denied",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "business_rule_violation",
    429: "rate_limited",
    503: "service_unavailable",
}


def error_body(code: str, message: str, *, details: Any = None, detail: Any = None) -> dict:
    ctx = get_context()
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "correlation_id": ctx.correlation_id if ctx else None,
    }
    if details is not None:
        error["details"] = details
    return {"error": error, "detail": message if detail is None else detail}


def internal_error_response() -> JSONResponse:
    return JSONResponse(error_body("internal_error", INTERNAL_MESSAGE), status_code=500)


async def _app_error(_: Request, exc: AppError) -> JSONResponse:
    if exc.status_code >= 500:
        log.error("app_error", extra={"code": exc.code})
        return JSONResponse(error_body(exc.code, exc.default_message), status_code=exc.status_code)
    return JSONResponse(
        error_body(exc.code, exc.message, details=exc.details), status_code=exc.status_code, headers=exc.headers
    )


async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code >= 500:
        log.error("http_error", extra={"status": exc.status_code})
        body = error_body(_HTTP_CODES.get(exc.status_code, "internal_error"), INTERNAL_MESSAGE)
    else:
        is_text = isinstance(exc.detail, str)
        body = error_body(
            _HTTP_CODES.get(exc.status_code, "request_error"),
            exc.detail if is_text else "Solicitud invalida.",
            details=None if is_text else exc.detail,
        )
    return JSONResponse(body, status_code=exc.status_code, headers=getattr(exc, "headers", None))


async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    # Drop ``input``/``ctx``: they can echo passwords or other submitted secrets.
    issues = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
    body = error_body("validation_error", "Datos invalidos.", details=issues, detail=issues)
    return JSONResponse(body, status_code=422)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
