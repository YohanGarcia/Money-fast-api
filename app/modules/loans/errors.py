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


class PaymentExceedsDueAmount(AppError):
    status_code, code = 422, "payment_exceeds_due_amount"
    default_message = "El pago supera lo exigible a la fecha. Adelantos, abonos a capital, prepago y liquidacion estan fuera de esta version."


class LoanNotPayable(AppError):
    status_code, code = 409, "loan_not_payable"
    default_message = "El prestamo no admite pagos en su estado actual."


class CurrencyMismatch(AppError):
    status_code, code = 422, "currency_mismatch"
    default_message = "La moneda del pago debe ser la del prestamo."


class DuplicateExternalReference(AppError):
    status_code, code = 409, "duplicate_external_reference"
    default_message = "Esa referencia externa ya fue registrada en la agencia."


class AllocationPolicyBlocked(AppError):
    status_code, code = 409, "allocation_policy_blocked_by_spec"
    default_message = (
        "BLOCKED_BY_SPEC: este contrato usa component_then_installment; solo installment_then_component esta definido."
    )


class PaymentInvariantViolation(AppError):
    status_code, code = 409, "payment_invariant_violation"
    default_message = (
        "La base de datos rechazo el pago por violar un invariante (aplicacion exacta o sobre-aplicacion)."
    )
