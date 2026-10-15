"""Activity-log catalog: which business operation a mutating request is, in Spanish.

Every mutating ``/api/v1`` request is recorded by ``AuditMiddleware``; this table gives it a category, an action
and a readable label. A request that is not listed is still recorded under ``other`` with its route, so nothing
escapes the log.
"""

import re

CATEGORIES: dict[str, str] = {
    "auth": "Sesiones y acceso",
    "customers": "Clientes",
    "applications": "Solicitudes",
    "loans": "Préstamos",
    "payments": "Pagos",
    "cash": "Caja",
    "routes": "Rutas",
    "users": "Usuarios",
    "branches": "Sucursales",
    "settings": "Configuración",
    "payroll": "Nómina",
    "billing": "Plan y facturación",
    "platform": "Plataforma",
    "other": "Otros",
}

# (method, regex on the path after /api/v1) -> (category, action, entity_type, label)
_RULES: list[tuple[str, str, str, str, str | None, str]] = [
    ("POST", r"/auth/register", "auth", "register", "company", "Registró una empresa"),
    ("POST", r"/auth/(request-password-reset|verify-reset-code|reset-password)", "auth", "password_reset", "user", "Recuperación de contraseña"),
    ("POST", r"/auth/(login|refresh|logout)", "auth", "session", "user", "Sesión"),
    ("POST", r"/customers", "customers", "created", "customer", "Creó un cliente"),
    ("PUT", r"/customers/(?P<id>\d+)", "customers", "updated", "customer", "Editó un cliente"),
    ("POST", r"/loan-applications", "applications", "created", "application", "Creó una solicitud de préstamo"),
    ("PUT", r"/loan-applications/(?P<id>\d+)", "applications", "updated", "application", "Editó una solicitud"),
    ("POST", r"/loan-applications/(?P<id>\d+)/customer", "applications", "customer_linked", "application", "Asignó el cliente a la solicitud"),
    ("POST", r"/loan-applications/(?P<id>\d+)/documents/[^/]+", "applications", "document_uploaded", "application", "Cargó un documento"),
    ("POST", r"/loan-applications/(?P<id>\d+)/documents/[^/]+/review", "applications", "document_reviewed", "application", "Revisó un documento"),
    ("POST", r"/loan-applications/(?P<id>\d+)/transition", "applications", "transition", "application", "Cambió la etapa de la solicitud"),
    ("POST", r"/loans", "loans", "created", "loan", "Creó un préstamo"),
    ("POST", r"/loans/(?P<id>\d+)/approve", "loans", "approved", "loan", "Aprobó un préstamo"),
    ("POST", r"/loans/(?P<id>\d+)/reject", "loans", "rejected", "loan", "Rechazó un préstamo"),
    ("POST", r"/payments", "payments", "created", "payment", "Registró un pago"),
    ("POST", r"/cash/commands", "cash", "command", "cash", "Operación de caja"),
    ("POST", r"/cash/.*", "cash", "operation", "cash", "Operación de caja"),
    ("POST", r"/routes", "routes", "created", "route", "Creó una ruta"),
    ("PUT", r"/routes/(?P<id>\d+)", "routes", "updated", "route", "Editó una ruta"),
    ("DELETE", r"/routes/(?P<id>\d+)", "routes", "deleted", "route", "Eliminó una ruta"),
    ("POST", r"/users", "users", "created", "user", "Creó un usuario"),
    ("PUT", r"/users/(?P<id>\d+)", "users", "updated", "user", "Editó un usuario"),
    ("POST", r"/branches", "branches", "created", "branch", "Creó una sucursal"),
    ("PUT", r"/branches/(?P<id>\d+)", "branches", "updated", "branch", "Editó una sucursal"),
    ("DELETE", r"/branches/(?P<id>\d+)", "branches", "deleted", "branch", "Eliminó una sucursal"),
    ("POST", r"/bank-accounts", "settings", "bank_account_created", "bank_account", "Creó una cuenta bancaria"),
    ("PUT", r"/bank-accounts/(?P<id>\d+)", "settings", "bank_account_updated", "bank_account", "Editó una cuenta bancaria"),
    ("DELETE", r"/bank-accounts/(?P<id>\d+)", "settings", "bank_account_deleted", "bank_account", "Eliminó una cuenta bancaria"),
    ("PUT", r"/company-settings", "settings", "company_updated", "company", "Actualizó los datos de la empresa"),
    ("PUT", r"/loan-settings", "settings", "loan_rules_updated", "loan_settings", "Actualizó las reglas de préstamo"),
    ("PUT", r"/print-settings", "settings", "print_updated", "print_settings", "Actualizó la impresión"),
    ("PUT", r"/payroll/config/(?P<id>\d+)", "payroll", "config_updated", "user", "Configuró la nómina de un empleado"),
    ("POST", r"/payroll/payments", "payroll", "paid", "user", "Registró un pago de nómina"),
    ("POST", r"/billing/.*", "billing", "transfer", "subscription", "Plan y facturación"),
    ("POST", r"/subscriptions/.*", "billing", "subscription", "subscription", "Suscripción"),
    ("PUT", r"/tour-progress/.*", "other", "tour", None, "Progreso del tutorial"),
    ("DELETE", r"/tour-progress/.*", "other", "tour", None, "Reinició un tutorial"),
    ("POST", r"/users/me/location", "other", "location", None, "Ubicación compartida"),
]
_COMPILED = [(m, re.compile(f"^{p}/?$"), c, a, e, label) for m, p, c, a, e, label in _RULES]

_PREFIX_CATEGORY = {
    "auth": "auth", "account": "auth", "session": "auth", "password": "auth",
    "user": "users", "legacy": "users", "platform": "platform", "billing": "billing", "cash": "cash",
    "customer": "customers", "loan": "loans", "payment": "payments", "route": "routes", "branch": "branches",
}


def classify(method: str, path: str) -> dict:
    """Describe a mutating request. ``path`` is relative to ``/api/v1``."""
    method = method.upper()
    if path.startswith("/platform/"):
        return {"category": "platform", "action": "platform", "entity_type": None, "entity_id": None, "label": "Acción de plataforma"}
    for m, rx, category, action, entity, label in _COMPILED:
        if m != method:
            continue
        hit = rx.match(path)
        if hit:
            groups = hit.groupdict()
            return {"category": category, "action": action, "entity_type": entity, "entity_id": groups.get("id"), "label": label}
    return {"category": "other", "action": "other", "entity_type": None, "entity_id": None, "label": f"{method} {path}"}


def category_of_event(event_type: str) -> str:
    """Category of an explicit event written before the log existed (``customer.created`` -> ``customers``)."""
    return _PREFIX_CATEGORY.get(event_type.split(".", 1)[0], "other")


def category_prefixes(category: str) -> list[str]:
    """Event-type prefixes that belong to a category (for rows written before the category column existed)."""
    return [p for p, c in _PREFIX_CATEGORY.items() if c == category]


EVENT_LABELS: dict[str, str] = {
    "auth.login.succeeded": "Inició sesión",
    "auth.login.failed": "Intento de inicio de sesión fallido",
    "auth.login.denied": "Inicio de sesión denegado",
    "auth.logout": "Cerró sesión",
    "account.locked": "Cuenta bloqueada por intentos fallidos",
    "account.unlocked": "Cuenta desbloqueada",
    "password.changed": "Cambió su contraseña",
    "password.change_failed": "Intento fallido de cambio de contraseña",
    "recovery.requested": "Solicitó recuperar la contraseña",
    "recovery.completed": "Recuperó su contraseña",
    "session.revoked": "Sesión revocada",
    "user.created": "Creó un usuario",
    "user.disabled": "Desactivó un usuario",
    "user.enabled": "Activó un usuario",
    "legacy.role_changed": "Cambió el rol de un usuario",
    "role.assigned": "Asignó un rol",
    "role.removed": "Quitó un rol",
    "customer.created": "Creó un cliente",
    "customer.updated": "Editó un cliente",
    "payment.confirmed": "Confirmó un pago",
    "payment.reversed": "Revirtió un pago",
    "cash.session.opened": "Abrió la caja",
    "cash.session.closed": "Cerró la caja",
    "cash.difference.detected": "Detectó una diferencia de caja",
    "cash.differences.resolve": "Resolvió una diferencia de caja",
    "cash.field_custody.accept": "Recibió efectivo de un cobrador",
    "cash.field_custody.refund": "Devolvió efectivo a un cobrador",
    "billing.transfer.notified": "Avisó una transferencia de su plan",
    "billing.transfer.cancelled": "Canceló una transferencia de su plan",
    "billing.plan.downgraded": "El plan pasó a Gratis",
    "credit_application.created": "Creó una solicitud de crédito",
    "credit_application.approved": "Aprobó una solicitud de crédito",
    "credit_application.rejected": "Rechazó una solicitud de crédito",
    "credit_application.formalized": "Formalizó una solicitud de crédito",
}
# Read/telemetry events written by the v2 modules: not user movements.
NOISE_SUFFIXES = (".read", ".read_sensitive", ".reads")


def label_for(event_type: str) -> str | None:
    return EVENT_LABELS.get(event_type)
