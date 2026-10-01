"""SMTP delivery. Never logs message bodies, tokens or recipient addresses."""

import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings

logger = logging.getLogger(__name__)


class EmailNotConfigured(RuntimeError):
    """SMTP settings are missing; nothing was sent."""


def smtp_configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_user and settings.smtp_password)


def send_html_email(to_email: str, subject: str, html: str) -> None:
    if not smtp_configured():
        raise EmailNotConfigured("SMTP no configurado.")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_from_email}>"
    msg["To"] = to_email
    msg.attach(MIMEText(html, "html"))
    context = ssl.create_default_context()
    try:
        if settings.smtp_port == 465:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context) as server:
                server.login(settings.smtp_user, settings.smtp_password)
                server.sendmail(settings.smtp_from_email, to_email, msg.as_string())
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
                server.ehlo()
                server.starttls(context=context)
                server.login(settings.smtp_user, settings.smtp_password)
                server.sendmail(settings.smtp_from_email, to_email, msg.as_string())
    except smtplib.SMTPException:
        logger.error("smtp_delivery_failed")  # no recipient, no content
        raise
