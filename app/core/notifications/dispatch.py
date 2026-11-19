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
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.events import _jsonable
from app.core.models import Tenant
from app.core.subscription import SubscriptionStatus
from app.core.notifications.channels.base import NotificationChannel
from app.core.notifications.channels.browser_push import BrowserPushChannel
from app.core.notifications.channels.in_app import InAppChannel
from app.models.notification_channel_pref import NotificationChannelPreference
from app.models.notification_event import NotificationEvent
from app.core.timezone import utc_now_naive

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
    audience_role: str | None = None,
) -> NotificationEvent:
    """`audience_role` (spec 161, A-164): rol destinatario; `None` = todo el personal (todos los eventos de
    antes de la spec 161, que no lo pasan)."""
    now = utc_now_naive()
    event = NotificationEvent(
        event_type=event_type,
        related_entity_type=related_entity_type,
        related_entity_id=related_entity_id,
        payload=_jsonable(dict(payload)),
        purge_at=now + timedelta(days=_retention_days(db, tenant_id)),
        audience_role=audience_role,
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


# ======================================================================
# Avisos de suscripción (spec 161, Historia de Usuario 5: FR-028 a FR-032, A-164)
# ======================================================================

#: Rol al que van dirigidos los tres avisos de suscripción: solo el ADMIN del tenant.
SUBSCRIPTION_AUDIENCE = "ADMIN"

#: Espacio de nombres fijo de `uuid5`: derivar el mismo `related_entity_id` para el mismo aviso y ciclo.
_SUBSCRIPTION_NAMESPACE = uuid.UUID("5b0f3e0a-2f7c-5c1e-9a55-7e6d6a1c0161")

_MESES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


def _subscription_entity_id(event_type: str, plan_vence_en: datetime) -> UUID:
    """`related_entity_id` del aviso: estable para un tipo y un **ciclo de vencimiento**, y distinto en cuanto un
    pago aprobado mueve `plan_vence_en`. Junto con el índice único parcial
    `ux__notification_events__subscription_once` es lo que garantiza «un aviso por tipo y ciclo» (FR-029),
    aunque el job corra muchas veces o en varias instancias a la vez."""
    return uuid.uuid5(_SUBSCRIPTION_NAMESPACE, f"{event_type}|{plan_vence_en.isoformat()}")


def _fecha_local(moment: datetime, timezone_name: str) -> str:
    """`6 de diciembre`: la fecha límite en la zona horaria del TENANT. `summarize()` no recibe el tenant, así
    que el texto se formatea al emitir el aviso y viaja en el payload. Una zona desconocida cae a la de por
    defecto del sistema en vez de romper la emisión."""
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo(settings.TENANT_TIMEZONE)
    local = moment.replace(tzinfo=timezone.utc).astimezone(zone)
    return f"{local.day} de {_MESES[local.month - 1]}"


def _dispatch_once(db: Session, tenant_id: int, *, event_type: str, related_entity_id: UUID, **kwargs) -> NotificationEvent | None:
    """Como `_dispatch`, pero **una sola vez** por `(event_type, related_entity_id)`: devuelve `None` si ese
    aviso ya existe. La consulta previa evita el insert en el caso normal; el `IntegrityError` del índice único
    parcial cubre la carrera entre dos instancias del planificador."""
    ya_existe = db.execute(
        select(NotificationEvent.id).where(
            NotificationEvent.event_type == event_type,
            NotificationEvent.related_entity_id == related_entity_id,
        )
    ).first()
    if ya_existe is not None:
        return None
    try:
        return _dispatch(db, tenant_id, event_type=event_type, related_entity_id=related_entity_id, **kwargs)
    except IntegrityError:
        db.rollback()
        return None


def _notify_subscription(db: Session, tenant: Tenant, event_type: str, status: SubscriptionStatus) -> NotificationEvent | None:
    return _dispatch_once(
        db, tenant.id,
        event_type=event_type,
        related_entity_type="subscription",
        related_entity_id=_subscription_entity_id(event_type, tenant.plan_vence_en),
        payload={
            "plan_vence_en": tenant.plan_vence_en,
            "fecha_limite_pago": status.fecha_limite_pago,
            "dias_restantes": status.dias_restantes,
            "fecha_limite_pago_local": _fecha_local(status.fecha_limite_pago, tenant.timezone),
        },
        audience_role=SUBSCRIPTION_AUDIENCE,
    )


def notify_subscription_expiring(db: Session, tenant: Tenant, status: SubscriptionStatus) -> NotificationEvent | None:
    """Faltan 48 h o menos para el vencimiento (estado `por_vencer`). Devuelve `None` si ya se avisó en este ciclo."""
    return _notify_subscription(db, tenant, "subscription.expiring", status)


def notify_subscription_grace_started(db: Session, tenant: Tenant, status: SubscriptionStatus) -> NotificationEvent | None:
    """Venció el plan e inició la gracia de 5 días (estado `en_gracia`), con la fecha límite de pago."""
    return _notify_subscription(db, tenant, "subscription.grace_started", status)


def notify_subscription_blocked(db: Session, tenant: Tenant, status: SubscriptionStatus) -> NotificationEvent | None:
    """Se agotó la gracia sin pago aprobado (estado `bloqueada`). El ADMIN lo puede leer aunque el resto del
    sistema esté bloqueado: `/notifications` y el ticket de tiempo real son rutas exentas."""
    return _notify_subscription(db, tenant, "subscription.blocked", status)
