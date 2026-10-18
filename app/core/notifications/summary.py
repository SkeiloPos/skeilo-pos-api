"""Texto ya formateado para mostrar al staff, compartido por los canales que
lo necesitan (`in_app`, `browser_push`) — mantiene el frontend/push desacoplado
de la forma del `payload` de cada `event_type` (research.md §4)."""
from __future__ import annotations

from decimal import Decimal

from app.models.notification_event import NotificationEvent


def _formato_pesos_espacio(valor) -> str:
    """`$ 22.000`: punto de miles, espacio tras el "$", sin decimales — mismo
    criterio que `formatMoney()` del frontend (spec 035). Distinto del
    `format_pesos()` de `cash/denominations.py` (sin espacio, con decimales),
    que sirve a otra pantalla y no se toca (spec 125, research.md R2)."""
    entero = int(Decimal(str(valor)))
    return "$ " + f"{entero:,}".replace(",", ".")


def summarize(event: NotificationEvent) -> str:
    payload = event.payload or {}
    if event.event_type == "order.created":
        mesa = payload.get("table_number")
        items = payload.get("items_count")
        total = payload.get("total")
        partes = []
        if mesa is not None:
            partes.append(f"Mesa {mesa}")
        if items is not None:
            partes.append(f"{items} ítem{'s' if items != 1 else ''}")
        if total is not None:
            partes.append(f"${total}")
        return " · ".join(partes) if partes else "Pedido nuevo"
    if event.event_type == "payment.completed":
        total = payload.get("total")
        return (
            f"Pago recibido · {_formato_pesos_espacio(total)}"
            if total is not None else "Pago recibido"
        )
    return event.event_type
