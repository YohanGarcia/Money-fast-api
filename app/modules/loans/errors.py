from app.core.errors import AppError


class FormalizationNotReady(AppError):
    status_code, code = 409, "formalization_not_ready"
    default_message = "Solo un contrato formalizado y listo para desembolso puede desembolsarse."


class ContractIntegrityFailed(AppError):
    status_code, code = 409, "contract_integrity_failed"
    default_message = "El contrato formalizado no supera la verificacion de integridad (hash/snapshot)."


class AlreadyDisbursed(AppError):
    status_code, code = 409, "already_disbursed"
    default_message = "Este contrato ya fue desembolsado con otros datos."


class DisbursementBlockedBySpec(AppError):
    status_code, code = 409, "disbursement_blocked_by_spec"
    default_message = "BLOCKED_BY_SPEC: la regla de negocio de este desembolso no esta definida."


class ScheduleGenerationFailed(AppError):
    status_code, code = 409, "schedule_generation_failed"
    default_message = "No fue posible generar el cronograma contractual."
