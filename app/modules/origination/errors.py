from app.core.errors import AppError


class OriginationValidationFailed(AppError):
    """The request violates the pinned product version's rules; ``details`` lists every issue."""

    status_code, code = 422, "origination_validation_failed"
    default_message = "La solicitud no cumple las reglas del producto."


class StaleApplicationVersion(AppError):
    status_code, code = 409, "stale_application_version"
    default_message = "La solicitud cambio despues de la revision. Recargala antes de decidir."


class AlreadyDecided(AppError):
    status_code, code = 409, "already_decided"
    default_message = "La solicitud ya tiene una decision distinta."


class ApplicationNotEditable(AppError):
    status_code, code = 409, "application_not_editable"
    default_message = "Solo un borrador se puede editar; reabre la solicitud con un motivo."


class MakerCheckerViolation(AppError):
    status_code, code = 403, "maker_checker_violation"
    default_message = "La politica exige que quien aprueba sea distinto de quien preparo la solicitud."


class ApprovalLimitExceeded(AppError):
    status_code, code = 403, "approval_limit_exceeded"
    default_message = "El monto aprobado supera tu limite de autorizacion."


class ApprovalPolicyNotConfigured(AppError):
    status_code, code = 409, "approval_policy_not_configured"
    default_message = (
        "BLOCKED_BY_SPEC: la agencia no ha configurado explicitamente su politica de aprobacion "
        "(maker-checker, limites, monto aprobado vs solicitado, evaluacion obligatoria)."
    )


class ProductNotAvailable(AppError):
    status_code, code = 409, "product_not_available"
    default_message = "El producto o su version no estan disponibles."


class BlockingConditionsPending(AppError):
    status_code, code = 409, "blocking_conditions_pending"
    default_message = "Hay condiciones bloqueantes pendientes."
