"""Quién se conecta al stream, y a qué canales tiene derecho.

`EventSource` **no puede enviar cabeceras**, así que ni el `x-session-token` del
comensal ni el `Authorization: Bearer` del staff pueden viajar como siempre. Cada
uno se resuelve de forma distinta:

- **comensal**: el token de sesión va en la query. Es aceptable —su alcance es una
  mesa durante unas horas y ya existe el precedente de `?s=`— y sigue siendo el
  mismo JWT firmado que protege `GET /cart/orders`;

- **staff**: el Bearer **no** va en la query, porque acabaría en los logs de
  nginx, en el historial y en el `Referer`. En su lugar se emite un ticket de un
  solo uso con 30 s de vida, que se consume con `GETDEL`.

En ambos casos **los canales se derivan de la credencial, nunca se piden**: es
imposible suscribirse a la mesa de otro o a otro tenant.
"""
from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from uuid import UUID

import anyio
from fastapi import HTTPException, status

from app.core.config import settings
from app.core.events import CH_STAFF, menu_channel, session_channel
from app.core.qr_context import open_session_context
from app.core.redis import token_blocklist as redis

logger = logging.getLogger(__name__)

_TICKET_PREFIX = "rt:ticket:"


@dataclass(frozen=True)
class Subscriber:
    tenant_id: int
    channels: frozenset[str]
    kind: str  # diner | staff
    #: Solo para comensales: acota el rate limit y el tope de conexiones.
    table_session_id: UUID | None = None


def _validate_diner_sync(token: str) -> Subscriber:
    """Valida el token del comensal contra la DB. **Bloqueante**: va a un hilo.

    `touch=False` a propósito: si la conexión refrescara la ventana deslizante,
    una pestaña olvidada en un bolsillo mantendría la mesa ocupada para siempre.
    El TTL lo siguen moviendo las llamadas REST reales (pedir, cancelar).

    La sesión de DB se cierra al salir del `with`. Mantenerla abierta durante la
    vida del stream anclaría una conexión de Postgres durante horas.
    """
    with open_session_context(token, touch=False) as ctx:
        return Subscriber(
            tenant_id=ctx.tenant.id,
            # spec 128 (US3, FR-009): además de su propia mesa, todo
            # comensal recibe el canal de menú del tenant -- un cambio de
            # visibilidad en QR no está atado a ninguna mesa en particular.
            channels=frozenset({
                session_channel(ctx.table_session.id), menu_channel(ctx.tenant.id),
            }),
            kind="diner",
            table_session_id=ctx.table_session.id,
        )


async def resolve_diner(token: str) -> Subscriber:
    """La DB del proyecto es síncrona, así que la validación va al threadpool.

    Mismo precedente que el barrido de sesiones (`scheduler.py`), que despacha
    `sweep_orphan_sessions` con `anyio.to_thread.run_sync` para no bloquear el
    event loop.
    """
    return await anyio.to_thread.run_sync(_validate_diner_sync, token)


async def issue_ticket(tenant_id: int, user_id) -> str:
    """Emite el ticket de un solo uso del staff."""
    raw = secrets.token_urlsafe(24)
    await redis.set(
        f"{_TICKET_PREFIX}{raw}",
        json.dumps({"t": tenant_id, "u": str(user_id)}),
        ex=settings.REALTIME_TICKET_TTL_SECONDS,
    )
    return raw


async def resolve_staff(ticket: str) -> Subscriber:
    """Consume el ticket. `GETDEL` es atómico: no se puede usar dos veces."""
    try:
        raw = await redis.getdel(f"{_TICKET_PREFIX}{ticket}")
    except Exception:
        logger.warning("Redis no responde al consumir el ticket", exc_info=True)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Tiempo real no disponible"
        )

    if raw is None:
        # Caducado, ya usado o inventado: no se distingue a propósito.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Ticket inválido o ya usado")

    data = json.loads(raw if isinstance(raw, str) else raw.decode())
    return Subscriber(
        tenant_id=int(data["t"]),
        channels=frozenset({CH_STAFF}),
        kind="staff",
    )


async def resolve_subscriber(token: str | None, ticket: str | None) -> Subscriber:
    """Una credencial y solo una."""
    if (token is None) == (ticket is None):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Indica `token` (comensal) o `ticket` (staff), pero no ambos",
        )
    if token is not None:
        return await resolve_diner(token)
    return await resolve_staff(ticket)
