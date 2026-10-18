"""Puntos de entrada del negocio a la capa de notificaciones (spec 077,
RF-002, RF-009, research.md §4, §11).

Solo dos disparadores en el alcance de esta funcionalidad: `order.created` y
`payment.completed` — ningún otro tipo del catálogo de `app/core/events.py`
genera `NotificationEvent` (research.md §11).

Cada función (a) inserta una fila `NotificationEvent` y (b) reparte a los
canales habilitados para `(tenant_id, event_type)` — la **ausencia** de fila
en `NotificationChannelPreference` se interpreta como habilitado (data-model.md
§ Regla de lectura). Los puntos de llamada de negocio no conocen la lista de
canales (SC-007): agregar uno nuevo es una línea en `_CHANNELS`, no un cambio
en `cart/router.py`/`orders/router.py`/`table_sessions/service.py`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.events import _jsonable
from app.core.models import Tenant
from app.core.notifications.channels.base import NotificationChannel
from app.core.notifications.channels.browser_push import BrowserPushChannel
from app.core.notifications.channels.in_app import InAppChannel
from app.models.notification_channel_pref import NotificationChannelPreference
from app.models.notification_event import NotificationEvent

logger = logging.getLogger(__name__)

#: Fábricas de canal, una por `NotificationChannelPreference.channel`. Añadir
#: un canal nuevo (email/SMS/WhatsApp) es una línea aquí.
_CHANNELS: dict[str, type] = {
    "in_app": InAppChannel,
    "push": BrowserPushChannel,
}


def _enabled_channel_names(db: Session, event_type: str) -> set[str]:
    prefs = db.execute(
        select(NotificationChannelPreference).where(
            NotificationChannelPreference.event_type == event_type
        )
    ).scalars().all()
    deshabilitados = {p.channel for p in prefs if not p.enabled}
    return set(_CHANNELS) - deshabilitados


def _retention_days(db: Session, tenant_id: int) -> int:
    tenant = db.get(Tenant, tenant_id)
    return tenant.notification_retention_days if tenant is not None else 90


def _dispatch(
    db: Session,
    tenant_id: int,
    *,
    event_type: str,
    related_entity_type: str,
    related_entity_id: UUID,
    payload: Mapping[str, Any],
) -> NotificationEvent:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    event = NotificationEvent(
        event_type=event_type,
        related_entity_type=related_entity_type,
        related_entity_id=related_entity_id,
        payload=_jsonable(dict(payload)),
        purge_at=now + timedelta(days=_retention_days(db, tenant_id)),
    )
    db.add(event)
    db.commit()

    for nombre in _enabled_channel_names(db, event_type):
        canal: NotificationChannel = _CHANNELS[nombre](tenant_id, db)
        try:
            canal.send(event)
        except Exception:
            # Mismo criterio fail-open que `events.publish()`: un canal caído
            # no puede tumbar la operación de negocio, que ya está comprometida.
            logger.warning(
                "Canal %s falló para la notificación %s (tenant %s)",
                nombre, event.id, tenant_id, exc_info=True,
            )
    return event


def notify_order_created(
    db: Session,
    tenant_id: int,
    *,
    order_id: UUID,
    table_session_id: UUID | None,
    dining_table_id: UUID | None,
    table_number: int | None,
    customer_name: str | None,
    items_count: int,
    total: Decimal,
) -> NotificationEvent:
    """El comensal envió su carrito (mismo disparador que `events.order_created`,
    research.md §11)."""
    return _dispatch(
        db, tenant_id,
        event_type="order.created",
        related_entity_type="customer_order",
        related_entity_id=order_id,
        payload={
            "table_session_id": table_session_id,
            "dining_table_id": dining_table_id,
            "table_number": table_number,
            "customer_name": customer_name,
            "items_count": items_count,
            "total": total,
        },
    )


def notify_payment_completed(
    db: Session,
    tenant_id: int,
    *,
    sale_id: UUID,
    table_session_id: UUID | None,
    total: Decimal,
    customer_name: str | None,
    billing_mode: str,
    invoice: Mapping[str, Any] | None = None,
    origin_client_id: str | None = None,
) -> NotificationEvent:
    """Una venta emitida (mismo disparador que `events.payment_completed`).

    `origin_client_id` (spec 125, FR-007): id de la pestaña/terminal que originó el
    cobro, si lo mandó. Viaja en el payload persistido y se reenvía en el evento
    `notification.created` (`InAppChannel.send()`) para que esa misma terminal pueda
    silenciar su propio aviso sin afectar la entrega a las demás.
    """
    return _dispatch(
        db, tenant_id,
        event_type="payment.completed",
        related_entity_type="sale",
        related_entity_id=sale_id,
        payload={
            "table_session_id": table_session_id,
            "total": total,
            "customer_name": customer_name,
            "billing_mode": billing_mode,
            "invoice": invoice,
            "origin_client_id": origin_client_id,
        },
    )
