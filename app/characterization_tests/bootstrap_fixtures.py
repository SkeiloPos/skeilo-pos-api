"""Infraestructura compartida para los tests del arranque agregado del panel
(spec 100) — no es código de producción. Mismo patrón que `plan_fixtures.py`:
SQLite en memoria, colapsando `tenant`/`shared` al schema por defecto, más la
tabla `notification_events` (y su shim JSONB) para poder ejercitar
`build_bootstrap()` de punta a punta."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.characterization_tests.plan_fixtures import (  # noqa: F401 - reexportados
    make_cash_register,
    make_dining_table,
    make_payment_method,
    make_plan,
    make_product,
    make_role,
    make_tenant,
    make_user,
)
from app.core.models import Base
from app.core.timezone import utc_now
from app.models.notification_event import NotificationEvent
from app.models.plan import Plan  # noqa: F401 - registra "plans" (FK de Tenant)

_TABLE_NAMES = [
    "tenants",
    "plans",
    "roles",
    "users",
    "user_invitations",
    "dining_tables",
    "cash_registers",
    "products",
    "product_toppings",
    "payment_methods",
    "notification_events",
]


# `notification_events.payload` es `postgresql.JSONB` — mismo shim que ya usa
# `test_notifications.py`: sin él, `create_all()` falla con `CompileError`
# sobre SQLite.
@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json_on_sqlite(element, compiler, **kw):  # pragma: no cover
    return "JSON"


def new_session() -> Session:
    """Sesión SQLAlchemy real, limpia, sobre SQLite en memoria — colapsa
    tanto `tenant` como `shared` al schema por defecto de SQLite."""
    tables = [t for t in Base.metadata.tables.values() if t.name in _TABLE_NAMES]
    engine = create_engine("sqlite:///:memory:")
    conn = engine.connect().execution_options(
        schema_translate_map={"tenant": None, "shared": None}
    )
    Base.metadata.create_all(bind=conn, tables=tables)
    conn.commit()
    return Session(bind=conn)


def make_notification(db: Session, **kw) -> NotificationEvent:
    """Fila mínima válida de `NotificationEvent` — `related_entity_id` y
    `purge_at` no tienen default a nivel de ORM/DB."""
    kw.setdefault("event_type", "order.created")
    kw.setdefault("related_entity_type", "customer_order")
    kw.setdefault("related_entity_id", uuid.uuid4())
    kw.setdefault("payload", {})
    now = utc_now().replace(tzinfo=None)
    kw.setdefault("created_at", now)
    kw.setdefault("purge_at", now + timedelta(days=90))
    notification = NotificationEvent(**kw)
    db.add(notification)
    db.flush()
    return notification
