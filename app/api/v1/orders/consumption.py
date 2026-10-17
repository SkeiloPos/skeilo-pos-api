"""Deducción de inventario al consolidar (Fase 4) — el pedido entra a cocina y
el stock se compromete. Espeja `deduct_sale` (sales/consumption.py) pero lee las
opciones de forma relacional (`OrderItemOption`/`Option`), no del JSONB de ventas.

El descuento es por ítem insertado (no snapshot al final), reutilizable por la
adición de un solo ítem de Fase 5.

Qué consume cada línea lo decide `catalog/consumption_plan.py`, compartido con la
venta de mostrador: aquí solo se elige la dirección del movimiento ('out' al
descontar, 'in' al revertir)."""
from collections import defaultdict
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import inventory_reasons as reasons
from fastapi import HTTPException

from app.catalog_engine import ChosenOption, sold_out_detail
from app.core.exceptions import InsufficientStockError
from app.models.option import Option
from app.models.order_item import OrderItem
from app.api.v1.inventory.stock import lock_items, record_movement
from app.api.v1.catalog.consumption_plan import (
    ConsumptionLine,
    ensure_lines_consume_inventory,
    plan_line_consumption,
    required_consumption,
)
from app.api.v1.catalog.line_pricing import check_availability


def chosen_from_rows(db: Session, item: OrderItem) -> list[ChosenOption]:
    """Reconstruye las `ChosenOption` de una línea **leyendo la marca `per_line` de cada
    fila** `OrderItemOption` (spec 089, A-94; research D4).

    Es el único constructor de opciones desde filas: descuento, reversa, anulación y
    cobro pasan por aquí, así siempre aplican la regla con la que se creó la línea, aunque
    el administrador cambie después el `pricing_type` del grupo (deduct == reverse)."""
    rows = {o.option_id: o for o in item.options}
    if not rows:
        return []
    options = db.execute(select(Option).where(Option.id.in_(rows.keys()))).scalars().all()
    return [ChosenOption(opt, rows[opt.id].quantity, rows[opt.id].per_line) for opt in options]


def ensure_consumes_inventory(
    db: Session, entries: list[tuple[OrderItem, list[ChosenOption]]]
) -> None:
    """Rechaza las líneas que no descontarían **nada**. Adaptador de
    `ensure_lines_consume_inventory` a las líneas de orden; la venta de mostrador usa
    el mismo núcleo desde `sales/consumption.py`."""
    ensure_lines_consume_inventory(
        db, [(item.product_variant_id, item.quantity, options) for item, options in entries]
    )


def lock_consumption(
    db: Session, entries: list[tuple[OrderItem, list[ChosenOption]]]
) -> None:
    """Pre-bloquea, en orden canónico de id, todos los insumos que consumirán las
    líneas dadas. Debe llamarse **antes** de la primera mutación de stock para que
    los locks de la transacción se tomen siempre en el mismo orden (ver
    `stock.lock_items`)."""
    needed: set[UUID] = set()
    for item, options in entries:
        needed |= set(
            required_consumption(db, item.product_variant_id, item.quantity, options)
        )
    lock_items(db, needed)


def deduct_order_items(
    db: Session,
    entries: list[tuple[OrderItem, list[ChosenOption]]],
    user_id: UUID | None,
    reference_id: UUID,
) -> None:
    """Descuenta el inventario de varias líneas de una vez, tomando los locks en
    orden fijo primero. Es la vía preferida cuando se insertan N líneas en la
    misma transacción (consolidación, confirmación de pedido).

    Rechaza el lote entero si alguna línea no consumiría nada: mejor parar y
    configurar la receta que emitir una venta que descuadra el inventario."""
    ensure_consumes_inventory(db, entries)
    lock_consumption(db, entries)
    for item, options in entries:
        deduct_order_item(db, item, options, user_id, reference_id)


def deduct_order_items_naming_product(
    db: Session,
    entries: list[tuple[OrderItem, list[ChosenOption]]],
    user_id: UUID | None,
    reference_id: UUID,
) -> None:
    """Igual que `deduct_order_items`, pero si un insumo se agota responde con un `detail` que
    nombra el PRODUCTO (spec 089, A-96, FR-029; research D18) en vez de solo el insumo. Es lo que
    usan los tres caminos donde el cajero agrega líneas a una orden (crear la orden, agregar un
    ítem, reemplazar una línea): así el mensaje dice qué producto quitar. El status se conserva
    (400) y, como siempre, el `except HTTPException` del llamador hace rollback total."""
    ensure_consumes_inventory(db, entries)
    lock_consumption(db, entries)
    for item, options in entries:
        try:
            deduct_order_item(db, item, options, user_id, reference_id)
        except InsufficientStockError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=sold_out_detail(db, item.product_variant_id, exc),
            ) from exc


def reverse_order_items(
    db: Session,
    entries: list[tuple[OrderItem, list[ChosenOption]]],
    user_id: UUID | None,
    reference_id: UUID,
) -> None:
    """Reversa en bloque (cancelación de orden). Aunque los 'in' nunca fallan por
    stock, siguen tomando locks: se piden en orden fijo por la misma razón que en
    `deduct_order_items`."""
    lock_consumption(db, entries)
    for item, options in entries:
        reverse_order_item(db, item, options, user_id, reference_id)


def deduct_order_item(
    db: Session,
    order_item: OrderItem,
    options: list[ChosenOption],
    user_id: UUID | None,
    reference_id: UUID,
) -> None:
    """Descuenta el inventario que consume una línea de orden (ver
    `plan_line_consumption`: receta fija + slots resueltos por las opciones elegidas).
    Escribe kardex 'out' con lock; `InsufficientStockError` (400) propaga y fuerza
    rollback."""
    for line in plan_line_consumption(
        db, order_item.product_variant_id, order_item.quantity, options
    ):
        record_movement(
            db, line.inventory_item_id, type="out", quantity=line.quantity,
            reason=reasons.VENTA, reference_type=reasons.REF_ORDER,
            reference_id=reference_id, user_id=user_id,
        )


def plan_addition_consumption(
    db: Session, order_item: OrderItem, chosen_option: ChosenOption,
) -> list[ConsumptionLine]:
    """Qué consume UNA adición agregada después de que la línea ya existe (spec 123).

    A diferencia de `plan_line_consumption`, nunca incluye la receta base de la
    variante (`source == "receta"`) — esa ya se descontó cuando el `OrderItem` se
    creó/consolidó. `plan_line_consumption` siempre la agrega multiplicada por
    `order_item.quantity`; llamar a `deduct_order_item`/`deduct_order_items`
    "acotado a la nueva opción" la volvería a descontar (research.md D9 de la
    spec 123) — por eso esta función filtra esas líneas antes de devolver nada."""
    return [
        line for line in plan_line_consumption(
            db, order_item.product_variant_id, order_item.quantity, [chosen_option]
        )
        if line.source != "receta"
    ]


def check_addition_availability(
    db: Session, order_item: OrderItem, chosen_option: ChosenOption, *, extra_context: str = "",
) -> None:
    """Chequeo preventivo (sin lock) de UNA adición post-hoc — mismo filtro que
    `plan_addition_consumption`, para no exigir de más contando la receta base."""
    required: dict[UUID, Decimal] = defaultdict(Decimal)
    for line in plan_addition_consumption(db, order_item, chosen_option):
        required[line.inventory_item_id] += line.quantity
    check_availability(db, dict(required), extra_context=extra_context)


def deduct_addition(
    db: Session,
    order_item: OrderItem,
    chosen_option: ChosenOption,
    user_id: UUID | None,
    reference_id: UUID,
) -> None:
    """Descuenta solo el insumo de UNA adición agregada después de que la línea ya
    existe (spec 123) — nunca la receta base de la variante (ver
    `plan_addition_consumption`). Usada por `orders/additions.py` (pedido sin pagar)
    y `sales/complementary.py` (cobro del pago complementario) — ninguno de los dos
    llama a `deduct_order_item`/`deduct_order_items` directamente."""
    for line in plan_addition_consumption(db, order_item, chosen_option):
        record_movement(
            db, line.inventory_item_id, type="out", quantity=line.quantity,
            reason=reasons.VENTA, reference_type=reasons.REF_ORDER,
            reference_id=reference_id, user_id=user_id,
        )


def reverse_addition(
    db: Session,
    order_item: OrderItem,
    chosen_option: ChosenOption,
    user_id: UUID | None,
    reference_id: UUID,
) -> None:
    """Inverso de `deduct_addition`: devuelve al stock la porción de UNA adición que se
    le quitó a la línea al bajarle la cantidad (spec 123, `orders/additions.py`) —
    nunca la receta base. Movimiento 'in' con `reason=AJUSTE` (mismo criterio que
    `reverse_order_item`): no es una venta, es la corrección de lo que ya se había
    descontado de más; nunca falla por stock insuficiente."""
    for line in plan_addition_consumption(db, order_item, chosen_option):
        record_movement(
            db, line.inventory_item_id, type="in", quantity=line.quantity,
            reason=reasons.AJUSTE, reference_type=reasons.REF_ORDER,
            reference_id=reference_id, user_id=user_id,
        )


def reverse_order_item(
    db: Session,
    order_item: OrderItem,
    options: list[ChosenOption],
    user_id: UUID | None,
    reference_id: UUID,
) -> None:
    """Inverso exacto de `deduct_order_item`: devuelve al stock lo que consumía una
    línea. Movimientos 'in' (siempre suman, no lanzan InsufficientStockError). Se usa
    al anular un ítem 'pendiente' (cocina no lo consumió) y en la cancelación de
    orden (Fase 7).

    Comparte `plan_line_consumption` con el descuento a propósito: una reversa que
    calcule distinto descuadra el inventario para siempre, porque nadie concilia los
    'in' contra los 'out'."""
    for line in plan_line_consumption(
        db, order_item.product_variant_id, order_item.quantity, options
    ):
        record_movement(
            db, line.inventory_item_id, type="in", quantity=line.quantity,
            reason=reasons.AJUSTE, reference_type=reasons.REF_ORDER_VOID,
            reference_id=reference_id, user_id=user_id,
        )
