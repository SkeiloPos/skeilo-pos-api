"""División automática de cuenta por ítem (spec 164): tres operaciones nuevas
que mueven/dividen `OrderItem` individuales entre órdenes de la misma mesa —
`split_order`, `move_item`, `split_item_to_new_order`. Mismo patrón de
`tables_advanced.py`: funciones de servicio puras + transacción explícita,
endpoint delgado en `router.py`."""
from decimal import Decimal
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.crud import get_or_404
from app.models.customer_order import CustomerOrder
from app.models.order_item import OrderItem, OrderItemOption, OrderItemTopping
from app.api.v1.orders import service
from app.api.v1.orders.schemas import OrderType
from app.api.v1.cash.service import resolve_dine_in_table_order


def _assert_not_paid(db: Session, order: CustomerOrder, detail: str) -> None:
    if service.order_has_sale(db, order.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail)


def _assert_same_table(source: CustomerOrder, target: CustomerOrder) -> None:
    if (
        source.table_session_id != target.table_session_id
        or source.dining_table_id != target.dining_table_id
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "La orden destino no pertenece a la misma mesa"
        )


def _find_mergeable_item(
    target_order: CustomerOrder, product_variant_id: UUID,
    unit_price: Decimal, discounted_unit_price: Decimal | None, estado_cocina: str,
) -> OrderItem | None:
    """Clave de fusión (research.md §3): mismo producto+variante+precio
    (incluido el descontado, si aplica) y mismo `estado_cocina`."""
    for it in target_order.items:
        if (
            it.product_variant_id == product_variant_id
            and it.unit_price == unit_price
            and it.discounted_unit_price == discounted_unit_price
            and it.estado_cocina == estado_cocina
        ):
            return it
    return None


def _delete_order_if_empty(db: Session, order: CustomerOrder) -> bool:
    """FR-007/SC-002: toda orden que quede con 0 `OrderItem` se elimina."""
    if len(order.items) == 0:
        db.delete(order)
        return True
    return False


def _sync_status_from_items(order: CustomerOrder) -> None:
    """Bugfix (reportado tras implementar spec 164): una orden nueva o
    modificada por estas 3 operaciones no puede quedar con `status`
    desalineado de `estado_cocina` de sus líneas, porque nada más en el
    sistema lo corrige después -- `status` solo cambia con la acción
    explícita "Entregar" (`POST /orders/{id}/deliver`), y esa acción (y su
    botón en la terminal, `canDeliverOrder`) solo aparece cuando hay algún
    ítem `pendiente` que entregar. Una orden nueva creada con una línea YA
    `entregado` (p. ej. se dividió/movió un ítem que el mesero ya había
    marcado entregado) quedaba con `status='pendiente'` para siempre: sin
    ítems `pendiente` que mostrar el botón, y por lo tanto sin forma de
    liberar la mesa (`release_table` bloquea mientras quede alguna orden en
    `'pendiente'`/`'confirmado'`, sin importar el pago).

    Misma idea documentada en research.md §4 ("el status de la orden
    resultante se deriva de sus líneas... una orden con todas las líneas
    entregado ya se considera entregada") -- aquí se aplica explícitamente
    en vez de asumir que algo más lo hace. No toca `'cancelada'` (terminal)
    ni una orden sin ítems (la borra `_delete_order_if_empty`, no esto)."""
    if order.status == "cancelada":
        return
    items = [it for it in order.items if it.estado_cocina != "cancelado"]
    if not items:
        return
    if all(it.estado_cocina == "entregado" for it in items):
        order.status = "entregada"
    elif order.status == "entregada":
        # Caso simétrico: se movió/fusionó una línea todavía `pendiente`
        # hacia una orden que ya estaba `entregada` -- reabre el status para
        # que "Entregar" (idempotente, re-entregable) vuelva a aparecer.
        order.status = "pendiente"


def _new_sibling_order(db: Session, source_order: CustomerOrder) -> CustomerOrder:
    """Orden nueva en la misma mesa que `source_order` (research.md §4):
    mismo `table_session_id`/`dining_table_id`/`channel`/`order_type`, un
    `table_order_number` nuevo con la misma numeración por turno de caja que
    `create_order`/`submit_cart`, y `participant_id=None`."""
    cash_shift_id = table_order_number = None
    if source_order.order_type == OrderType.DINE_IN.value:
        cash_shift_id, table_order_number = resolve_dine_in_table_order(db)
    sibling = CustomerOrder(
        table_session_id=source_order.table_session_id,
        dining_table_id=source_order.dining_table_id,
        customer_name=source_order.customer_name,
        channel=source_order.channel,
        order_type=source_order.order_type,
        participant_id=None,
        status="pendiente",
        payment_status="UNPAID",
        cash_shift_id=cash_shift_id,
        table_order_number=table_order_number,
        promotion_evaluated_at=datetime.now(timezone.utc),
    )
    db.add(sibling)
    db.flush()
    return sibling


def _copy_item_to_order(
    db: Session, item: OrderItem, target_order: CustomerOrder, quantity: int,
) -> OrderItem:
    """Inserta una línea nueva en `target_order` con los campos de `item`
    copiados y `quantity` propia, duplicando sus opciones/toppings 1:1
    (data-model.md §OrderItemOption/OrderItemTopping) — usado tanto para una
    línea completa (`split_order`) como para una cantidad parcial (`move_item`/
    `split_item_to_new_order`, caso "no fusión")."""
    discounted_line_total = None
    if item.discounted_line_total is not None:
        ratio = Decimal(quantity) / Decimal(item.quantity)
        discounted_line_total = item.discounted_line_total * ratio
    new_item = OrderItem(
        order_id=target_order.id,
        participant_id=item.participant_id,
        product_variant_id=item.product_variant_id,
        quantity=quantity,
        unit_price=item.unit_price,
        addons_total=item.addons_total,
        discounted_unit_price=item.discounted_unit_price,
        discounted_line_total=discounted_line_total,
        combo_id=item.combo_id,
        estado_cocina=item.estado_cocina,
        void_de=item.void_de,
        notes=item.notes,
    )
    db.add(new_item)
    db.flush()
    for opt in item.options:
        db.add(OrderItemOption(
            order_item_id=new_item.id, option_id=opt.option_id, quantity=opt.quantity,
            per_line=opt.per_line, added_after_sale=opt.added_after_sale,
            complementary_sale_id=opt.complementary_sale_id,
        ))
    for top in item.toppings:
        db.add(OrderItemTopping(
            order_item_id=new_item.id, topping_product_variant_id=top.topping_product_variant_id,
            quantity=top.quantity, unit_price=top.unit_price,
            added_after_sale=top.added_after_sale, complementary_sale_id=top.complementary_sale_id,
        ))
    db.flush()
    return new_item


def _decrement_source_item(db: Session, item: OrderItem, moved_qty: int) -> None:
    """Resta `moved_qty` de la línea de origen; la elimina si queda en 0
    (data-model.md §OrderItem) en vez de dejarla con `quantity = 0`."""
    if moved_qty >= item.quantity:
        # Quitar de la colección ya cargada (no `db.delete(item)` directo):
        # mantiene `order.items` en memoria sincronizado con la fila borrada
        # (cascade "delete-orphan"), que es lo que lee `_delete_order_if_empty`
        # a continuación -- `db.delete(item)` deja la colección con la
        # referencia vieja hasta que algo la expira/recarga.
        item.order.items.remove(item)
        db.flush()
        return
    if item.discounted_line_total is not None:
        remaining_ratio = Decimal(item.quantity - moved_qty) / Decimal(item.quantity)
        item.discounted_line_total = item.discounted_line_total * remaining_ratio
    item.quantity -= moved_qty
    db.flush()


def _validate_quantity_range(quantity: int | None, line_quantity: int) -> None:
    """Edge Cases: `0 < cantidad_movida < cantidad_total_de_la_línea` cuando se
    fracciona; mover la totalidad (`quantity is None` o `quantity ==
    line_quantity`) siempre es válido."""
    if quantity is not None and quantity != line_quantity and not (0 < quantity < line_quantity):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "La cantidad a mover debe ser mayor a 0 y menor a la cantidad de la línea",
        )


def _get_order_item(order: CustomerOrder, item_id: UUID) -> OrderItem:
    item = next((it for it in order.items if it.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    return item


def split_order(db: Session, order_id: UUID) -> dict:
    """User Story 1 (FR-001, FR-002, FR-003): convierte cada línea de la orden
    en una cuenta independiente y elimina la original. No-op con 1 sola línea."""
    order = get_or_404(db, CustomerOrder, order_id, "Order not found")
    _assert_not_paid(db, order, "La orden ya fue cobrada; no se puede dividir.")

    items = list(order.items)
    if len(items) <= 1:
        return {"original_order_id": order.id, "created_orders": []}

    created: list[dict] = []
    for item in items:
        sibling = _new_sibling_order(db, order)
        new_item = _copy_item_to_order(db, item, sibling, item.quantity)
        _sync_status_from_items(sibling)
        created.append({
            "order_id": sibling.id,
            "table_order_number": sibling.table_order_number,
            "item": {
                "product_variant_id": new_item.product_variant_id,
                "quantity": new_item.quantity,
                "unit_price": new_item.unit_price,
            },
        })
        order.items.remove(item)
    db.flush()
    db.delete(order)
    db.commit()
    return {"original_order_id": order_id, "created_orders": created}


def move_item(
    db: Session, order_id: UUID, item_id: UUID, target_order_id: UUID, quantity: int | None = None,
) -> dict:
    """User Story 2 (FR-004, FR-005): mueve una línea (completa o parcial)
    hacia una orden destino existente de la misma mesa, fusionando con una
    línea compatible si existe."""
    order = get_or_404(db, CustomerOrder, order_id, "Order not found")
    item = _get_order_item(order, item_id)

    if target_order_id == order_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No se puede mover un ítem a la misma orden.")
    target_order = get_or_404(db, CustomerOrder, target_order_id, "Target order not found")

    _assert_same_table(order, target_order)
    detail = "La orden ya fue cobrada; no se puede mover el ítem."
    _assert_not_paid(db, order, detail)
    _assert_not_paid(db, target_order, detail)
    _validate_quantity_range(quantity, item.quantity)

    moved_qty = item.quantity if quantity is None else quantity
    merge_target = _find_mergeable_item(
        target_order, item.product_variant_id, item.unit_price, item.discounted_unit_price,
        item.estado_cocina,
    )
    merged = merge_target is not None
    if merged:
        merge_target.quantity += moved_qty
        if merge_target.discounted_unit_price is not None:
            merge_target.discounted_line_total = merge_target.discounted_unit_price * merge_target.quantity
        db.flush()
    else:
        _copy_item_to_order(db, item, target_order, moved_qty)
    _sync_status_from_items(target_order)

    _decrement_source_item(db, item, moved_qty)

    source_deleted = _delete_order_if_empty(db, order)
    if not source_deleted:
        _sync_status_from_items(order)
    db.commit()

    source_total = Decimal("0") if source_deleted else service.build_billing_summary(db, order).total
    target_total = service.build_billing_summary(db, target_order).total

    return {
        "source_order": {"order_id": order_id, "deleted": source_deleted, "total": source_total},
        "target_order": {"order_id": target_order_id, "deleted": False, "total": target_total},
        "merged": merged,
    }


def split_item_to_new_order(
    db: Session, order_id: UUID, item_id: UUID, quantity: int | None = None,
) -> dict:
    """User Story 3 (FR-006, FR-007): crea una orden nueva en la misma mesa
    que contiene únicamente el ítem (o la cantidad indicada)."""
    order = get_or_404(db, CustomerOrder, order_id, "Order not found")
    item = _get_order_item(order, item_id)

    _assert_not_paid(db, order, "La orden ya fue cobrada; no se puede crear una cuenta nueva.")
    _validate_quantity_range(quantity, item.quantity)

    moved_qty = item.quantity if quantity is None else quantity

    # Edge Cases: bloqueado solo cuando se mueve la TOTALIDAD de la ÚNICA
    # línea restante (no genera ningún cambio real); una cantidad parcial de
    # esa misma línea sí está permitida.
    if len(order.items) == 1 and moved_qty == item.quantity:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Esta acción no genera ningún cambio: el ítem ya es una cuenta independiente.",
        )

    new_order = _new_sibling_order(db, order)
    _copy_item_to_order(db, item, new_order, moved_qty)
    _sync_status_from_items(new_order)
    _decrement_source_item(db, item, moved_qty)

    source_deleted = _delete_order_if_empty(db, order)
    if not source_deleted:
        _sync_status_from_items(order)
    db.commit()

    source_total = Decimal("0") if source_deleted else service.build_billing_summary(db, order).total
    new_total = service.build_billing_summary(db, new_order).total

    return {
        "source_order": {"order_id": order_id, "deleted": source_deleted, "total": source_total},
        "new_order": {
            "order_id": new_order.id,
            "table_order_number": new_order.table_order_number,
            "total": new_total,
        },
    }
