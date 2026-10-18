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
from app.models.order_item import OrderItem, OrderItemOption
from app.models.sale import Sale
from app.api.v1.inventory.stock import lock_items
from app.api.v1.orders.consumption import (
    check_addition_availability, deduct_addition, plan_addition_consumption,
)
from app.api.v1.sales.builder import SaleLine, build_sale, ensure_open_shift
from app.api.v1.sales.schemas import ComplementaryPaymentIn


def _pending_rows(db: Session, order_id: UUID) -> list[OrderItemOption]:
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


def settle_pending_balance(
    db: Session, order_id: UUID, data: ComplementaryPaymentIn, cashier: User, tenant: Tenant,
) -> Sale:
    order = get_or_404(db, CustomerOrder, order_id, "Order not found")
    rows = _pending_rows(db, order.id)
    if not rows:
        raise HTTPException(status.HTTP_409_CONFLICT, "No hay saldo pendiente por cobrar")

    options = {
        o.id: o for o in db.execute(
            select(Option).where(Option.id.in_({r.option_id for r in rows}))
        ).scalars().all()
    }
    pending_total = sum(
        (Decimal(options[r.option_id].extra_price) * r.quantity for r in rows), Decimal("0")
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
        r.id: ChosenOption(options[r.option_id], r.quantity, r.per_line) for r in rows
    }

    try:
        if has_inventory:
            # Lock en orden canónico de TODOS los insumos cubiertos antes de
            # descontar ninguno (research.md D9) -- mismo motivo que
            # `lock_consumption`: evita deadlock entre dos cobros
            # concurrentes que toquen los mismos insumos en distinto orden.
            needed: set[UUID] = set()
            for r in rows:
                needed |= {
                    line.inventory_item_id
                    for line in plan_addition_consumption(db, r.order_item, chosen_by_row[r.id])
                }
            lock_items(db, needed)
            for r in rows:
                check_addition_availability(
                    db, r.order_item, chosen_by_row[r.id], extra_context="cobro de saldo pendiente",
                )

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
            for r in rows
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
            for r in rows:
                deduct_addition(db, r.order_item, chosen_by_row[r.id], cashier.id, reference_id=order.id)

        for r in rows:
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
