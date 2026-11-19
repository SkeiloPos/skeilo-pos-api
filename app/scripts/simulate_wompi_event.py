"""Simulador de eventos de Wompi para probar el pago de la suscripción SIN cuenta de Wompi (spec 161).

Mientras no existan credenciales reales, este script firma un evento `transaction.updated` con la misma fórmula
de checksum que verifica `POST /api/v1/webhooks/wompi` y, opcionalmente, lo envía a un servidor. Sirve para
recorrer a mano el ciclo completo: pedir el link (`POST /plan/subscription/payment-link`), "pagarlo" con este
simulador y comprobar que las fechas del tenant se recalculan y que el tenant se desbloquea.

Uso:

    # Solo imprime el evento firmado (para pegarlo en curl, Postman, etc.):
    python -m app.scripts.simulate_wompi_event --reference SKP-8f0c… --amount-cents 8990000 --secret test_events_local

    # Lo envía al webhook de un servidor local y muestra la respuesta:
    python -m app.scripts.simulate_wompi_event --reference SKP-8f0c… --amount-cents 8990000 \\
        --secret test_events_local --post http://localhost:8000/api/v1/webhooks/wompi

    # Otros resultados: --status DECLINED | ERROR | VOIDED | PENDING ;  --tamper simula una firma alterada (→ 401).

`--reference` y `--amount-cents` salen de la respuesta del link de pago (`gateway_reference` y `amount` × 100).
`--secret` (o la variable de entorno `WOMPI_EVENTS_SECRET`) debe ser el mismo que usa el servidor: con credenciales
de prueba locales, nunca con las de producción.

**Qué NO prueba**: firma los eventos con la MISMA función con que el servidor los verifica, así que confirma el
flujo del sistema pero no que la fórmula coincida con la de Wompi. Eso solo lo confirma un evento real de Sandbox
(tarea pendiente de la spec 161, FR-041). Es una herramienta de desarrollo: no la apuntes a producción.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import uuid
import urllib.request
from typing import Optional

from app.core import wompi

#: Resultados de una transacción que Wompi puede notificar (documentación de Wompi).
STATUSES = ("APPROVED", "DECLINED", "ERROR", "VOIDED", "PENDING")

#: Campos firmados por defecto en un evento de Wompi, en el orden de su documentación.
DEFAULT_PROPERTIES = ["transaction.id", "transaction.status", "transaction.amount_in_cents"]


def build_event(
    reference: str,
    *,
    amount_in_cents: int,
    secret: str,
    status: str = "APPROVED",
    currency: str = wompi.CURRENCY,
    transaction_id: Optional[str] = None,
    timestamp: Optional[int] = None,
    event: str = "transaction.updated",
    properties: Optional[list[str]] = None,
    tamper: bool = False,
) -> dict:
    """Un evento `transaction.updated` firmado con `secret`.

    El checksum va en MAYÚSCULAS, como lo muestra la documentación de Wompi, para ejercitar la comparación sin
    distinguir mayúsculas del servidor. Con `tamper=True` el checksum se altera (firma inválida)."""
    ts = int(time.time()) if timestamp is None else timestamp
    ev = {
        "event": event,
        "data": {
            "transaction": {
                # Único por evento aunque salgan en el mismo segundo: Wompi nunca repite el id de una transacción, y el sistema
                # rechaza (con razón) que una misma transacción se ligue a dos pagos.
                "id": transaction_id or f"TX-SIM-{uuid.uuid4().hex[:12]}",
                "status": status.upper(),
                "amount_in_cents": amount_in_cents,
                "reference": reference,
                "currency": currency,
            }
        },
        "environment": "test",
        # `compute_event_checksum` valida la estructura y exige un checksum no vacío: se calcula con un marcador y se reemplaza.
        "signature": {"properties": list(properties or DEFAULT_PROPERTIES), "checksum": "pendiente"},
        "timestamp": ts,
        "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts)),
    }
    checksum = wompi.compute_event_checksum(ev, secret).upper()
    ev["signature"]["checksum"] = ("0" * 64) if tamper else checksum
    return ev


def post_event(url: str, event: dict, *, timeout: float = 10.0) -> tuple[int, str]:
    """Envía el evento como lo haría Wompi (cuerpo JSON y cabecera `X-Event-Checksum`). Devuelve (estado, cuerpo)."""
    request = urllib.request.Request(
        url,
        data=json.dumps(event).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "X-Event-Checksum": event["signature"]["checksum"]},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:  # 4xx/5xx también son una respuesta que queremos mostrar
        return exc.code, exc.read().decode()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Firma (y opcionalmente envía) un evento simulado de Wompi. Solo desarrollo.")
    parser.add_argument("--reference", required=True, help="`gateway_reference` del pago (SKP-…).")
    parser.add_argument("--amount-cents", required=True, type=int, help="Monto en centavos (`amount` × 100).")
    parser.add_argument("--status", default="APPROVED", type=str.upper, choices=STATUSES)
    parser.add_argument("--currency", default=wompi.CURRENCY)
    parser.add_argument("--tx-id", default=None, help="Id de transacción de Wompi (por defecto, uno nuevo).")
    parser.add_argument("--secret", default=os.environ.get("WOMPI_EVENTS_SECRET"), help="Secreto de eventos (o env WOMPI_EVENTS_SECRET).")
    parser.add_argument("--post", metavar="URL", default=None, help="Envía el evento a esta URL en vez de solo imprimirlo.")
    parser.add_argument("--tamper", action="store_true", help="Altera el checksum para simular una firma inválida.")
    args = parser.parse_args(argv)

    if not args.secret:
        print("ERROR: falta --secret (o la variable de entorno WOMPI_EVENTS_SECRET).")
        return 2

    event = build_event(
        args.reference, amount_in_cents=args.amount_cents, secret=args.secret, status=args.status,
        currency=args.currency, transaction_id=args.tx_id, tamper=args.tamper,
    )
    if args.post is None:
        print(json.dumps(event, indent=2, ensure_ascii=False))
        return 0

    status, body = post_event(args.post, event)
    print(f"POST {args.post} -> {status}\n{body}")
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
