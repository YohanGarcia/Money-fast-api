import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings

logger = logging.getLogger(__name__)


def send_reset_code_email(to_email: str, code: str) -> None:
    """Send password reset code via SMTP. Logs code to console if SMTP is not configured."""
    if not settings.smtp_host or not settings.smtp_user or not settings.smtp_password:
        logger.warning(
            "SMTP not configured. Password reset code for %s: %s",
            to_email,
            code,
        )
        return

    html = f"""
    <html>
      <body style="font-family: Arial, sans-serif; background: #f4f4f4; padding: 32px;">
        <div style="max-width: 480px; margin: 0 auto; background: white; border-radius: 8px; padding: 32px;">
          <h2 style="color: #2a7a2a; text-align: center;">MoneyFast</h2>
          <p style="font-size: 16px; color: #333;">¡Hola!</p>
          <p style="font-size: 15px; color: #333;">
            Recibimos una solicitud para restablecer tu contraseña.
            Usa el siguiente código de verificación:
          </p>
          <div style="text-align: center; margin: 28px 0;">
            <span style="font-size: 36px; font-weight: bold; letter-spacing: 8px; color: #2a7a2a;">
              {code}
            </span>
          </div>
          <p style="font-size: 14px; color: #666;">
            Este código expira en <strong>15 minutos</strong>.
          </p>
          <p style="font-size: 13px; color: #999; margin-top: 24px;">
            Si no solicitaste este cambio, ignora este correo.
            Tu contraseña actual permanece segura.
          </p>
          <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;" />
          <p style="font-size: 12px; color: #bbb; text-align: center;">
            MoneyFast &mdash; contacto@moneyfast.com
          </p>
        </div>
      </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Código de recuperación de contraseña — MoneyFast"
    msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_from_email}>"
    msg["To"] = to_email
    msg.attach(MIMEText(html, "html"))

    try:
        context = ssl.create_default_context()
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

        logger.info("Password reset email sent to %s", to_email)

    except smtplib.SMTPException as exc:
        logger.error("Failed to send reset email to %s: %s", to_email, exc)
        raise
