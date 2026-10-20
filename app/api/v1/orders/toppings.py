"""Agregar una adición (producto-topping) a una línea de pedido ya persistida
(spec 129). Paralelo a `orders/additions.py` (spec 123), pero sobre
`OrderItemTopping` en vez de `OrderItemOption` -- las adiciones ya no viven en
grupos de opciones (research.md D4 de spec 129).

Misma mecánica de pago complementario que `additions.py` (`added_after_sale`/
`complementary_sale_id`): si el pedido ya tiene una `Sale` emitida, el descuento
de inventario se difiere y la `Sale` ya emitida nunca se toca (Principio VII).

El precio de una adición adjunta MUST ser siempre su precio de catálogo (FR-017):
se cobra una vez por línea (`addons_total`), nunca multiplicado por
`item.quantity` -- misma regla ya vigente para adiciones "Por cantidad" (spec 117,
spec.md Assumptions).

El consumo de inventario de una adición adjunta reusa `plan_line_consumption`
sobre la variante PRINCIPAL de la adición (research.md D5 de spec 129) y su
propia cantidad elegida, nunca la de `item.quantity` -- a diferencia de
`plan_addition_consumption` (que resuelve slots de la variante del producto
principal), aquí la receta es la de la adición misma."""
from collections import defaultdict
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crud import get_or_404
from app.core.models import Tenant, User
from app.core.plan_limits import has_module_access
from app.models.customer_order import CustomerOrder
from app.models.order_item import EN_CURSO, OrderItem, OrderItemTopping
from app.models.product_variant import ProductVariant
from app.models.product_topping import ProductTopping
from app.api.v1.catalog.schemas import ToppingSelectionIn, UpdateToppingQuantityIn
from app.api.v1.catalog.service import resolve_principal_variant
from app.api.v1.catalog.line_pricing import check_availability
from app.api.v1.catalog.consumption_plan import plan_line_consumption
from app.api.v1.inventory.stock import record_movement
from app.core import inventory_reasons as reasons
from app.api.v1.orders import service


def _ensure_editable(db: Session, item_id: UUID) -> tuple[OrderItem, CustomerOrder]:
    item = get_or_404(db, OrderItem, item_id, "Order item not found")
    order = get_or_404(db, CustomerOrder, item.order_id, "Order not found")
    if order.status == "cancelada":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Este pedido está cancelado; no se le pueden agregar adiciones.",
        )
    if item.estado_cocina not in EN_CURSO:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Producto ya preparado: agrega un producto nuevo"
        )
    return item, order

def _consumption_required(db: Session, variant_id: UUID, quantity: int) -> dict[UUID, Decimal]:
    required: dict[UUID, Decimal] = defaultdict(Decimal)
    for line in plan_line_consumption(db, variant_id, quantity, []):
        required[line.inventory_item_id] += line.quantity
    return dict(required)


def _deduct(db: Session, variant_id: UUID, quantity: int, user_id, reference_id: UUID) -> None:
    for line in plan_line_consumption(db, variant_id, quantity, []):
        record_movement(
            db, line.inventory_item_id, type="out", quantity=line.quantity,
            reason=reasons.VENTA, reference_type=reasons.REF_ORDER,
            reference_id=reference_id, user_id=user_id,
        )


def _reverse(db: Session, variant_id: UUID, quantity: int, user_id, reference_id: UUID) -> None:
    for line in plan_line_consumption(db, variant_id, quantity, []):
        record_movement(
            db, line.inventory_item_id, type="in", quantity=line.quantity,
            reason=reasons.AJUSTE, reference_type=reasons.REF_ORDER,
            reference_id=reference_id, user_id=user_id,
        )


def add_topping_to_order_item(
    db: Session, item_id: UUID, data: ToppingSelectionIn, user: User, tenant: Tenant,
) -> CustomerOrder:
    item, order = _ensure_editable(db, item_id)

    variant = get_or_404(db, ProductVariant, item.product_variant_id, "Variant not found")
    associated = db.execute(
        select(ProductTopping.id).where(
            ProductTopping.product_id == variant.product_id,
            ProductTopping.topping_product_id == data.topping_product_id,
        )
    ).scalar()
    if associated is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Esa adición no está asociada a este producto.",
        )

    topping_variant = resolve_principal_variant(db, data.topping_product_id)

    existing = db.execute(
        select(OrderItemTopping.id).where(
            OrderItemTopping.order_item_id == item.id,
            OrderItemTopping.topping_product_variant_id == topping_variant.id,
        )
    ).scalar()
    if existing is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Esa adición ya está en la línea; edítala o quítala en vez de agregarla de nuevo.",
        )

    if has_module_access(db, tenant, "inventario"):
        check_availability(
            db, _consumption_required(db, topping_variant.id, data.quantity),
            extra_context="agregar adición",
        )

    added_after_sale = service.order_has_sale(db, order.id)

    db.add(OrderItemTopping(
        order_item_id=item.id, topping_product_variant_id=topping_variant.id,
        quantity=data.quantity, unit_price=topping_variant.price,
        added_after_sale=added_after_sale,
    ))
    # FR-017/FR-007 (Assumptions): precio de catálogo, cobrado una vez por línea --
    # nunca multiplicado por `item.quantity` (spec 117).
    item.addons_total = item.addons_total + topping_variant.price * data.quantity

    if not added_after_sale:
        if has_module_access(db, tenant, "inventario"):
            _deduct(db, topping_variant.id, data.quantity, user.id, order.id)
        from app.api.v1.orders.checkout import refresh_order_item_discounts
        refresh_order_item_discounts(db, order)
    # `added_after_sale=True`: la `Sale` ya emitida no se toca (Principio VII); el
    # descuento de inventario y el cobro se difieren al pago complementario.

    db.commit()
    return db.execute(select(CustomerOrder).where(CustomerOrder.id == order.id)).scalar_one()


def update_topping_quantity(
    db: Session, item_id: UUID, topping_product_id: UUID, data: UpdateToppingQuantityIn,
    user: User, tenant: Tenant,
) -> CustomerOrder:
    item, order = _ensure_editable(db, item_id)
    topping_variant = resolve_principal_variant(db, topping_product_id)

    existing = db.execute(
        select(OrderItemTopping).where(
            OrderItemTopping.order_item_id == item.id,
            OrderItemTopping.topping_product_variant_id == topping_variant.id,
        )
    ).scalar_one_or_none()
    if existing is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Esa adición no está en la línea; agrégala con «Agregar adición».",
        )
    if existing.complementary_sale_id is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Esa adición ya fue cobrada; anula la línea para rehacerla.",
        )
    if data.quantity == existing.quantity:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Esa adición ya tiene esa cantidad."
        )

    delta = data.quantity - existing.quantity
    if delta > 0 and has_module_access(db, tenant, "inventario"):
        check_availability(
            db, _consumption_required(db, topping_variant.id, delta),
            extra_context="editar adición",
        )

    existing.quantity = data.quantity
    item.addons_total = item.addons_total + topping_variant.price * delta

    if not existing.added_after_sale:
        if has_module_access(db, tenant, "inventario"):
            if delta > 0:
                _deduct(db, topping_variant.id, delta, user.id, order.id)
            else:
                _reverse(db, topping_variant.id, -delta, user.id, order.id)
        from app.api.v1.orders.checkout import refresh_order_item_discounts
        refresh_order_item_discounts(db, order)

    db.commit()
    return db.execute(select(CustomerOrder).where(CustomerOrder.id == order.id)).scalar_one()


def remove_topping(
    db: Session, item_id: UUID, topping_product_id: UUID, user: User, tenant: Tenant,
) -> CustomerOrder:
    item, order = _ensure_editable(db, item_id)
    topping_variant = resolve_principal_variant(db, topping_product_id)

    existing = db.execute(
        select(OrderItemTopping).where(
            OrderItemTopping.order_item_id == item.id,
            OrderItemTopping.topping_product_variant_id == topping_variant.id,
        )
    ).scalar_one_or_none()
    if existing is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Esa adición no está en la línea; agrégala con «Agregar adición».",
        )
    if existing.complementary_sale_id is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Esa adición ya fue cobrada; anula la línea para rehacerla.",
        )

    item.addons_total = item.addons_total - existing.unit_price * existing.quantity

    if not existing.added_after_sale:
        if has_module_access(db, tenant, "inventario"):
            _reverse(db, topping_variant.id, existing.quantity, user.id, order.id)
        from app.api.v1.orders.checkout import refresh_order_item_discounts
        refresh_order_item_discounts(db, order)

    db.delete(existing)
    db.commit()
    return db.execute(select(CustomerOrder).where(CustomerOrder.id == order.id)).scalar_one()
