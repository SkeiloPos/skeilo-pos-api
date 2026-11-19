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
