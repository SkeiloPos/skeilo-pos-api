"""Lógica de negocio de la relación proveedor-insumo (spec 106).

Un archivo propio, importado por `router.py` -- mismo patrón que `stock.py`/`export.py`
(research.md D1): la relación es un sub-recurso tanto de `/inventory/items` como de
`/inventory/suppliers`, ambos servidos por el único router de `inventory`.
"""
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select, update as sa_update, func, text
from sqlalchemy.orm import Session

from app.core.crud import get_or_404
from app.core.timezone import utc_now
from app.api.v1.inventory.stock import lock_items
from app.models.supplier import Supplier
from app.models.supplier_item import SupplierItem
from app.models.inventory_item import InventoryItem
from app.models.purchase import PurchaseItem
from app.api.v1.inventory.schemas import SupplierItemCreate, SupplierItemUpdate


def _get_or_404(db: Session, relation_id: UUID) -> SupplierItem:
    return get_or_404(db, SupplierItem, relation_id, "Relación proveedor-insumo no encontrada")


def _format_decimal(value: Decimal) -> str:
    """Quita ceros decimales sobrantes para que la equivalencia se lea '500', no
    '500.000' (FR-007 muestra "1 Bolsa x 500 g = 500 g · $16/g", no "500.000")."""
    text_value = str(value)
    if "." in text_value:
        text_value = text_value.rstrip("0").rstrip(".")
    return text_value


def _equivalence(relation: SupplierItem, unit_abbreviation: str) -> Optional[str]:
    if relation.last_cost is None:
        return None
    cost_per_unit = (relation.last_cost / relation.conversion_factor).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return (
        f"1 {relation.presentation} = {_format_decimal(relation.conversion_factor)} "
        f"{unit_abbreviation} · ${_format_decimal(cost_per_unit)}/{unit_abbreviation}"
    )


def to_response(db: Session, relation: SupplierItem, was_preferred: bool = False) -> dict:
    supplier = db.get(Supplier, relation.supplier_id)
    item = db.get(InventoryItem, relation.inventory_item_id)
    return {
        "id": relation.id,
        "supplier_id": relation.supplier_id,
        "supplier_name": supplier.name if supplier else "",
        "inventory_item_id": relation.inventory_item_id,
        "inventory_item_name": item.name if item else "",
        "presentation": relation.presentation,
        "conversion_factor": relation.conversion_factor,
        "last_cost": relation.last_cost,
        "last_cost_at": relation.last_cost_at,
        "active": relation.active,
        "preferred": relation.preferred,
        "equivalence": _equivalence(relation, item.unit_measure.abbreviation if item else ""),
        "was_preferred": was_preferred,
    }


def _factor_is_locked(db: Session, relation_id: UUID) -> bool:
    """FR-014: una vez que una relación aparece en al menos una línea de compra, su factor
    ya no se puede editar -- el snapshot protege el histórico (precarga/sugerido)."""
    return db.execute(
        select(PurchaseItem.id).where(PurchaseItem.supplier_item_id == relation_id).limit(1)
    ).first() is not None


def _reject_if_system_supplier(supplier: Supplier) -> None:
    if supplier.is_system:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No se puede vincular el proveedor de sistema 'Compra ocasional'",
        )


def _reject_if_factor_invalid(factor: Decimal, item: InventoryItem) -> None:
    """FR-002: el factor debe ser > 0; el mensaje referencia la unidad base del insumo, no
    un texto genérico -- por eso no se valida con `Field(gt=0)` en el schema."""
    if factor <= 0:
        unit = item.unit_measure.abbreviation
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"El factor debe ser mayor que 0: indica cuántos {unit} trae 1 "
                   f"presentación",
        )


def create_or_reactivate(
    db: Session,
    *,
    supplier_id: UUID,
    inventory_item_id: UUID,
    body: SupplierItemCreate,
) -> tuple[SupplierItem, int]:
    """FR-001/FR-004/FR-005/FR-012/FR-013. Devuelve (relación, status_code)."""
    supplier = get_or_404(db, Supplier, supplier_id, "Proveedor no encontrado")
    item = get_or_404(db, InventoryItem, inventory_item_id, "Insumo no encontrado")
    _reject_if_system_supplier(supplier)
    _reject_if_factor_invalid(body.conversion_factor, item)

    existing = db.execute(
        select(SupplierItem).where(
            SupplierItem.supplier_id == supplier_id,
            SupplierItem.inventory_item_id == inventory_item_id,
            func.lower(func.trim(SupplierItem.presentation))
            == body.presentation.strip().lower(),
        )
    ).scalar_one_or_none()

    if existing is not None and existing.active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "existing_id": str(existing.id),
                "detail": "Ya existe una relación activa con esta presentación; edita la "
                          "existente en su lugar",
            },
        )

    if existing is not None and not existing.active:
        existing.active = True
        existing.presentation = body.presentation
        existing.conversion_factor = body.conversion_factor
        if body.last_cost is not None:
            existing.last_cost = body.last_cost
            existing.last_cost_at = utc_now()
        db.commit()
        db.refresh(existing)
        return existing, status.HTTP_200_OK

    relation = SupplierItem(
        supplier_id=supplier_id,
        inventory_item_id=inventory_item_id,
        presentation=body.presentation,
        conversion_factor=body.conversion_factor,
        last_cost=body.last_cost,
        last_cost_at=utc_now() if body.last_cost is not None else None,
    )
    db.add(relation)
    db.commit()
    db.refresh(relation)
    return relation, status.HTTP_201_CREATED


def _apply_preferred_swap(db: Session, relation: SupplierItem) -> None:
    """RN-16/FR-008/FR-009 (research.md D2): bloquea el insumo padre antes de desmarcar la
    preferida anterior y marcar la nueva, en ese orden, dentro de la misma transacción -- es
    lo único que no viola el índice parcial `ux__supplier_items__preferred` (no DEFERRABLE) a
    mitad de camino. No hace commit -- el llamador decide cuándo cerrar la transacción.

    Las dos mitades del swap se hacen con dos UPDATE explícitos (`synchronize_session=False`),
    no con una mezcla de UPDATE masivo + atributo ORM -- bajo concurrencia real, mezclar
    ambos dejó alguna vez la fila ganadora en `False` (el UPDATE masivo de la otra conexión,
    que corre después de que esta transacción ya commiteó, no tiene forma de saber que el
    atributo en memoria de este proceso decía `True` si el propio commit no lo materializó
    todavía de forma explícita vía SQL). Dos UPDATE explícitos no tienen esa ambigüedad."""
    lock_items(db, [relation.inventory_item_id])
    db.execute(
        sa_update(SupplierItem)
        .where(
            SupplierItem.inventory_item_id == relation.inventory_item_id,
            SupplierItem.preferred.is_(True),
            SupplierItem.id != relation.id,
        )
        .values(preferred=False)
        .execution_options(synchronize_session=False)
    )
    db.execute(
        sa_update(SupplierItem)
        .where(SupplierItem.id == relation.id)
        .values(preferred=True)
        .execution_options(synchronize_session=False)
    )
    db.expire(relation, ["preferred"])


def update(db: Session, relation_id: UUID, body: SupplierItemUpdate) -> tuple[SupplierItem, bool]:
    """FR-006/FR-010/FR-011/FR-014. Devuelve (relación, was_preferred)."""
    relation = _get_or_404(db, relation_id)

    if body.conversion_factor is not None:
        if _factor_is_locked(db, relation_id):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Esta relación ya se usó en una compra; no se puede cambiar su "
                       "factor. Crea una presentación nueva en su lugar.",
            )
        item = get_or_404(db, InventoryItem, relation.inventory_item_id, "Insumo no encontrado")
        _reject_if_factor_invalid(body.conversion_factor, item)

    will_be_active = body.active if body.active is not None else relation.active
    if body.preferred and not will_be_active:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Solo una relación activa puede marcarse como preferida",
        )
    if body.preferred:
        supplier = get_or_404(db, Supplier, relation.supplier_id, "Proveedor no encontrado")
        _reject_if_system_supplier(supplier)

    was_preferred = bool(body.active is False and relation.preferred)

    if body.presentation is not None:
        relation.presentation = body.presentation
    if body.conversion_factor is not None:
        relation.conversion_factor = body.conversion_factor
    if body.last_cost is not None:
        relation.last_cost = body.last_cost
        relation.last_cost_at = utc_now()
    if body.active is not None:
        relation.active = body.active
        if body.active is False:
            relation.preferred = False

    if body.preferred:
        _apply_preferred_swap(db, relation)

    db.commit()
    db.refresh(relation)
    return relation, was_preferred


def set_preferred(db: Session, relation_id: UUID) -> SupplierItem:
    """Variante de un solo campo de `update()`, para el caso directo "marcar preferida" sin
    editar nada más (ver research.md D2 para el orden transaccional)."""
    relation = _get_or_404(db, relation_id)
    if not relation.active:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Solo una relación activa puede marcarse como preferida",
        )
    supplier = get_or_404(db, Supplier, relation.supplier_id, "Proveedor no encontrado")
    _reject_if_system_supplier(supplier)

    _apply_preferred_swap(db, relation)
    db.commit()
    db.refresh(relation)
    return relation


def list_for_item(
    db: Session, inventory_item_id: UUID, include_inactive: bool
) -> list[SupplierItem]:
    get_or_404(db, InventoryItem, inventory_item_id, "Insumo no encontrado")
    query = select(SupplierItem).where(SupplierItem.inventory_item_id == inventory_item_id)
    if not include_inactive:
        query = query.where(SupplierItem.active.is_(True))
    query = query.order_by(SupplierItem.preferred.desc(), SupplierItem.created_at.asc())
    return list(db.execute(query).scalars().all())


def list_for_supplier(db: Session, supplier_id: UUID, include_inactive: bool) -> list[SupplierItem]:
    supplier = get_or_404(db, Supplier, supplier_id, "Proveedor no encontrado")
    if supplier.is_system:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Proveedor no encontrado",
        )
    query = select(SupplierItem).where(SupplierItem.supplier_id == supplier_id)
    if not include_inactive:
        query = query.where(SupplierItem.active.is_(True))
    query = query.order_by(SupplierItem.preferred.desc(), SupplierItem.created_at.asc())
    return list(db.execute(query).scalars().all())


def attach_preferred_supplier_names(db: Session, items) -> None:
    """Fija `item.preferred_supplier_name` (atributo transitorio, no una columna mapeada)
    en cada `InventoryItem` recibido, para que `InventoryItemResponse.preferred_supplier_name`
    lo lea vía `from_attributes`. Una sola consulta para todo el lote -- se usa en el
    listado y el detalle de insumo, no en la relación proveedor-insumo misma."""
    items = list(items)
    if not items:
        return
    ids = [item.id for item in items]
    rows = db.execute(
        select(SupplierItem.inventory_item_id, Supplier.name)
        .join(Supplier, Supplier.id == SupplierItem.supplier_id)
        .where(
            SupplierItem.inventory_item_id.in_(ids),
            SupplierItem.preferred.is_(True),
            SupplierItem.active.is_(True),
        )
    ).all()
    names_by_item = {item_id: name for item_id, name in rows}
    for item in items:
        item.preferred_supplier_name = names_by_item.get(item.id)
