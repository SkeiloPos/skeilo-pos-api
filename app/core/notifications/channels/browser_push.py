"""Canal push del navegador (spec 077, RF-004). Web Push (RFC 8030) cifrado
con VAPID (RFC 8292) vía `pywebpush` (research.md §5)."""
from __future__ import annotations

import json
import logging

from pywebpush import WebPushException, webpush
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.models import Role, User
from app.core.notifications.summary import summarize
from app.models.notification_event import NotificationEvent
from app.models.push_subscription import PushSubscription

logger = logging.getLogger(__name__)


class BrowserPushChannel:
    """Intenta entregar a **todas** las `PushSubscription` activas del
    tenant, sin filtrar por el usuario que disparó el evento (data-model.md
    § PushSubscription — coherente con FR-003, "llega a todos los usuarios
    conectados del tenant"). Fire-and-forget: un push fallido no puede tumbar
    la operación de negocio que ya está comprometida (research.md §5, mismo
    criterio *fail-open* que `events.publish()`)."""

    def __init__(self, tenant_id: int, db: Session) -> None:
        self._tenant_id = tenant_id
        self._db = db

    def send(self, event: NotificationEvent) -> None:
        if not settings.VAPID_PRIVATE_KEY or not settings.VAPID_SUBJECT:
            logger.warning("VAPID no configurado; se omite el canal push")
            return

        consulta = select(PushSubscription).where(PushSubscription.active.is_(True))
        if event.audience_role:
            # spec 161 (A-164): una notificación dirigida a un rol solo se empuja a los dispositivos de usuarios de
            # ese rol. `users`/`roles` son del esquema `shared`, que el `schema_translate_map` no toca.
            consulta = consulta.where(
                PushSubscription.user_id.in_(
                    select(User.id).join(Role, User.role_id == Role.id).where(Role.name == event.audience_role)
                )
            )
        suscripciones = self._db.execute(consulta).scalars().all()
        if not suscripciones:
            return

        payload = json.dumps({
            "notification_id": str(event.id),
            "event_type": event.event_type,
            "related_entity_type": event.related_entity_type,
            "related_entity_id": str(event.related_entity_id),
            "summary": summarize(event),
        })

        cambios = False
        for sub in suscripciones:
            if self._send_one(sub, payload):
                cambios = True
        if cambios:
            self._db.commit()

    def _send_one(self, sub: PushSubscription, payload: str) -> bool:
        """Devuelve si desactivó la suscripción (para saber si hace falta
        `commit`)."""
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh_key, "auth": sub.auth_key},
                },
                data=payload,
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.VAPID_SUBJECT},
            )
            return False
        except WebPushException as exc:
            # 404/410: el push service dice que la suscripción ya no existe
            # (navegador desinstalado, permiso revocado, endpoint rotado) —
            # se marca inactiva. Cualquier otro código (5xx, timeout) puede
            # ser un fallo transitorio del push service, no de la suscripción
            # en sí (data-model.md § PushSubscription).
            if exc.status_code in (404, 410):
                sub.active = False
                return True
            logger.warning(
                "Push falló para la suscripción %s (status %s)",
                sub.id, exc.status_code, exc_info=True,
            )
            return False
        except Exception:
            logger.warning("Push falló para la suscripción %s", sub.id, exc_info=True)
            return False
