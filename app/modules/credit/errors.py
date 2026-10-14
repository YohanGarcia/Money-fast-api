from app.core.errors import AppError


class ProductValidationFailed(AppError):
    """The rules are incomplete/invalid: ``details`` lists every issue (path, code, message)."""

    status_code, code = 422, "product_validation_failed"
    default_message = "La configuracion del producto no es valida."


class VersionImmutable(AppError):
    status_code, code = 409, "version_immutable"
    default_message = "La version esta publicada y no se puede modificar; crea una nueva version."


class RulesIntegrityFailed(AppError):
    status_code, code = 409, "rules_integrity_failed"
    default_message = "El hash de reglas no coincide con el contenido almacenado."


class RowVersionConflict(AppError):
    status_code, code = 409, "version_conflict"
    default_message = "El registro cambio. Recarga antes de guardar."


class SimulationRejected(AppError):
    status_code, code = 422, "simulation_rejected"
    default_message = "La simulacion no es posible con esos datos."
