"""Resuelve y persiste las adiciones (productos-topping, spec 129) de una línea de
pedido **en la misma transacción** que crea esa línea -- compartido por
`orders.service.create_order` (comanda nueva) y `orders.consolidation.add_item_to_order`
(ítem anexado a un pedido ya existente), que antes dejaban esto para una llamada
aparte por adición (`POST /orders/items/{item_id}/toppings`, `orders.toppings`),
un N+1 de escrituras evitable porque la selección ya se conoce al armar la línea.

Mismo criterio de descuento de inventario que `orders.toppings.add_topping_to_order_item`
(variante PRINCIPAL de la adición, nunca la del producto al que va adjunta) y de
precio (catálogo, cobrado una vez por línea -- nunca × la cantidad del producto)."""
from collections import defaultdict
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.catalog.consumption_plan import plan_line_consumption
from app.api.v1.catalog.line_pricing import check_availability
from app.api.v1.catalog.schemas import ToppingSelectionIn
from app.api.v1.catalog.service import resolve_principal_variant
from app.api.v1.inventory.stock import record_movement
from app.core import inventory_reasons as reasons
from app.models.order_item import OrderItem, OrderItemTopping
from app.models.product_topping import ProductTopping
from app.models.product_variant import ProductVariant

#: Una adición ya resuelta a su variante principal, con la cantidad elegida.
ResolvedTopping = tuple[ProductVariant, int]


def resolve_toppings(
    db: Session, product_id: UUID, selections: list[ToppingSelectionIn],
) -> tuple[list[ResolvedTopping], Decimal]:
    """Valida que cada adición esté asociada a `product_id` y la resuelve a su
    variante principal. Devuelve las adiciones resueltas y su suma a precio de
    catálogo (FR-017) -- lista vacía / `Decimal(0)` sin ninguna adición elegida."""
    resolved: list[ResolvedTopping] = []
    total = Decimal("0")
    for sel in selections:
        associated = db.execute(
            select(ProductTopping.id).where(
                ProductTopping.product_id == product_id,
                ProductTopping.topping_product_id == sel.topping_product_id,
            )
        ).scalar()
        if associated is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"Esa adición no está asociada a este producto: {sel.topping_product_id}",
            )
        topping_variant = resolve_principal_variant(db, sel.topping_product_id)
        resolved.append((topping_variant, sel.quantity))
        total += topping_variant.price * sel.quantity
    return resolved, total


def persist_toppings(db: Session, item: OrderItem, resolved: list[ResolvedTopping]) -> None:
    """Crea las filas `OrderItemTopping` -- siempre, sin importar si el descuento de
    inventario se difiere (mismo criterio que `OrderItemOption`: la selección se
    guarda aparte de si ya descontó stock)."""
    for topping_variant, quantity in resolved:
        db.add(OrderItemTopping(
            order_item_id=item.id, topping_product_variant_id=topping_variant.id,
            quantity=quantity, unit_price=topping_variant.price,
        ))


def accumulate_consumption(
    db: Session, resolved: list[ResolvedTopping], into: dict[UUID, Decimal],
) -> None:
    """Suma al diccionario `into` (insumo -> cantidad) lo que consumirían estas
    adiciones -- para poder validar disponibilidad de TODAS las líneas de un
    pedido de una vez (`check_availability`), en vez de una consulta por adición."""
    for topping_variant, quantity in resolved:
        for line in plan_line_consumption(db, topping_variant.id, quantity, []):
            into[line.inventory_item_id] += line.quantity


def deduct_toppings(
    db: Session, resolved: list[ResolvedTopping], user_id: UUID | None, reference_id: UUID,
) -> None:
    """Descuenta el inventario de estas adiciones -- llamar solo después de que
    `check_availability` ya validó el agregado de todas las líneas del pedido."""
    for topping_variant, quantity in resolved:
        for line in plan_line_consumption(db, topping_variant.id, quantity, []):
            record_movement(
                db, line.inventory_item_id, type="out", quantity=line.quantity,
                reason=reasons.VENTA, reference_type=reasons.REF_ORDER,
                reference_id=reference_id, user_id=user_id,
            )
