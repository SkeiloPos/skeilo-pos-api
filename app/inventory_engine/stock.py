"""Motor de stock único (sin lotes). Toda variación pasa por `record_movement`,
que escribe el kardex (`inventory_movements`) y ajusta `current_stock` de forma
atómica bajo `SELECT ... FOR UPDATE`.
"""
from decimal import Decimal
from uuid import UUID
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import InsufficientStockError
from app.models.inventory_item import InventoryItem
from app.models.inventory_movement import InventoryMovement


def _track_stock(db: Session, item_id: UUID, initial: Decimal, final: Decimal) -> None:
    """Registra en `db.info` el stock inicial/final de un insumo dentro de esta
    transacción (research.md D4, spec 148). El oyente de sesión en
    `app/core/availability_hooks.py` usa esto para publicar `availability.changed`
    después del commit, calculando la diferencia de pedibilidad una sola vez por
    transacción. El inicial se fija solo la primera vez que se toca el insumo
    (para que una venta que baja y repone el mismo insumo en la misma transacción
    no dispare ningún aviso); el final se actualiza en cada movimiento."""
    stocks: dict[UUID, tuple[Decimal, Decimal]] = db.info.setdefault("availability_stock", {})
    if item_id not in stocks:
        stocks[item_id] = (initial, final)
    else:
        stocks[item_id] = (stocks[item_id][0], final)


def lock_items(db: Session, item_ids: Iterable[UUID]) -> dict[UUID, InventoryItem]:
    """Bloquea en **una sola query y en orden de id** todos los insumos indicados.

    El orden fijo es lo que evita el deadlock: sin él, dos transacciones que
    consumen los mismos insumos en distinto orden (dos confirmaciones de mesas
    distintas con recetas que comparten insumos) se bloquean mutuamente. Llamar a
    esto antes de la primera mutación deja los locks tomados en orden canónico;
    los `SELECT … FOR UPDATE` posteriores de `record_movement` sobre esas mismas
    filas ya son no-ops dentro de la misma transacción.
    """
    ids = sorted(set(item_ids))
    if not ids:
        return {}
    items = db.execute(
        select(InventoryItem)
        .where(InventoryItem.id.in_(ids))
        .order_by(InventoryItem.id)
        .with_for_update()
    ).scalars().all()
    return {item.id: item for item in items}


def record_movement(
    db: Session,
    inventory_item_id: UUID,
    *,
    type: str,
    quantity: Decimal,
    reason: Optional[str] = None,
    reference_type: Optional[str] = None,
    reference_id: Optional[UUID] = None,
    user_id: Optional[UUID] = None,
    allow_negative: bool = False,
) -> InventoryMovement:
    """Aplica un movimiento de inventario. `quantity` es siempre > 0; el signo lo
    determina `type`: 'in' suma, 'out' resta. Bloquea la fila del insumo para
    evitar condiciones de carrera.

    'adjustment' **no** se acepta aquí: el signo lo lleva `signed_delta`, así que
    va por `apply_adjustment`. Antes se colaba y se comportaba silenciosamente
    como una salida."""
    if quantity <= 0:
        raise ValueError("quantity must be > 0")
    if type not in ("in", "out"):
        raise ValueError(
            f"record_movement solo acepta 'in'/'out' (recibido: {type!r}); "
            "usa apply_adjustment para los ajustes con signo"
        )

    item = db.execute(
        select(InventoryItem).where(InventoryItem.id == inventory_item_id).with_for_update()
    ).scalar_one()

    delta = quantity if type == "in" else -quantity
    new_stock = item.current_stock + delta
    if new_stock < 0 and not allow_negative:
        raise InsufficientStockError(
            f"Stock insuficiente de '{item.name}': disponible {item.current_stock}, "
            f"requerido {quantity}.",
            item_name=item.name,
        )
    _track_stock(db, inventory_item_id, item.current_stock, new_stock)
    item.current_stock = new_stock

    movement = InventoryMovement(
        inventory_item_id=inventory_item_id,
        type=type,
        quantity=quantity,
        reason=reason,
        reference_type=reference_type,
        reference_id=reference_id,
        user_id=user_id,
    )
    db.add(movement)
    return movement


def apply_adjustment(
    db: Session,
    inventory_item_id: UUID,
    *,
    signed_delta: Decimal,
    reason: Optional[str] = None,
    user_id: Optional[UUID] = None,
) -> InventoryMovement:
    """Ajuste manual de stock (delta con signo). Registra un movimiento
    'adjustment' con la magnitud y mueve el stock en la dirección del delta."""
    if signed_delta == 0:
        raise ValueError("signed_delta must be != 0")
    item = db.execute(
        select(InventoryItem).where(InventoryItem.id == inventory_item_id).with_for_update()
    ).scalar_one()
    new_stock = item.current_stock + signed_delta
    if new_stock < 0:
        raise InsufficientStockError(
            f"El ajuste dejaría el stock de '{item.name}' en negativo."
        )
    _track_stock(db, inventory_item_id, item.current_stock, new_stock)
    item.current_stock = new_stock
    movement = InventoryMovement(
        inventory_item_id=inventory_item_id,
        type="adjustment",
        quantity=abs(signed_delta),
        reason=reason,
    )
    movement.user_id = user_id
    db.add(movement)
    return movement
