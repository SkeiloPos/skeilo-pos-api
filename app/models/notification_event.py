from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, UUIDPrimaryKeyMixin


class NotificationEvent(UUIDPrimaryKeyMixin, Base):
    """Copia durable de un hecho de negocio dirigido al staff (spec 077,
    RF-006). No reemplaza al bus de tiempo real (`app/core/events.py`): es la
    fuente de verdad para el listado paginado, la recuperación tras
    desconexión (RF-008) y la purga por retención (RNF-005)."""

    __tablename__ = "notification_events"

    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    related_entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    related_entity_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    payload: Mapped[Any] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )
    # Retención vigente al crear la fila (`created_at` + `tenants.notification_
    # retention_days` de ese momento) — no se recalcula si el tenant cambia su
    # retención después (data-model.md § Nota de purga).
    purge_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    # `NULL` = pendiente para todo el tenant (marca compartida, no por usuario).
    attended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    attended_by_user_id: Mapped[Optional[UUID]] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    # Rol al que va dirigida la notificación (spec 161, FR-031, A-164). `NULL` = todo el personal, que es el
    # comportamiento de todos los eventos anteriores a la spec 161 (`order.created`, `payment.completed`): sin
    # backfill ni cambio para ellos. Con valor (`ADMIN`), solo los usuarios de ese rol la ven en el listado,
    # en el canal en tiempo real (`staff:<rol>`) y en el push; los demás roles ni siquiera pueden marcarla
    # atendida (404).
    audience_role: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # Resumen informativo de intentos por canal, p. ej.
    # {"in_app": {"delivered_at": "..."}, "push": {"attempted": 3, "delivered": 2}}.
    delivery: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        # Un aviso de suscripción por tipo y ciclo de vencimiento (spec 161, FR-029, SC-008): `related_entity_id`
        # se deriva del tipo y de `plan_vence_en`, así que repetirlo es el mismo aviso. Parcial: no toca a los
        # demás tipos de evento, que no tienen esa unicidad. Garantiza «una sola vez» aunque dos instancias
        # del planificador corran a la vez.
        Index(
            "ux__notification_events__subscription_once",
            "event_type",
            "related_entity_id",
            unique=True,
            postgresql_where=text("event_type LIKE 'subscription.%'"),
            sqlite_where=text("event_type LIKE 'subscription.%'"),
        ),
        {"schema": "tenant"},
    )
