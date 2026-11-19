"""Integración con Wompi (Colombia) para el pago de la suscripción del tenant (spec 161;
research.md Decisiones 1-3; contrato `wompi-webhook.md`).

Solo biblioteca estándar (Principio IX): `hashlib`, `hmac`, `urllib.parse`, `decimal`. **No hay ninguna
llamada de red a Wompi**: el link es una dirección del Web Checkout firmada con el secreto de
integridad, con referencia propia y vencimiento propio (decisión D3 de la spec).

Fuente de las fórmulas: documentación pública de Wompi Colombia (Web Checkout y Eventos), leída el
2026-11-17. La firma de integridad se verificó contra el ejemplo de la propia documentación. La
fórmula del checksum de EVENTOS está implementada tal como la describe el texto, pero su ejemplo
numérico no reproduce (research.md Decisión 1): hay que confirmarla con un evento real de Sandbox
(tarea pendiente de la spec, FR-041).
"""
from __future__ import annotations

import hmac
import logging
import uuid
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any, Optional
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

#: Wompi solo opera en pesos colombianos en el Web Checkout.
CURRENCY = "COP"


def build_reference(payment_id: uuid.UUID) -> str:
    """Referencia propia del cobro: `SKP-<uuid del pago en hex>`, 36 caracteres.

    Determinista respecto al pago (se deriva de su `id`), única, y solo alfanumérica con guion — lo que
    Wompi recomienda. No lleva datos del tenant en claro. Es lo que Wompi devuelve en
    `transaction.reference` y con lo que el webhook localiza el pago."""
    return f"SKP-{payment_id.hex}"


def amount_in_cents(amount: Decimal) -> int:
    """Pesos a centavos, **exacto**: se calcula con `Decimal` (nunca `float`) y se rechaza un monto con
    fracción de centavo en vez de redondearlo en silencio."""
    cents = Decimal(amount) * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"El monto {amount!r} no es un número exacto de centavos")
    return int(cents)


def format_expiration(moment: datetime) -> str:
    """`expiration-time` en el formato de la documentación de Wompi: ISO 8601 en UTC, con milisegundos.
    Se recibe un instante UTC *naive* (como el resto del esquema compartido) y los microsegundos se
    descartan. **Esta misma cadena** debe entrar en la firma y en la URL: si difieren, Wompi la rechaza."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def integrity_signature(
    reference: str, cents: int, currency: str, expiration: Optional[str], secret: str
) -> str:
    """Firma de integridad del Web Checkout: SHA-256 hexadecimal en minúsculas de
    `<referencia><monto en centavos><moneda>[<expiration-time>]<secreto de integridad>`, sin separadores.
    Con `expiration` va entre la moneda y el secreto (orden de la documentación)."""
    return sha256(f"{reference}{cents}{currency}{expiration or ''}{secret}".encode()).hexdigest()


def build_checkout_url(
    *,
    base_url: str,
    public_key: str,
    reference: str,
    amount_in_cents: int,
    currency: str,
    signature: str,
    redirect_url: str,
    expiration: Optional[str],
) -> str:
    """Dirección del Web Checkout con todos los parámetros por query. `urlencode` codifica los dos puntos de
    `signature:integrity` como `%3A`, igual que un formulario HTML `GET`, que es como la documentación lo
    muestra."""
    params = {
        "public-key": public_key,
        "currency": currency,
        "amount-in-cents": amount_in_cents,
        "reference": reference,
        "signature:integrity": signature,
        "redirect-url": redirect_url,
    }
    if expiration:
        params["expiration-time"] = expiration
    return f"{base_url}?{urlencode(params)}"


def gateway_configured(config) -> bool:
    """¿Se puede firmar un link? Hacen falta la llave pública y el secreto de integridad. Una cadena vacía
    cuenta como «sin configurar»: `.env.example` trae `WOMPI_PUBLIC_KEY=` vacío. El secreto de EVENTOS no
    hace falta para generar un link, solo para recibir el webhook."""
    return bool(config.WOMPI_PUBLIC_KEY) and bool(config.WOMPI_INTEGRITY_SECRET)


# ======================================================================
# Eventos (webhook) — spec 161, Historia de Usuario 4 (FR-019; contrato `wompi-webhook.md § 2`)
# ======================================================================

#: Propiedades que el evento DEBE traer firmadas: solo lo firmado está protegido, y de ahí salen el estado y
#: el monto que se aplican. `transaction.reference` no figura entre los firmados por defecto (research.md
#: Decisión 3): el pago se localiza por ella, y la defensa es `gateway_transaction_id` único.
REQUIRED_SIGNED_PROPERTIES = ("transaction.id", "transaction.status", "transaction.amount_in_cents")


class WompiEventError(Exception):
    """Un evento que no se acepta: `400` (estructura inválida) o `401` (firma inválida o sin secreto).
    Wompi reintenta ante cualquier respuesta distinta de 200 (3 veces en 24 h); para un evento forjado o mal
    formado eso es inofensivo. `detail` nunca incluye el secreto ni el checksum esperado."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _signed_value(data: Any, dotted_path: str) -> Any:
    """Resuelve `transaction.status` dentro de `data`. Una ruta que no existe es un evento mal formado."""
    current = data
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise WompiEventError(400, "Evento inválido: falta una propiedad firmada")
        current = current[part]
    return current


def _check_structure(event: Any) -> tuple[list[str], str, Any]:
    if not isinstance(event, dict):
        raise WompiEventError(400, "Evento inválido")
    signature = event.get("signature")
    if not isinstance(signature, dict):
        raise WompiEventError(400, "Evento inválido: falta la firma")
    properties = signature.get("properties")
    if not isinstance(properties, list) or not properties or not all(isinstance(p, str) and p for p in properties):
        raise WompiEventError(400, "Evento inválido: faltan los campos firmados")
    checksum = signature.get("checksum")
    if not isinstance(checksum, str) or not checksum:
        raise WompiEventError(400, "Evento inválido: falta el checksum")
    if not isinstance(event.get("data"), dict):
        raise WompiEventError(400, "Evento inválido: falta data")
    timestamp = event.get("timestamp")
    if timestamp is None or isinstance(timestamp, bool) or not isinstance(timestamp, (int, str)):
        raise WompiEventError(400, "Evento inválido: falta el timestamp")
    return properties, checksum, timestamp


def compute_event_checksum(event: dict, secret: str) -> str:
    """Checksum del evento según la documentación de Wompi: SHA-256 hexadecimal en minúsculas de los valores de
    `signature.properties` tomados de `data` **en el orden recibido** (los `properties` pueden variar: se leen
    del evento y no se suponen fijos), seguidos de `timestamp` y del secreto de eventos, sin separadores."""
    properties, _, timestamp = _check_structure(event)
    values = "".join(str(_signed_value(event["data"], p)) for p in properties)
    return sha256(f"{values}{timestamp}{secret}".encode()).hexdigest()


def _same_checksum(expected: str, received: str) -> bool:
    """Comparación en tiempo constante y sin distinguir mayúsculas (la documentación lo muestra en mayúsculas).
    Se compara en bytes: `hmac.compare_digest` rechaza `str` no ASCII con `TypeError`."""
    return hmac.compare_digest(expected.lower().encode(), received.strip().lower().encode())


def verify_event(event: Any, header_checksum: Optional[str], secret: Optional[str]) -> None:
    """Valida la autenticidad y la estructura de un evento; lanza `WompiEventError` si no se acepta.

    Orden (contrato § 2; la primera comprobación que falla corta):

    1. Sin `WOMPI_EVENTS_SECRET` configurado → `401` (se trata como firma inválida, FR-019) y `logger.error`.
    2. Estructura → `400`.
    3. El checksum del cuerpo no coincide con el calculado → `401`. Si además llega la cabecera
       `X-Event-Checksum`, también debe coincidir.
    4. Los `properties` firmados no incluyen los tres mínimos → `400`.
    """
    if not secret:
        logger.error("WOMPI_EVENTS_SECRET no está configurado: se rechaza el evento de Wompi")
        raise WompiEventError(401, "Firma inválida")
    _, received, _ = _check_structure(event)
    expected = compute_event_checksum(event, secret)
    if not _same_checksum(expected, received):
        raise WompiEventError(401, "Firma inválida")
    if header_checksum and not _same_checksum(expected, header_checksum):
        raise WompiEventError(401, "Firma inválida")
    signed = set(event["signature"]["properties"])
    if not set(REQUIRED_SIGNED_PROPERTIES) <= signed:
        raise WompiEventError(400, "Evento inválido: el estado y el monto deben ir firmados")
