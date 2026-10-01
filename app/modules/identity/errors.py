"""Identity/authorization error catalogue (T-002 §23). Messages never reveal secrets or internals."""

from app.core.errors import AppError


class InvalidCredentials(AppError):
    status_code, code, default_message = 401, "invalid_credentials", "Credenciales invalidas."


class AccountLocked(AppError):
    status_code, code, default_message = 403, "account_locked", "La cuenta esta bloqueada temporalmente."


class AccountDisabled(AppError):
    status_code, code, default_message = 403, "account_disabled", "El usuario esta inactivo."


class SessionInvalid(AppError):
    status_code, code, default_message = 401, "authentication_failed", "No fue posible validar la sesion."

    def __init__(self, message: str | None = None, **kwargs) -> None:
        super().__init__(message, headers={"WWW-Authenticate": "Bearer"}, **kwargs)


class SessionExpired(SessionInvalid):
    code = "session_expired"


class RateLimited(AppError):
    status_code, code = 429, "rate_limited"
    default_message = "Demasiados intentos. Intenta de nuevo mas tarde."

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(headers={"Retry-After": str(max(1, retry_after_seconds))})
        self.retry_after_seconds = retry_after_seconds


class PermissionDenied(AppError):
    status_code, code, default_message = 403, "permission_denied", "No tienes permiso para esta accion."


class TenantMismatch(AppError):
    """Deliberately a 404: a valid id from another tenant must be indistinguishable from a missing one."""

    status_code, code, default_message = 404, "not_found", "Recurso no encontrado."


class SelfEscalationDenied(AppError):
    status_code, code = 403, "self_escalation_denied"
    default_message = "No puedes modificar tus propios privilegios."


class DelegationCeilingViolation(AppError):
    status_code, code = 403, "delegation_ceiling_violation"
    default_message = "No puedes conceder privilegios por encima de tu autoridad."


class DuplicateAssignment(AppError):
    status_code, code, default_message = 409, "duplicate_role_assignment", "El usuario ya tiene ese rol."


class InvalidStateTransition(AppError):
    status_code, code, default_message = 409, "invalid_state_transition", "Transicion de estado no permitida."


class InvalidRecoveryToken(AppError):
    status_code, code = 400, "invalid_recovery_token"
    default_message = "El enlace o codigo de recuperacion no es valido o ya expiro."


class RecoveryTokenReused(AppError):
    status_code, code = 400, "recovery_token_reused"
    default_message = "El enlace o codigo de recuperacion ya fue utilizado."


class PasswordPolicyViolation(AppError):
    status_code, code, default_message = 422, "password_policy_violation", "La contrasena no cumple la politica."


class InvalidExternalToken(AppError):
    status_code, code = 401, "invalid_external_token"
    default_message = "La identidad externa no pudo ser validada."


class ExternalIdentityNotLinked(AppError):
    """Also returned for an unknown tenant slug, so the response never reveals which tenants exist."""

    status_code, code = 401, "external_identity_not_linked"
    default_message = "Esta identidad externa no esta vinculada a una cuenta de esa agencia."


class ExternalIdentityConflict(AppError):
    status_code, code = 409, "external_identity_conflict"
    default_message = "La identidad externa ya esta vinculada a otra cuenta."


class TenantInactive(AppError):
    status_code, code = 403, "tenant_inactive"
    default_message = "La agencia esta inactiva."
