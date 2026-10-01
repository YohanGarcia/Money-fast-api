from app.core.errors import AppError


class DuplicateIdentity(AppError):
    """EXACT_MATCH: the identity already exists in this tenant. Nothing is created and nothing is merged."""

    status_code, code = 409, "duplicate_identity"
    default_message = "Ya existe una identidad con ese documento en esta agencia."


class PossibleDuplicate(AppError):
    """POSSIBLE_MATCH: weak signals only. The caller must review and explicitly acknowledge to continue."""

    status_code, code = 409, "possible_duplicate"
    default_message = "Hay posibles duplicados. Revisalos y confirma para continuar."


class VersionConflict(AppError):
    status_code, code = 409, "version_conflict"
    default_message = "La ficha cambio. Recarga antes de guardar."
