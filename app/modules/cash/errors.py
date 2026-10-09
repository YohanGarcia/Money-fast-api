from app.core.errors import AppError


class CashPointNotFound(AppError):
    status_code, code = 404, "cash_point_not_found"
    default_message = "Caja (punto de caja) no encontrada."


class CashSessionNotFound(AppError):
    status_code, code = 404, "cash_session_not_found"
    default_message = "Jornada de caja no encontrada."


class HandoverNotFound(AppError):
    status_code, code = 404, "cash_handover_not_found"
    default_message = "Entrega de cierre no encontrada."


class CashNotEnabled(AppError):
    status_code, code = 409, "cash_unavailable"
    default_message = "Caja no esta habilitada en esta agencia o la sucursal no tiene caja configurada."


class CashPointNotActive(AppError):
    status_code, code = 409, "cash_point_not_active"
    default_message = "La caja esta suspendida o inactiva: no admite una nueva jornada."


class CashPointBusy(AppError):
    status_code, code = 409, "cash_point_busy"
    default_message = "La caja ya tiene una jornada abierta o en cierre: una sola jornada activa por caja."


class InvalidOpening(AppError):
    status_code, code = 422, "invalid_cash_opening"
    default_message = "Apertura invalida: el origen es cero o capital, con importe y conteo por denominaciones exactos."


class InvalidCount(AppError):
    status_code, code = 422, "invalid_cash_count"
    default_message = "Conteo por denominaciones invalido."


class ObservationRequired(AppError):
    status_code, code = 422, "observation_required"
    default_message = "Hay una diferencia entre lo contado y lo esperado: describe lo observado."


class InsufficientCapital(AppError):
    status_code, code = 409, "insufficient_capital"
    default_message = "El capital disponible no alcanza para este fondo de apertura."


class NotSessionOwner(AppError):
    status_code, code = 403, "cash_session_not_owned"
    default_message = "Solo el cajero duenio de la jornada puede cerrarla."


class SessionNotOpen(AppError):
    status_code, code = 409, "cash_session_not_open"
    default_message = "La jornada no esta abierta."


class ReceiverRequired(AppError):
    status_code, code = 422, "receiver_required"
    default_message = (
        "Hay efectivo que entregar: indica quien lo recibira (distinto del cajero, con permiso de recepcion)."
    )


class InvalidReceiver(AppError):
    status_code, code = 422, "invalid_receiver"
    default_message = "El receptor debe ser un usuario activo de la agencia, distinto del cajero y con permiso para recibir en esta sucursal."


class HandoverNotPending(AppError):
    status_code, code = 409, "cash_handover_not_pending"
    default_message = "La entrega de cierre ya fue confirmada."


class MakerCannotAccept(AppError):
    status_code, code = 403, "maker_checker_violation"
    default_message = "Quien entrega el efectivo no puede confirmar su propia entrega."


class NotNamedReceiver(AppError):
    status_code, code = 403, "not_named_receiver"
    default_message = "Solo el receptor indicado al cerrar puede confirmar esta entrega."


class OnBehalfOpeningForbidden(AppError):
    status_code, code = 403, "on_behalf_opening_forbidden"
    default_message = "Cada cajero abre su propia jornada: no se abre caja a nombre de otra persona."


class DifferenceReviewNotAvailable(AppError):
    status_code, code = 409, "difference_review_not_available"
    default_message = (
        "La revision de diferencias pertenece a un paquete posterior: una jornada cerrada no se reabre ni se ajusta."
    )


class DifferenceNotFound(AppError):
    status_code, code = 404, "cash_difference_not_found"
    default_message = "Diferencia de caja no encontrada."


class DifferenceNotPending(AppError):
    status_code, code = 409, "difference_not_pending"
    default_message = "La diferencia ya fue resuelta."


class SessionNotClosed(AppError):
    status_code, code = 409, "session_not_closed"
    default_message = "La jornada aun no esta cerrada: sus diferencias se resuelven despues del cierre definitivo."


class MakerCannotResolve(AppError):
    status_code, code = 403, "maker_checker_violation"
    default_message = "Quien abrio, opero, cerro o detecto la diferencia de la jornada no puede resolverla: la revision es independiente."


class ResolutionNotApplicable(AppError):
    status_code, code = 422, "resolution_type_not_applicable"
    default_message = (
        "Ese tipo de resolucion no aplica: la perdida solo resuelve un faltante y el sobrante un sobrante, ambos de "
        "cierre; una diferencia de apertura solo se resuelve sin accion adicional."
    )


class ResolutionReasonRequired(AppError):
    status_code, code = 422, "resolution_reason_required"
    default_message = "Indica el motivo de la resolucion (al menos 10 caracteres)."


class ResolutionReferenceRequired(AppError):
    status_code, code = 422, "resolution_reference_required"
    default_message = "Indica la referencia que respalda la decision (acta, informe o documento)."
