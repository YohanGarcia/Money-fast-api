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
