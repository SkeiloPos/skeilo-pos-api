"""Contexto público del comensal a partir de un token de QR firmado.

Reemplaza la resolución por header x-tenant-host en el flujo público: el token
firmado lleva tenant + mesa, así que resolvemos el schema del tenant y abrimos
una sesión ya scopeada, sin exponer el table_id plano ni confiar en headers.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterator
from uuid import UUID

from fastapi import Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import resolve_tenant_by_id, with_db
from app.core.models import Tenant
from app.core.timezone import utc_now_naive
from app.core.qr_token import (
    QrTokenError,
    SessionExpiredError,
    SessionInvalidError,
    verify_qr_token,
    verify_session_token,
)


@dataclass
class QrContext:
    db: Session
    tenant: Tenant
    table_id: UUID


@contextmanager
def open_qr_context(token: str) -> Iterator[QrContext]:
    """Verifica el token de QR, resuelve el tenant y abre una sesión scopeada a
    su schema. Lanza 401 si el token es inválido/manipulado."""
    try:
        claims = verify_qr_token(token)
    except QrTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token de QR inválido"
        )
    tenant = resolve_tenant_by_id(claims.tenant_id)
    with with_db(tenant.schema) as db:
        yield QrContext(db=db, tenant=tenant, table_id=claims.table_id)


def get_qr_context(x_qr_token: str = Header(..., alias="x-qr-token")) -> Iterator[QrContext]:
    """Dependencia FastAPI: lee el token del header `x-qr-token` y entrega el
    contexto del comensal (db tenant-scoped + mesa)."""
    with open_qr_context(x_qr_token) as ctx:
        yield ctx


# --------------------------------------------------------------- Sesión (carrito)

#: Cabecera de los 401 cuya causa es que la mesa/sesión SE CERRÓ (spec 089, A-95): valor `closed`.
#: Ausente en los 401 por vencimiento (inactividad, duración máxima, firma). `main.py` la expone por CORS.
SESSION_STATE_HEADER = "X-Session-State"


@dataclass
class SessionContext:
    db: Session
    tenant: Tenant
    table_id: UUID
    participant: "SessionParticipant"  # noqa: F821
    table_session: "TableSession"  # noqa: F821


def close_participant(db: Session, participant) -> None:
    """Cierra a un comensal y abandona su carrito abierto. **No hace commit**: se
    une a la transacción del caller.

    Sin impacto de inventario: el descuento solo ocurre al confirmar el pedido,
    así que un carrito abandonado no devuelve nada al stock.

    No cierra la `table_session` — los demás comensales siguen en la mesa. De
    liberar la mesa cuando ya no queda nadie se encarga `try_release_if_empty`.
    """
    from app.models.cart import Cart

    participant.status = "closed"
    if participant.closed_at is None:
        participant.closed_at = utc_now_naive()
    for cart in db.execute(
        select(Cart).where(
            Cart.participant_id == participant.id, Cart.status == "abierto"
        )
    ).scalars():
        cart.status = "abandonado"


def _abandon_expired(db: Session, participant, tenant=None) -> None:
    """Cierra al comensal cuyo token expiró y libera la mesa si era el último y
    no quedó ningún pedido que cobrar.

    spec 089 (A-95): si la mesa se liberó, avisa a los comensales conectados DESPUÉS del commit
    (`session.closed`, `reason="empty"`)."""
    from app.api.v1.table_sessions.service import notify_sessions_closed, try_release_if_empty

    close_participant(db, participant)
    closed = try_release_if_empty(db, participant.table_session_id)
    db.commit()
    notify_sessions_closed(getattr(tenant, "id", None), closed, reason="empty")


def _should_refresh(participant, now: datetime) -> bool:
    """¿Toca reescribir `expires_at`, o la ventana sigue lo bastante fresca?

    Sin esta comprobación **cada lectura** del comensal era un UPDATE + COMMIT en
    Postgres. Con el menú QR sondeando cada 10 s eso son ~360 escrituras/h por
    comensal que no aportan nada al negocio; con la holgura, ~6.

    El umbral **no** es "la mitad de la ventana": el barrido de sesiones huérfanas
    recupera la última actividad revirtiendo el desplazamiento
    (`expires_at - SESSION_TTL_MINUTES`, `scheduler.py:64`), así que el desfase que
    permitamos aquí es también el error máximo de esa derivación. Con una holgura
    mayor que `EMPTY_SESSION_TTL_MINUTES` el barrido cerraría mesas **activas** sin
    pedidos, creyéndolas abandonadas.
    """
    if participant.expires_at is None:
        return True
    ventana = timedelta(minutes=settings.SESSION_TTL_MINUTES)
    holgura = timedelta(minutes=settings.SESSION_TTL_REFRESH_SLACK_MINUTES)
    return (participant.expires_at - now) <= ventana - holgura


@contextmanager
def open_session_context(token: str, *, touch: bool = True) -> Iterator[SessionContext]:
    """Autentica al comensal por su token y devuelve el contexto de reingreso.

    `touch=False` valida igual pero **no corre la ventana deslizante**. Lo usa el
    handshake del stream SSE: si una conexión abierta refrescara el TTL, una
    pestaña olvidada mantendría la mesa viva indefinidamente. El TTL lo siguen
    moviendo las llamadas REST reales (pedir, cancelar).

    Cadena de validación (todo antes de exponer datos):

    1. firma + ``exp`` del token;
    2. el participante existe y sigue `open`;
    3. **la sesión de mesa del token es la que sigue `active` en esa mesa** — si la
       mesa se cerró y se abrió otra, el token viejo no sirve para entrar a la
       nueva (regla de reingreso: nunca reusar un `table_session_id` inválido);
    4. TTL deslizante del comensal (4 h, se corre en cada actividad);
    5. tope absoluto contra `TableSession.opened_at` (24 h) como red de seguridad,
       independiente de que el token se siga refrescando.
    """
    from app.models.session_participant import SessionParticipant
    from app.models.table_session import TableSession

    try:
        claims = verify_session_token(token)
    except SessionExpiredError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesión expirada. Vuelve a escanear el QR.",
        )
    except SessionInvalidError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token de sesión inválido"
        )

    tenant = resolve_tenant_by_id(claims.tenant_id)
    with with_db(tenant.schema) as db:
        participant = db.get(SessionParticipant, claims.participant_id)
        if participant is None or participant.status != "open":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesión no activa",
                # spec 089 (A-95, D10): el cliente distingue "cerrada" de "vencida" por esta
                # cabecera, sin tocar el cuerpo ni el mensaje (los tests y el frontend actual
                # dependen de esos textos).
                headers={SESSION_STATE_HEADER: "closed"},
            )

        table_session = db.get(TableSession, claims.table_session_id)
        if (
            table_session is None
            or table_session.status != "active"
            or participant.table_session_id != table_session.id
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="La mesa ya no tiene esta sesión abierta. Vuelve a escanear el QR.",
                headers={SESSION_STATE_HEADER: "closed"},
            )

        now = utc_now_naive()

        if participant.expires_at is not None and participant.expires_at <= now:
            _abandon_expired(db, participant, tenant)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Sesión expirada por inactividad. Vuelve a escanear el QR.",
            )

        max_age = timedelta(minutes=settings.SESSION_ABS_MAX_MINUTES)
        if table_session.opened_at + max_age <= now:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="La sesión de la mesa superó su duración máxima. Vuelve a escanear el QR.",
            )

        # Refresco deslizante: la actividad corre la ventana del comensal, pero
        # solo cuando de verdad hace falta (ver `_should_refresh`).
        if touch and _should_refresh(participant, now):
            participant.expires_at = now + timedelta(minutes=settings.SESSION_TTL_MINUTES)
            db.commit()

        yield SessionContext(
            db=db,
            tenant=tenant,
            table_id=claims.table_id,
            participant=participant,
            table_session=table_session,
        )


def get_session_context(
    x_session_token: str = Header(..., alias="x-session-token"),
    req: Request = None,
) -> Iterator[SessionContext]:
    """Dependencia FastAPI para operaciones de carrito: autentica al comensal por
    el token de sesión y entrega db tenant-scoped + sesión (deslizada).

    `req` (spec 074, extensión de logging operativo) lo inyecta FastAPI solo por
    su anotación `Request`; su default `None` mantiene invocable esta función
    fuera de FastAPI. Solo se usa para el efecto colateral de abajo — lo que
    esta dependencia entrega a quien la usa es exactamente el mismo
    `SessionContext` de siempre."""
    with open_session_context(x_session_token) as ctx:
        # spec 074 (research.md § 8): el comensal ya está autenticado aquí;
        # `OperationalLogMiddleware` lee esto de `request.state` al terminar la
        # petición en vez de volver a validar el token de sesión.
        if req is not None:
            req.state.actor_id = str(ctx.participant.id)
            req.state.actor_type = "comensal"
        yield ctx
