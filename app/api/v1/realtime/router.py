"""Stream SSE: el reemplazo del sondeo.

**SSE y no WebSockets** porque el flujo es unidireccional servidor→cliente: el
comensal nunca envía nada por este canal, sus pedidos van por POST REST. A cambio
se obtiene gratis lo que con WebSockets habría que escribir a mano —reconexión
automática, backoff y replay vía `Last-Event-ID`— sobre HTTP/1.1 plano, sin
upgrade de protocolo. El día que aparezca tráfico cliente→servidor con estado
(chat con el mesero, comandas por voz) se migra: el diseño de canales y eventos
no cambia, solo el transporte.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.api.v1.realtime.auth import Subscriber, issue_ticket, resolve_subscriber
from app.api.v1.realtime.schemas import TicketResponse
from app.core.config import settings
from app.core.db import get_tenant
from app.core.dependencies import get_current_user
from app.core.event_bus import Event, Subscription, event_bus, id_le
from app.core.models import Tenant, User
from app.core.rate_limit import enforce as rate_limit
from app.core.redis import token_blocklist as redis

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/realtime", tags=["realtime"])

_CONN_PREFIX = "rt:conn:"


# ------------------------------------------------------------------ formato SSE

def _frame(event: str, data: dict, entry_id: str | None = None) -> bytes:
    """Un frame SSE. `json.dumps` escapa los saltos de línea, así que el payload
    no puede romper el formato."""
    out = ""
    if entry_id is not None:
        out += f"id: {entry_id}\n"
    out += f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
    return out.encode()


def _event_frame(ev: Event) -> bytes:
    """El `id:` lleva el id **crudo** de Redis, que es lo que el navegador
    reenviará como `Last-Event-ID` y se traduce en un `XRANGE` exacto. El `v` del
    payload es el mismo id como entero, para ordenar en el cliente."""
    return _frame(ev.type, {"type": ev.type, "v": ev.v, "at": ev.at, **ev.data}, ev.id)


# --------------------------------------------------- tope de conexiones por mesa

async def _acquire_slot(sub: Subscriber) -> bool:
    """Reserva una conexión para la sesión de mesa. Una mesa no tiene 50 comensales.

    El `EXPIRE` es la auto-curación: si un proceso muere sin liberar su hueco, el
    contador caduca solo en vez de dejar la mesa bloqueada. Fail-open si Redis no
    responde, como el resto del rate limiting.
    """
    if sub.table_session_id is None:
        return True
    key = f"{_CONN_PREFIX}{sub.table_session_id}"
    try:
        n = await redis.incr(key)
        if n == 1:
            await redis.expire(key, settings.REALTIME_MAX_CONNECTION_SECONDS + 300)
        if n > settings.REALTIME_MAX_CONN_PER_SESSION:
            await redis.decr(key)
            return False
        return True
    except Exception:
        logger.warning("No se pudo contar conexiones; se deja pasar", exc_info=True)
        return True


async def _release_slot(sub: Subscriber) -> None:
    if sub.table_session_id is None:
        return
    try:
        await redis.decr(f"{_CONN_PREFIX}{sub.table_session_id}")
    except Exception:
        logger.warning("No se pudo liberar el hueco de conexión", exc_info=True)


# ----------------------------------------------------------------- el generador

async def _stream(
    request: Request, sub: Subscriber, subscription: Subscription, last_event_id: str | None,
):
    deadline = time.monotonic() + settings.REALTIME_MAX_CONNECTION_SECONDS
    max_sent: str | None = None
    try:
        # 1. El `retry:` fija el backoff del navegador antes que nada.
        yield f"retry: {settings.REALTIME_RETRY_MS}\n\n".encode()

        # 2. Replay de lo perdido. La suscripción ya está activa (se hizo antes de
        #    entrar aquí), así que nada se cuela por la rendija entre ambos.
        if last_event_id:
            perdidos, hueco = await event_bus.history(
                sub.tenant_id, last_event_id, limit=settings.REALTIME_REPLAY_MAX
            )
            if hueco:
                # Estuvo fuera más que la retención del stream. Un `resync`
                # explícito es más honesto que un hueco silencioso.
                yield _frame("resync", {"type": "resync", "reason": "gap"})
            else:
                for ev in perdidos:
                    if subscription.wants(ev):
                        yield _event_frame(ev)
                        max_sent = ev.id

        # 3. Handshake: el cliente ya sabe que está dentro y a qué escucha.
        yield _frame("hello", {
            "type": "hello",
            "channels": sorted(subscription.channels),
            "heartbeat": settings.REALTIME_HEARTBEAT_SECONDS,
        })

        # 4. En vivo.
        while True:
            if subscription.dropped:
                # No consumía y se le llenó la cola: que recargue por REST.
                yield _frame("resync", {"type": "resync", "reason": "backpressure"})
                return
            if time.monotonic() >= deadline:
                # Corte de vida máxima: al reconectar revalida el token. Evita
                # sockets inmortales con credenciales viejas.
                return
            if await request.is_disconnected():
                return

            try:
                ev = await asyncio.wait_for(
                    subscription.queue.get(), timeout=settings.REALTIME_HEARTBEAT_SECONDS
                )
            except asyncio.TimeoutError:
                # Comentario SSE: mantiene vivo el túnel a través del proxy y
                # detecta el otro extremo muerto.
                yield b": ping\n\n"
                continue

            # El replay pudo adelantar lo que ya estaba en la cola.
            if max_sent is not None and id_le(ev.id, max_sent):
                continue
            max_sent = ev.id
            yield _event_frame(ev)
    finally:
        await event_bus.unsubscribe(subscription)
        await _release_slot(sub)


# ------------------------------------------------------------------- endpoints

@router.post(
    "/ticket",
    response_model=TicketResponse,
    summary="Ticket de un solo uso para abrir el stream (staff)",
)
async def create_ticket(
    request: Request,
    user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
) -> TicketResponse:
    """El Bearer no puede ir en la query del stream —acabaría en los logs de
    nginx, en el historial y en el `Referer`—, así que se cambia por un ticket
    efímero que solo sirve para abrir la conexión."""
    if not settings.REALTIME_ENABLED:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Tiempo real desactivado")
    await rate_limit(request, "realtime_ticket")
    raw = await issue_ticket(tenant.id, user.id, user.role_name)
    return TicketResponse(ticket=raw, expires_in=settings.REALTIME_TICKET_TTL_SECONDS)


@router.get("/stream", summary="Stream de eventos (SSE)")
async def stream(
    request: Request,
    token: str | None = Query(None, description="Token de sesión del comensal"),
    ticket: str | None = Query(None, description="Ticket de un solo uso del staff"),
    last_event_id_q: str | None = Query(None, alias="last_event_id"),
    last_event_id_h: str | None = Header(None, alias="last-event-id"),
) -> StreamingResponse:
    """Sin `Depends(get_db)` ni `Depends(get_tenant)`, y es deliberado:
    `EventSource` no manda cabeceras, así que aquí no hay `x-tenant-host`. El
    tenant sale siempre del claim firmado del token o del ticket.
    """
    if not settings.REALTIME_ENABLED:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Tiempo real desactivado")

    # Un 401 aquí, antes de abrir el stream, hace que `EventSource` deje de
    # reintentar: es justo lo que queremos para una sesión muerta.
    sub = await resolve_subscriber(token, ticket)

    await rate_limit(request, "realtime_connect", table_id=sub.table_session_id)

    if not await _acquire_slot(sub):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Demasiadas conexiones abiertas para esta mesa",
            headers={"Retry-After": "30"},
        )

    # La cabecera manda (la envía el navegador solo); el query param es el
    # respaldo para la reconexión que gestionamos a mano, donde no se pueden
    # poner cabeceras.
    last_event_id = last_event_id_h or last_event_id_q

    # Suscribir **antes** del generador: si se hiciera dentro, los eventos
    # emitidos entre el replay y la suscripción se perderían sin dejar rastro.
    try:
        subscription = await event_bus.subscribe(sub.tenant_id, sub.channels)
    except Exception:
        await _release_slot(sub)
        raise

    return StreamingResponse(
        _stream(request, sub, subscription, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            # Cinturón por si nginx no tiene `proxy_buffering off`: sin esto el
            # proxy acumula la respuesta y el cliente no recibe nada.
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
