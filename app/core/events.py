"""Catálogo de eventos de negocio y su publicación en Redis Streams.

**Un stream por tenant** (`events:tenant:{id}`), con el filtrado por canal hecho
en memoria dentro de cada proceso. Un stream por *mesa* fragmentaría demasiado
(el `XREAD` iría sobre un conjunto de claves cambiante); por tenant el volumen es
bajísimo —del orden de 50 eventos/s en el peor caso— y filtrar en memoria es
gratis.

Streams y no Pub/Sub porque los ids son **persistentes y ordenados**: el id de la
entrada *es* el id del evento SSE, así que el `Last-Event-ID` que el navegador
reenvía solo se traduce en un `XRANGE` exacto y el replay sale gratis. Con
Pub/Sub habría que construir ese buffer aparte.

Eventos **delgados**: llevan ids y lo que cambió, no el objeto completo. Así el
bus se mantiene pequeño, no se filtra información entre canales, y el cliente
decide si le basta el evento o necesita re-consultar por REST. Postgres sigue
siendo la fuente de verdad; esto es una notificación, no el dato.

**Publicar siempre después del COMMIT.** Si la transacción falla no puede haber
salido un evento anunciando algo que no ocurrió.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence
from uuid import UUID

from redis import Redis as SyncRedis

from app.core.config import settings

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------- Claves

def stream_key(tenant_id: int) -> str:
    return f"events:tenant:{tenant_id}"


#: Canal del personal: la terminal ve todo el tenant.
CH_STAFF = "staff"


def session_channel(table_session_id: UUID | str) -> str:
    """Canal de una sesión de mesa: solo sus comensales.

    La suscripción **se deriva del token, nunca se pide**: el `table_session_id`
    viene firmado en el JWT del comensal, así que es imposible escuchar otra mesa.
    """
    return f"session:{table_session_id}"


# ------------------------------------------------------------------ Publicador

_publisher: SyncRedis | None = None


def _client() -> SyncRedis:
    """Cliente **síncrono** dedicado a publicar.

    Deliberadamente no es el `redis.asyncio` de `app.core.redis`. La capa de
    servicio y la mitad de los routers son síncronos (la DB lo es), así que un
    cliente async obligaría a dos caminos de código —o a convertir routers `def`
    en `async def`, que pondría `db.execute()` bloqueante en el event loop—.
    Con el cliente síncrono se publica igual desde un servicio, desde un router
    `def`, desde un router `async def` y desde el hilo del scheduler.

    El coste es ruido: un XADD a Redis local son ~0,1-0,3 ms, y el
    `socket_timeout` acota el peor caso.
    """
    global _publisher
    if _publisher is None:
        _publisher = SyncRedis.from_url(
            settings.REDIS_URL,
            socket_timeout=settings.REALTIME_PUBLISH_TIMEOUT_SECONDS,
            socket_connect_timeout=settings.REALTIME_PUBLISH_TIMEOUT_SECONDS,
            max_connections=10,
            health_check_interval=30,
            decode_responses=True,
        )
    return _publisher


def _jsonable(value: Any) -> Any:
    """UUID, Decimal y datetime a algo que `json.dumps` acepte.

    Los importes van como **string**, no como float: redondear dinero en coma
    flotante al serializar es exactamente el bug que `Decimal` evita en la DB.
    """
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def publish(
    tenant_id: int,
    *,
    type: str,
    channels: Sequence[str],
    payload: Mapping[str, Any] | None = None,
) -> str | None:
    """Añade el evento al stream del tenant. Devuelve el id de la entrada.

    **Nunca lanza.** Un evento perdido degrada la UI al sondeo de respaldo, que
    para eso se mantiene; una excepción aquí tumbaría una operación de negocio
    que ya está comprometida en Postgres. Misma filosofía *fail-open* que
    `rate_limit.enforce()`.
    """
    if not settings.REALTIME_ENABLED:
        return None
    if not channels:
        return None

    fields = {
        "type": type,
        "ch": ",".join(channels),
        "at": datetime.now(timezone.utc).isoformat(),
        "d": json.dumps(_jsonable(dict(payload or {})), separators=(",", ":")),
    }
    try:
        return _client().xadd(
            stream_key(tenant_id),
            fields,
            maxlen=settings.REALTIME_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception:
        logger.warning(
            "No se pudo publicar %s del tenant %s; los clientes caerán al sondeo",
            type, tenant_id, exc_info=True,
        )
        return None


def version_of(entry_id: str) -> int:
    """Versión monótona por tenant, derivada del id de la entrada de Redis.

    Redis es el **único punto de serialización** del tenant, así que sus ids son
    estrictamente crecientes entre procesos y sin skew de reloj. Eso lo hace
    mejor `v` que cualquier contador por entidad: `CustomerOrder.version` solo se
    incrementa al bloquear y confirmar, nunca en las transiciones de cocina, que
    son justo las que hay que ordenar.

    `ms*1000 + seq` ≈ 1,75e15 hoy, muy por debajo de `Number.MAX_SAFE_INTEGER`
    (9,007e15), así que el cliente JS lo maneja sin perder precisión. El `seq` se
    satura a 999: haría falta un tenant emitiendo >1000 eventos en el mismo
    milisegundo para llegar ahí.
    """
    ms, _, seq = entry_id.partition("-")
    return int(ms) * 1000 + min(int(seq or 0), 999)


# ------------------------------------------------------- Catálogo (§8 del doc)
#
# Un wrapper por evento para que los puntos de llamada sean una línea y no haya
# literales de tipo sueltos por el código.

def order_created(
    tenant_id: int, *, order_id, table_session_id, dining_table_id,
    table_number, customer_name, items_count, total,
) -> str | None:
    """El comensal envió su carrito. El pedido queda en `recibida`."""
    return publish(
        tenant_id,
        type="order.created",
        channels=[CH_STAFF, session_channel(table_session_id)],
        payload={
            "order_id": order_id, "table_session_id": table_session_id,
            "dining_table_id": dining_table_id, "table_number": table_number,
            "customer_name": customer_name, "items_count": items_count, "total": total,
        },
    )


def order_confirmed(tenant_id: int, *, order_id, table_session_id) -> str | None:
    """El staff aceptó el pedido: 'recibida' → 'abierta' (descuenta inventario)."""
    return publish(
        tenant_id,
        type="order.confirmed",
        channels=[CH_STAFF, session_channel(table_session_id)],
        payload={"order_id": order_id, "table_session_id": table_session_id, "status": "abierta"},
    )


def item_kitchen_changed(
    tenant_id: int, *, order_id, table_session_id, item_id, estado_cocina,
) -> str | None:
    """El evento más frecuente: cocina movió un ítem de columna."""
    channels = [CH_STAFF]
    if table_session_id is not None:
        channels.append(session_channel(table_session_id))
    return publish(
        tenant_id,
        type="order.item_kitchen_changed",
        channels=channels,
        payload={
            "order_id": order_id, "table_session_id": table_session_id,
            "item_id": item_id, "estado_cocina": estado_cocina,
        },
    )


def item_addon_added(
    tenant_id: int, *, order_id, table_session_id, item_id, option_name, quantity,
) -> str | None:
    """Una adición se agregó a una línea ya persistida (spec 123) -- distinto
    de `item_kitchen_changed`: no cambia `estado_cocina`, así que no se
    reutiliza ese tipo."""
    channels = [CH_STAFF]
    if table_session_id is not None:
        channels.append(session_channel(table_session_id))
    return publish(
        tenant_id,
        type="order.item_addon_added",
        channels=channels,
        payload={
            "order_id": order_id, "table_session_id": table_session_id,
            "item_id": item_id, "option_name": option_name, "quantity": quantity,
        },
    )


def item_voided(
    tenant_id: int, *, order_id, table_session_id, item_id, motivo,
    replacement_item_id=None,
) -> str | None:
    channels = [CH_STAFF]
    if table_session_id is not None:
        channels.append(session_channel(table_session_id))
    return publish(
        tenant_id,
        type="order.item_voided",
        channels=channels,
        payload={
            "order_id": order_id, "table_session_id": table_session_id,
            "item_id": item_id, "motivo": motivo,
            "replacement_item_id": replacement_item_id,
        },
    )


def order_cancelled(tenant_id: int, *, order_id, table_session_id, motivo) -> str | None:
    channels = [CH_STAFF]
    if table_session_id is not None:
        channels.append(session_channel(table_session_id))
    return publish(
        tenant_id,
        type="order.cancelled",
        channels=channels,
        payload={
            "order_id": order_id, "table_session_id": table_session_id, "motivo": motivo,
        },
    )


def bill_changed(tenant_id: int, *, table_session_id) -> str | None:
    """La cuenta de la mesa dejó de estar al día.

    **Sin `total` a propósito.** Calcularlo obliga a un `compute_bill()` extra por
    evento, y el cliente no puede usarlo para nada más: recargar la cuenta sola le
    borraría al cajero el efectivo que está tecleando (`SessionBillPanelComponent`
    resetea el pago cuando cambia el objeto `bill`). El evento solo marca la
    cuenta como obsoleta; el importe real lo trae el REST al pulsar "actualizar".
    """
    if table_session_id is None:
        return None
    return publish(
        tenant_id,
        type="session.bill_changed",
        channels=[CH_STAFF, session_channel(table_session_id)],
        payload={"table_session_id": table_session_id},
    )


def payment_completed(
    tenant_id: int, *, sale_id, table_session_id, total, customer_name,
    billing_mode, invoice=None,
) -> str | None:
    """Una venta emitida. En cobro dividido llega una por comensal.

    Spec 077 (RF-005): además del canal `staff` de siempre, se agrega
    `session_channel(table_session_id)` cuando hay mesa asociada — el
    comensal anónimo con el menú QR abierto ya está suscrito a ese canal
    (`connectDiner`), pero hasta ahora este evento nunca se lo enrutaba.
    Ventas de mostrador sin mesa (`table_session_id is None`) siguen sin
    llegar a ningún comensal — no aplica, no hay comensal QR involucrado.
    """
    channels = [CH_STAFF]
    if table_session_id is not None:
        channels.append(session_channel(table_session_id))
    return publish(
        tenant_id,
        type="payment.completed",
        channels=channels,
        payload={
            "sale_id": sale_id, "table_session_id": table_session_id, "total": total,
            "customer_name": customer_name, "billing_mode": billing_mode,
            "invoice": invoice,
        },
    )


def session_closed(
    tenant_id: int, *, table_session_id, dining_table_id, reason: str,
) -> str | None:
    """La mesa se cerró. `reason`: paid | swept | released | empty.

    `empty` (spec 089, A-95): cierre automático de una sesión sin órdenes (el último comensal salió,
    su token venció o canceló su último pedido). El cliente ignora el valor: cualquier `reason`
    produce la misma pantalla de "¡Gracias por tu visita!"."""
    return publish(
        tenant_id,
        type="session.closed",
        channels=[CH_STAFF, session_channel(table_session_id)],
        payload={
            "table_session_id": table_session_id,
            "dining_table_id": dining_table_id,
            "reason": reason,
        },
    )


def table_status_changed(
    tenant_id: int, *, dining_table_id, table_number, status: str,
) -> str | None:
    """Pinta el tablero del cajero."""
    return publish(
        tenant_id,
        type="table.status_changed",
        channels=[CH_STAFF],
        payload={
            "dining_table_id": dining_table_id,
            "table_number": table_number,
            "status": status,
        },
    )
