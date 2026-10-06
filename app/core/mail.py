import logging

import httpx

from .config import settings

logger = logging.getLogger(__name__)

EMAIL_SEND_PATH = "/api/email/send"

def create_message(recipients: list[str], subject: str, body: str, text_body: str | None = None):
    message = {
        "from": f"{settings.MAIL_FROM_NAME} <{settings.MAIL_FROM}>",
        "to": recipients,
        "subject": subject,
        "htmlBody": body,
    }
    # FR-047: la clave "textBody" se añade solo cuando hay texto plano, de
    # modo que sin él el payload queda byte por byte igual al actual
    # (research.md Decisión 5).
    if text_body is not None:
        message["textBody"] = text_body
    return message

def send_email(message: dict):
    url = f"{settings.EMAIL_API_URL.rstrip('/')}{EMAIL_SEND_PATH}"
    logger.info("Enviando email a %s con asunto '%s'", message["to"], message["subject"])
    try:
        response = httpx.post(url, json=message, timeout=10.0)
        response.raise_for_status()
        return response
    except httpx.HTTPStatusError as exc:
        # La API respondió con un status de error: expone su cuerpo.
        detail = exc.response.text
        logger.error(
            "Email API respondió %s: %s", exc.response.status_code, detail
        )
        raise RuntimeError(
            f"Error del servicio de email ({exc.response.status_code}): {detail}"
        ) from exc
    except httpx.RequestError as exc:
        # No hubo respuesta (timeout, conexión, DNS…).
        logger.error("No se pudo contactar el servicio de email: %s", exc)
        raise RuntimeError(f"No se pudo contactar el servicio de email: {exc}") from exc


def password_reset_email_body(reset_url: str, expiry_minutes: int) -> str:
    return f"""\
<div style="font-family: Arial, sans-serif; max-width: 560px; margin: 0 auto; color: #1f2937;">
  <h2 style="color: #111827;">Restablecer tu contraseña</h2>
  <p>Recibimos una solicitud para restablecer la contraseña de tu cuenta. Haz clic en el
    siguiente enlace para definir una nueva:</p>
  <p style="margin: 24px 0;">
    <a href="{reset_url}"
       style="background: #4f46e5; color: #ffffff; padding: 12px 20px; border-radius: 8px;
              text-decoration: none; font-weight: bold;">
      Restablecer contraseña
    </a>
  </p>
  <p style="color: #6b7280; font-size: 14px;">
    Este enlace es válido por {expiry_minutes} minutos y solo puede usarse una vez.
  </p>
  <p style="color: #6b7280; font-size: 14px;">
    Si no solicitaste este cambio, puedes ignorar este correo — tu contraseña actual sigue
    funcionando sin cambios.
  </p>
</div>"""


def password_changed_email_body(when: str, email: str) -> str:
    return f"""\
<div style="font-family: Arial, sans-serif; max-width: 560px; margin: 0 auto; color: #1f2937;">
  <h2 style="color: #111827;">Tu contraseña fue cambiada</h2>
  <p>La contraseña de la cuenta <strong>{email}</strong> se actualizó el {when}.</p>
  <p style="color: #b91c1c; font-size: 14px;">
    Si no fuiste tú quien hizo este cambio, contacta a un administrador de tu negocio de
    inmediato.
  </p>
</div>"""
