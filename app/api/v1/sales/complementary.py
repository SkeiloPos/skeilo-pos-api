"""Cobro del saldo pendiente generado por adiciones agregadas después de que
un pedido ya tenía una `Sale` emitida (spec 123).

Atómico: si el tenant tiene el módulo de inventario y falta stock de
cualquier insumo cubierto, se aborta completo (research.md D9) — ninguna
`Sale` complementaria se genera y ningún insumo se descuenta, ni el que sí
tenía stock suficiente."""
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.crud import get_or_404
from app.core.models import Tenant, User
from app.core.plan_limits import has_module_access
from app.catalog_engine import ChosenOption
from app.models.customer_order import CustomerOrder
from app.models.option import Option
from app.models.order_item import OrderItem, OrderItemOption, OrderItemTopping
from app.models.sale import Sale
from app.api.v1.inventory.stock import lock_items
from app.api.v1.orders.consumption import (
    check_addition_availability, deduct_addition, plan_addition_consumption,
)
from app.api.v1.catalog.consumption_plan import plan_line_consumption
from app.api.v1.sales.builder import SaleLine, build_sale, ensure_open_shift
from app.api.v1.sales.schemas import ComplementaryPaymentIn


def _pending_option_rows(db: Session, order_id: UUID) -> list[OrderItemOption]:
    return db.execute(
        select(OrderItemOption)
        .join(OrderItem, OrderItem.id == OrderItemOption.order_item_id)
        .options(
            selectinload(OrderItemOption.order_item),
        )
        .where(
            OrderItem.order_id == order_id,
            OrderItem.estado_cocina != "anulado",
            OrderItemOption.added_after_sale.is_(True),
            OrderItemOption.complementary_sale_id.is_(None),
        )
    ).scalars().all()


def _pending_topping_rows(db: Session, order_id: UUID) -> list[OrderItemTopping]:
    """spec 129: adiciones (productos-topping) agregadas después de la `Sale`,
    mismo filtro que `_pending_option_rows` -- ver `order_pending_balance`."""
    return db.execute(
        select(OrderItemTopping)
        .join(OrderItem, OrderItem.id == OrderItemTopping.order_item_id)
        .options(selectinload(OrderItemTopping.order_item))
        .where(
            OrderItem.order_id == order_id,
            OrderItem.estado_cocina != "anulado",
            OrderItemTopping.added_after_sale.is_(True),
            OrderItemTopping.complementary_sale_id.is_(None),
        )
    ).scalars().all()


def settle_pending_balance(
    db: Session, order_id: UUID, data: ComplementaryPaymentIn, cashier: User, tenant: Tenant,
) -> Sale:
    order = get_or_404(db, CustomerOrder, order_id, "Order not found")
    option_rows = _pending_option_rows(db, order.id)
    topping_rows = _pending_topping_rows(db, order.id)
    if not option_rows and not topping_rows:
        raise HTTPException(status.HTTP_409_CONFLICT, "No hay saldo pendiente por cobrar")

    options = {
        o.id: o for o in db.execute(
            select(Option).where(Option.id.in_({r.option_id for r in option_rows}))
        ).scalars().all()
    }
    pending_total = sum(
        (Decimal(options[r.option_id].extra_price) * r.quantity for r in option_rows), Decimal("0")
    ) + sum(
        (Decimal(r.unit_price) * r.quantity for r in topping_rows), Decimal("0")
    )

    if len(data.payments) != 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "El pago complementario exige un único método de pago",
        )
    amount = Decimal(data.payments[0].amount)
    if amount != pending_total:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"El monto ({amount}) no coincide con el saldo pendiente ({pending_total})",
        )

    shift = ensure_open_shift(db, data.cash_shift_id)
    has_inventory = has_module_access(db, tenant, "inventario")

    chosen_by_row: dict[UUID, ChosenOption] = {
        r.id: ChosenOption(options[r.option_id], r.quantity, r.per_line) for r in option_rows
    }

    try:
        if has_inventory:
            # Lock en orden canónico de TODOS los insumos cubiertos antes de
            # descontar ninguno (research.md D9) -- mismo motivo que
            # `lock_consumption`: evita deadlock entre dos cobros
            # concurrentes que toquen los mismos insumos en distinto orden.
            needed: set[UUID] = set()
            for r in option_rows:
                needed |= {
                    line.inventory_item_id
                    for line in plan_addition_consumption(db, r.order_item, chosen_by_row[r.id])
                }
            # spec 129: la receta de la adición es la de su PROPIA variante
            # principal (research.md D5), no la del order_item al que está adjunta.
            for r in topping_rows:
                needed |= {
                    line.inventory_item_id
                    for line in plan_line_consumption(db, r.topping_product_variant_id, r.quantity, [])
                }
            lock_items(db, needed)
            for r in option_rows:
                check_addition_availability(
                    db, r.order_item, chosen_by_row[r.id], extra_context="cobro de saldo pendiente",
                )
            for r in topping_rows:
                _check_topping_availability(db, r, extra_context="cobro de saldo pendiente")

        lines = [
            SaleLine(
                product_variant_id=r.order_item.product_variant_id,
                description=f"Adición: {options[r.option_id].name}",
                options=[{
                    "option_id": str(r.option_id),
                    "name": options[r.option_id].name,
                    "extra_price": str(options[r.option_id].extra_price),
                }],
                quantity=r.quantity,
                unit_price=options[r.option_id].extra_price,
                line_id=r.order_item_id,
            )
            for r in option_rows
        ] + [
            SaleLine(
                # spec 129: la adición es su propio producto -- la línea de venta
                # nombra SU variante, no la del order_item al que está adjunta.
                product_variant_id=r.topping_product_variant_id,
                description=f"Adición: {r.name}",
                options=[],
                quantity=r.quantity,
                unit_price=r.unit_price,
                line_id=r.order_item_id,
            )
            for r in topping_rows
        ]

        sale = build_sale(
            db,
            lines=lines,
            shift=shift,
            cashier=cashier,
            payments=data.payments,
            customer_name=order.customer_name,
            dining_table_id=order.dining_table_id,
            table_session_id=order.table_session_id,
            customer_order_id=order.id,
            kind="complementario",
        )

        if has_inventory:
            for r in option_rows:
                deduct_addition(db, r.order_item, chosen_by_row[r.id], cashier.id, reference_id=order.id)
            for r in topping_rows:
                _deduct_topping(db, r, cashier.id, reference_id=order.id)

        for r in option_rows:
            r.complementary_sale_id = sale.id
        for r in topping_rows:
            r.complementary_sale_id = sale.id

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

    return db.execute(
        select(Sale)
        .options(selectinload(Sale.items), selectinload(Sale.payments))
        .where(Sale.id == sale.id)
    ).scalar_one()


def _check_topping_availability(
    db: Session, row: OrderItemTopping, *, extra_context: str = ""
) -> None:
    from collections import defaultdict
    from app.api.v1.catalog.line_pricing import check_availability
    required: dict[UUID, Decimal] = defaultdict(Decimal)
    for line in plan_line_consumption(db, row.topping_product_variant_id, row.quantity, []):
        required[line.inventory_item_id] += line.quantity
    check_availability(db, dict(required), extra_context=extra_context)


def _deduct_topping(db: Session, row: OrderItemTopping, user_id, *, reference_id: UUID) -> None:
    from app.core import inventory_reasons as reasons
    from app.api.v1.inventory.stock import record_movement
    for line in plan_line_consumption(db, row.topping_product_variant_id, row.quantity, []):
        record_movement(
            db, line.inventory_item_id, type="out", quantity=line.quantity,
            reason=reasons.VENTA, reference_type=reasons.REF_ORDER,
            reference_id=reference_id, user_id=user_id,
        )
