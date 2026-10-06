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
