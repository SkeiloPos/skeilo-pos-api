"""Gancho de sesión que detecta transiciones de "pedibilidad" causadas por
movimientos de inventario y publica `availability.changed` después del
commit, una sola vez por transacción (spec 148, research.md D4).

Cubre los 13 puntos de movimiento de stock actuales (y los futuros) sin
tocarlos: `app/inventory_engine/stock.py::record_movement`/`apply_adjustment`
ya registran, antes de mutar `current_stock`, el par (inicial, final) por
insumo en `db.info["availability_stock"]`. Este módulo solo lee esa marca.

Instalación **explícita**, nunca al importar (`install_availability_hooks()`
se llama desde `app/main.py` en el `lifespan` y desde `app/celery_task.py` en
`worker_process_init`): así ninguna suite de characterization tests existente
cambia de comportamiento ni intenta hablar con Redis sin que el test lo pida.
"""
from __future__ import annotations

import logging

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.catalog_engine.availability import compute_availability, diff_availability
from app.core import events
from app.core.models import Tenant

logger = logging.getLogger(__name__)

_installed = False

# {(id(bind), schema): tenant_id} -- evita resolver `schema -> tenant_id` con
# una query en cada commit. Se indexa también por el bind (no solo el schema)
# para no mezclar resultados entre motores distintos (p. ej. una suite de
# tests que reutiliza el mismo nombre de schema entre casos con tenants
# distintos); el par (bind, schema) -> tenant_id sí es inmutable.
_tenant_id_cache: dict[tuple[int, str], int] = {}


def _resolve_tenant_id(session: Session) -> int | None:
    """`session.info["tenant_id"]` si ya está (camino normal de `get_db`,
    spec 148); si no, lo resuelve a partir de `tenant_schema` (camino de
    Celery/scripts que usan `with_db` directo, research.md H5)."""
    tenant_id = session.info.get("tenant_id")
    if tenant_id is not None:
        return tenant_id

    schema = session.info.get("tenant_schema")
    if not schema:
        return None

    bind = session.get_bind()
    cache_key = (id(bind), schema)
    cached = _tenant_id_cache.get(cache_key)
    if cached is not None:
        return cached

    resolved = session.execute(
        select(Tenant.id).where(Tenant.schema == schema)
    ).scalar_one_or_none()
    if resolved is not None:
        _tenant_id_cache[cache_key] = resolved
    return resolved


def _before_commit(session: Session) -> None:
    """Calcula, mientras la transacción sigue abierta, la diferencia de
    pedibilidad entre el stock inicial y el final de los insumos tocados en
    esta sesión (FR-009). Nunca impide el commit (FR-005): cualquier error se
    registra y se descarta."""
    try:
        stocks: dict = session.info.get("availability_stock")
        if not stocks:
            return
        item_ids = list(stocks.keys())
        initial_stock = {item_id: pair[0] for item_id, pair in stocks.items()}
        final_stock = {item_id: pair[1] for item_id, pair in stocks.items()}

        before = compute_availability(session, stock=initial_stock, scope_item_ids=item_ids)
        after = compute_availability(session, stock=final_stock, scope_item_ids=item_ids)
        changes = diff_availability(before, after)
        if changes:
            session.info["availability_pending"] = changes
    except Exception:
        logger.exception(
            "Error calculando la diferencia de disponibilidad antes del commit"
        )


def _after_commit(session: Session) -> None:
    """Publica, ya confirmada la transacción, el lote de cambios calculado en
    `_before_commit` (FR-004: nunca antes del commit). `events.publish`
    (vía `availability_changed`) ya es fail-open por su cuenta, pero este
    oyente nunca debe propagar una excepción hacia el `commit()` del
    llamador tampoco."""
    changes = session.info.pop("availability_pending", None)
    session.info.pop("availability_stock", None)
    if not changes:
        return
    try:
        tenant_id = _resolve_tenant_id(session)
        if tenant_id is None:
            logger.warning(
                "No se pudo resolver tenant_id para publicar availability.changed "
                "(%d cambios perdidos)", len(changes),
            )
            return
        events.availability_changed(tenant_id, changes)
    except Exception:
        logger.exception("Error publicando availability.changed tras el commit")


def _after_rollback(session: Session) -> None:
    """Un rollback descarta lo acumulado: nunca se anuncia lo que no ocurrió
    (FR-004)."""
    session.info.pop("availability_pending", None)
    session.info.pop("availability_stock", None)


def install_availability_hooks() -> None:
    """Idempotente: una segunda llamada no duplica los oyentes."""
    global _installed
    if _installed:
        return
    event.listen(Session, "before_commit", _before_commit)
    event.listen(Session, "after_commit", _after_commit)
    event.listen(Session, "after_rollback", _after_rollback)
    _installed = True
