"""Domain-level API errors. Each maps to one stable ``code`` and HTTP status."""

from typing import Any


class AppError(Exception):
    status_code = 500
    code = "internal_error"
    default_message = "Error interno."

    def __init__(self, message: str | None = None, *, details: Any = None) -> None:
        self.message = message or self.default_message
        self.details = details
        super().__init__(self.message)


class ValidationFailed(AppError):
    status_code, code, default_message = 422, "validation_error", "Datos invalidos."


class AuthenticationFailed(AppError):
    status_code, code, default_message = 401, "authentication_failed", "No autenticado."


class AuthorizationDenied(AppError):
    status_code, code, default_message = 403, "authorization_denied", "No autorizado."


class ResourceNotFound(AppError):
    status_code, code, default_message = 404, "not_found", "Recurso no encontrado."


class Conflict(AppError):
    status_code, code, default_message = 409, "conflict", "Conflicto con el estado actual."


class IdempotencyConflict(AppError):
    status_code, code = 409, "idempotency_conflict"
    default_message = "La clave de idempotencia pertenece a otra operacion."


class BusinessRuleViolation(AppError):
    status_code, code = 422, "business_rule_violation"
    default_message = "La operacion viola una regla de negocio."


class InternalError(AppError):
    status_code, code, default_message = 500, "internal_error", "Error interno."


class ServiceUnavailable(AppError):
    status_code, code, default_message = 503, "service_unavailable", "Servicio no disponible."
