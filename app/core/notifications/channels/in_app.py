"""Canal `in_app`: reutiliza el bus de tiempo real ya existente
(`app/core/events.py`), sin tocarlo — solo agrega un tipo de evento nuevo al
catálogo (`notification.created`). Ver contracts/realtime-events.md."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core import events
from app.core.events import CH_STAFF
from app.core.notifications.summary import summarize
from app.models.notification_event import NotificationEvent


class InAppChannel:
    """Publica un `notification.created` en el canal `staff` del tenant. Nunca
    en `session:{table_session_id}` — este evento es para el personal, no
    para el comensal (contracts/realtime-events.md)."""

    def __init__(self, tenant_id: int, db: Session) -> None:
        self._tenant_id = tenant_id

    def send(self, event: NotificationEvent) -> None:
        events.publish(
            self._tenant_id,
            type="notification.created",
            channels=[CH_STAFF],
            payload={
                "notification_id": event.id,
                "event_type": event.event_type,
                "related_entity_type": event.related_entity_type,
                "related_entity_id": event.related_entity_id,
                "summary": summarize(event),
                # spec 125 (FR-007): presente solo si el emisor lo mandó (hoy,
                # `payment.completed`) — la terminal de origen lo usa para
                # silenciar su propio aviso sin afectar a las demás.
                "origin_client_id": (event.payload or {}).get("origin_client_id"),
            },
        )
