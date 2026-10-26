from uuid import UUID

from sqlalchemy import select, exists
from sqlalchemy.orm import Session

from app.models.inventory_item import InventoryItem
from app.models.purchase import PurchaseItem


def unit_measure_in_use(db: Session, unit_measure_id: UUID) -> bool:
    """`True` si `unit_measure_id` está referenciada por algún `InventoryItem` o
    `PurchaseItem`, activo o inactivo (research.md D3, Assumptions del spec 137:
    "en uso" incluye histórico)."""
    inventory_exists = select(
        exists().where(InventoryItem.unit_measure_id == unit_measure_id)
    )
    purchase_exists = select(
        exists().where(PurchaseItem.unit_measure_id == unit_measure_id)
    )
    return bool(db.execute(inventory_exists).scalar()) or bool(
        db.execute(purchase_exists).scalar()
    )
