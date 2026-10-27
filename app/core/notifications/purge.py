"""Purga por retención de notificaciones (spec 077, RNF-005).

Mismo patrón que `app/core/scheduler.py::expire_promotions()`: recorre
`shared.tenants`, abre `with_db(schema)` por tenant y borra las filas
vencidas. `purge_at` ya trae la retención congelada al momento de crear cada
`NotificationEvent` (data-model.md § Nota de purga) — este job no recalcula
contra la retención *actual* del tenant, solo filtra `purge_at < now()`.
"""
from __future__ import annotations

import logging

from sqlalchemy import delete, text

from app.core.db import with_db
from app.models.notification_event import NotificationEvent
from app.core.timezone import utc_now_naive

logger = logging.getLogger(__name__)


def purge_expired_notifications() -> int:
    """Borra, en todos los tenants, las `NotificationEvent` con `purge_at`
    vencido. Devuelve cuántas filas se purgaron en total."""
    now = utc_now_naive()

    with with_db(None) as db:
        schemas = [r[0] for r in db.execute(text("SELECT schema FROM shared.tenants")).fetchall()]

    total = 0
    for schema in schemas:
        try:
            with with_db(schema) as db:
                result = db.execute(
                    delete(NotificationEvent).where(NotificationEvent.purge_at < now)
                )
                db.commit()
                total += result.rowcount
        except Exception:
            # Un tenant roto no debe impedir la purga de los demás (mismo
            # criterio que `scheduler.py::sweep_orphan_sessions`).
            logger.exception("Error purgando notificaciones del schema %s", schema)

    if total:
        logger.info("Purga de notificaciones: %d fila(s) purgada(s)", total)
    return total
