from app.core.errors import AppError


class CustodyReceiptNotFound(AppError):
    status_code, code = 404, "custody_receipt_not_found"
    default_message = "Uno o mas pagos no tienen custodia de campo registrada (pago de ventanilla, anterior a T-019 o de otra agencia)."


class NotCustodian(AppError):
    status_code, code = 403, "not_custodian"
    default_message = "Solo el custodio fisico del efectivo puede declarar o cancelar su rendicion."


class CustodyBranchMismatch(AppError):
    status_code, code = 422, "custody_branch_mismatch"
    default_message = (
        "Todos los pagos deben haberse recibido en la sucursal de la rendicion (no hay rendicion entre sucursales)."
    )


class ReceiptAlreadyClaimed(AppError):
    status_code, code = 409, "receipt_already_claimed"
    default_message = "Uno o mas pagos ya estan en una rendicion declarada o aceptada."


class RenditionNotFound(AppError):
    status_code, code = 404, "rendition_not_found"
    default_message = "Rendicion no encontrada."


class RenditionNotDeclared(AppError):
    status_code, code = 409, "rendition_not_declared"
    default_message = "La rendicion ya fue decidida (aceptada, rechazada o cancelada): no admite otra decision."


class MakerCheckerViolation(AppError):
    status_code, code = 403, "maker_checker_violation"
    default_message = "El custodio no puede aceptar ni rechazar su propia rendicion."


class RenditionCountMismatch(AppError):
    status_code, code = 409, "rendition_count_mismatch"
    default_message = (
        "El monto contado no es igual al declarado: no se acepta. Rechaza la rendicion indicando el motivo."
    )


class InvalidCustodyAmount(AppError):
    status_code, code = 422, "invalid_custody_amount"
    default_message = "El monto debe expresarse en centavos exactos y no ser negativo."


class CustodianInactive(AppError):
    status_code, code = 409, "custodian_inactive"
    default_message = "El custodio no esta activo: no puede recibir ni declarar efectivo de campo."


class OutstandingFieldCustody(AppError):
    status_code, code = 409, "outstanding_field_custody"
    default_message = (
        "El usuario tiene efectivo de campo en custodia o una rendicion declarada: resuelvelo antes de desactivarlo."
    )


# --- T-020 field refund ---
class ReversalNotFound(AppError):
    status_code, code = 404, "reversal_not_found"
    default_message = "Reversion no encontrada."


class FieldRefundNotApplicable(AppError):
    status_code, code = 409, "field_refund_not_applicable"
    default_message = (
        "La reversion de un pago de ventanilla ya devolvio el efectivo de la caja: no hay reembolso de campo."
    )


class PreCustodyNotRefundable(AppError):
    status_code, code = 409, "pre_custody_not_refundable"
    default_message = (
        "El pago de campo es anterior a la custodia (T-019): no se sabe donde esta el efectivo; no se reembolsa."
    )


class AlreadyRefunded(AppError):
    status_code, code = 409, "already_refunded"
    default_message = "Esta reversion ya tiene su reembolso fisico."


class ReceiptInDeclaredRendition(AppError):
    status_code, code = 409, "receipt_in_declared_rendition"
    default_message = "El pago esta en una rendicion declarada: cancelala o rechazala antes de reembolsar."


class RefundSessionRequired(AppError):
    status_code, code = 422, "refund_session_required"
    default_message = "El efectivo ya esta en la caja: indica cash_session_id (tu jornada abierta actual)."


class RefundSessionNotApplicable(AppError):
    status_code, code = 422, "refund_session_not_applicable"
    default_message = (
        "El efectivo sigue con el cobrador: el reembolso directo no usa jornada de caja (no envies cash_session_id)."
    )


class ReceiptRefunded(AppError):
    status_code, code = 409, "receipt_refunded"
    default_message = "Uno o mas pagos ya fueron reembolsados por su cobrador: salieron de la custodia y no se rinden."
