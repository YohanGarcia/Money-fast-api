"""Delivery of one-time secrets (recovery / activation).

The secret travels only through the notifier. It is never logged, returned by an API, or written to audit.
When no channel is configured the secret is simply not delivered (the caller audits the failure).
"""

import logging
from datetime import datetime
from typing import Protocol

from app.services.email_service import EmailNotConfigured, send_html_email, smtp_configured

log = logging.getLogger("app.identity.notifications")

_SUBJECTS = {"recovery": "Recuperacion de acceso - MoneyFast", "activation": "Activa tu cuenta - MoneyFast"}
_INTRO = {
    "recovery": "Recibimos una solicitud para restablecer tu contrasena.",
    "activation": "Se creo una cuenta para ti. Usa el siguiente codigo para elegir tu contrasena.",
}


class SecretNotifier(Protocol):
    def send(self, *, email: str, secret: str, purpose: str, expires_at: datetime) -> bool:
        """Deliver ``secret``; return False when it could not be delivered. Must not log ``secret``."""


class SmtpSecretNotifier:
    def send(self, *, email: str, secret: str, purpose: str, expires_at: datetime) -> bool:
        if not smtp_configured():
            log.warning("secret_delivery_unavailable", extra={"purpose": purpose})
            return False
        html = (
            f"<p>{_INTRO[purpose]}</p><p style='font-size:18px'><b>{secret}</b></p>"
            f"<p>Expira el {expires_at:%Y-%m-%d %H:%M} UTC y solo puede usarse una vez. "
            "Si no lo solicitaste, ignora este mensaje.</p>"
        )
        try:
            send_html_email(email, _SUBJECTS[purpose], html)
        except (EmailNotConfigured, OSError, Exception):  # noqa: BLE001 - delivery must never break the flow
            log.warning("secret_delivery_failed", extra={"purpose": purpose})
            return False
        return True


def get_notifier() -> SecretNotifier:
    """FastAPI dependency; tests override it with an in-memory sink."""
    return SmtpSecretNotifier()
