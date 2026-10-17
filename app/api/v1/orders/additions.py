"""Agregar una adición a una línea de pedido ya persistida (spec 123).

Hasta esta spec no existía ningún camino para modificar un `OrderItem` ya
creado: `add_item_to_order`/`create_order`/`void_item` (reemplazo) siempre
insertan una fila nueva. Esta función es la primera que agrega una opción
sobre una fila existente, mientras su `estado_cocina` siga en curso —
funciona igual para un pedido sin pagar (recalcula normal, FR-005) que para
uno que ya tiene una `Sale` emitida (genera saldo pendiente, FR-006), sin
tocar nunca esa `Sale` (FR-007, Principio VII)."""
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crud import get_or_404
from app.core.models import Tenant, User
from app.core.plan_limits import has_module_access
from app.catalog_engine import mark_per_line
from app.models.customer_order import CustomerOrder
from app.models.order_item import EN_CURSO, OrderItem, OrderItemOption
from app.models.product_variant import ProductVariant
from app.catalog_engine import ChosenOption
from app.models.option import Option
from app.api.v1.catalog.line_pricing import load_valid_options, validate_option_selection
from app.api.v1.catalog.schemas import OptionSelectionIn, UpdateAdditionQuantityIn
from app.api.v1.orders import service
from app.api.v1.orders.consumption import (
    check_addition_availability, deduct_addition, reverse_addition,
)


def add_addition_to_order_item(
    db: Session, item_id: UUID, data: OptionSelectionIn, user: User, tenant: Tenant,
) -> CustomerOrder:
    item = get_or_404(db, OrderItem, item_id, "Order item not found")
    order = get_or_404(db, CustomerOrder, item.order_id, "Order not found")

    # Un pedido cancelado no anula el `estado_cocina` de sus ítems (bugfix
    # spec 050) -- el chequeo de abajo por sí solo no basta para rechazarlo.
    if order.status == "cancelada":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Este pedido está cancelado; no se le pueden agregar adiciones."
        )

    if item.estado_cocina not in EN_CURSO:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Producto ya preparado: agrega un producto nuevo"
        )

    existing = db.execute(
        select(OrderItemOption.id).where(
            OrderItemOption.order_item_id == item.id,
            OrderItemOption.option_id == data.option_id,
        )
    ).scalar()
    if existing is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Esa opción ya está en la línea; anula la línea para rehacerla.",
        )

    variant = get_or_404(db, ProductVariant, item.product_variant_id, "Variant not found")
    [chosen] = load_valid_options(db, [data], variant=variant, quantity=item.quantity)
    # spec 117 (A-126): nunca es el carrito QR -- `qr_addons_per_line=False`.
    [chosen] = mark_per_line(db, [chosen], qr_addons_per_line=False)

    if has_module_access(db, tenant, "inventario"):
        check_addition_availability(db, item, chosen, extra_context="agregar adición")

    added_after_sale = service.order_has_sale(db, order.id)

    db.add(OrderItemOption(
        order_item_id=item.id, option_id=chosen.option.id, quantity=chosen.quantity,
        per_line=chosen.per_line, added_after_sale=added_after_sale,
    ))

    # Misma fórmula incremental que `compute_unit_price`/`compute_addons_total`
    # (catalog_engine/core.py) aplicarían sobre el conjunto completo de
    # opciones -- ambas son sumas lineales, así que agregar una sola opción más
    # a la columna ya persistida es equivalente a recalcular desde cero.
    incremento = chosen.option.extra_price * chosen.quantity
    if chosen.per_line:
        item.addons_total = item.addons_total + incremento
    else:
        item.unit_price = item.unit_price + incremento

    if added_after_sale:
        # FR-012: la `Sale` ya emitida nunca se toca; el descuento de
        # inventario de esta adición se difiere al pago complementario.
        pass
    else:
        # FR-005: pedido sin pagar -- mismo punto de descuento que cualquier
        # línea nueva (`add_item_to_order`), no se difiere al cobro.
        if has_module_access(db, tenant, "inventario"):
            deduct_addition(db, item, chosen, user.id, reference_id=order.id)
        # `refresh_order_item_discounts` "nunca sobre un pedido ya cobrado"
        # (checkout.py, docstring) -- solo tiene sentido en este branch.
        from app.api.v1.orders.checkout import refresh_order_item_discounts
        refresh_order_item_discounts(db, order)

    db.commit()

    return db.execute(
        select(CustomerOrder)
        .where(CustomerOrder.id == order.id)
    ).scalar_one()


def update_addition_quantity(
    db: Session, item_id: UUID, option_id: UUID, data: UpdateAdditionQuantityIn,
    user: User, tenant: Tenant,
) -> CustomerOrder:
    """Sube o baja la cantidad de una adición que **ya** está en la línea (spec 123), en
    vez de obligar a anular la línea entera solo para ajustar un topping que el cajero ya
    agregó. Mismas guardas que `add_addition_to_order_item`, más dos propias: la opción
    debe existir ya en la línea (lo inverso del 422 de arriba) y la nueva cantidad no
    puede ser la misma que ya tiene -- `UpdateAdditionQuantityIn` (`ge=1`) ya impide
    bajarla a 0; quitarla del todo sigue yendo por anular la línea.

    Subir y bajar comparten la misma fórmula de precio (lineal en el delta, con signo),
    pero el inventario no es simétrico: subir valida el delta contra los topes del grupo y
    lo descuenta (`deduct_addition`); bajar nunca choca con un tope (ningún grupo
    "cantidad" exige mínimo) y solo devuelve al stock lo que el delta negativo representa
    (`reverse_addition`, nunca falla por stock insuficiente).

    No toca filas ya liquidadas por un pago complementario (`complementary_sale_id` no
    nulo): esa cantidad ya se cobró y descontó, y `sales/complementary.py` no tiene
    forma de facturar un ajuste parcial sobre una fila que ya cerró."""
    item = get_or_404(db, OrderItem, item_id, "Order item not found")
    order = get_or_404(db, CustomerOrder, item.order_id, "Order not found")

    if order.status == "cancelada":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Este pedido está cancelado; no se le pueden modificar adiciones.",
        )

    if item.estado_cocina not in EN_CURSO:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Producto ya preparado: agrega un producto nuevo"
        )

    existing = db.execute(
        select(OrderItemOption).where(
            OrderItemOption.order_item_id == item.id,
            OrderItemOption.option_id == option_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Esa opción no está en la línea; agrégala con «Agregar adición».",
        )
    if existing.complementary_sale_id is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Esa adición ya fue cobrada; anula la línea para rehacerla.",
        )
    if data.quantity == existing.quantity:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Esa adición ya tiene esa cantidad.",
        )

    delta = data.quantity - existing.quantity
    option = get_or_404(db, Option, existing.option_id, "Option not found")

    if delta > 0:
        variant = get_or_404(db, ProductVariant, item.product_variant_id, "Variant not found")
        # Misma validación de grupo que `add_addition_to_order_item` -- en escala del
        # delta, no de la cantidad ya persistida (ver docstring del módulo: cada llamada
        # valida solo lo que está agregando, igual que hoy hace una adición normal). Solo
        # aplica al subir: bajar nunca puede violar un tope máximo.
        load_valid_options(
            db, [OptionSelectionIn(option_id=option_id, quantity=delta)],
            variant=variant, quantity=item.quantity,
        )
        chosen = ChosenOption(option, delta, existing.per_line)
        if has_module_access(db, tenant, "inventario"):
            check_addition_availability(db, item, chosen, extra_context="editar adición")
    else:
        # Bajar la cantidad -- el delta negativo es lo que hay que devolver al stock,
        # nunca lo que hay que descontar.
        chosen = ChosenOption(option, -delta, existing.per_line)

    existing.quantity = data.quantity

    # Mismo cálculo incremental que `add_addition_to_order_item`, pero sobre el delta (con
    # signo) en vez de la cantidad entera: ambos son sumas lineales (ver su comentario).
    incremento = option.extra_price * delta
    if existing.per_line:
        item.addons_total = item.addons_total + incremento
    else:
        item.unit_price = item.unit_price + incremento

    if existing.added_after_sale:
        # FR-012: igual que al agregarla -- el ajuste de inventario de este delta se
        # difiere al pago complementario que todavía no se ha hecho.
        pass
    else:
        if has_module_access(db, tenant, "inventario"):
            if delta > 0:
                deduct_addition(db, item, chosen, user.id, reference_id=order.id)
            else:
                reverse_addition(db, item, chosen, user.id, reference_id=order.id)
        from app.api.v1.orders.checkout import refresh_order_item_discounts
        refresh_order_item_discounts(db, order)

    db.commit()

    return db.execute(
        select(CustomerOrder).where(CustomerOrder.id == order.id)
    ).scalar_one()


def remove_addition(
    db: Session, item_id: UUID, option_id: UUID, user: User, tenant: Tenant,
) -> CustomerOrder:
    """Quita del todo una opción de la línea (spec 123) -- lo que faltaba junto a
    agregar/editar cantidad: hasta ahora la única forma de deshacer una adición era
    anular la línea entera y rehacerla. Mismas guardas de estado que las otras dos.

    No es "bajar la cantidad a 0" (`UpdateAdditionQuantityIn` ya lo impide, `ge=1`):
    valida con `validate_option_selection` que la línea SIN esta opción siga siendo una
    selección válida para la variante -- un sabor obligatorio (`conteo`, `min_select>0`,
    o un grupo que descuenta inventario con `_exige_maximo`) no se puede quitar y dejar
    la línea sin flavor/por debajo del mínimo; un topping opcional (`cantidad`, sin
    mínimo posible) siempre se puede quitar."""
    item = get_or_404(db, OrderItem, item_id, "Order item not found")
    order = get_or_404(db, CustomerOrder, item.order_id, "Order not found")

    if order.status == "cancelada":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Este pedido está cancelado; no se le pueden quitar adiciones.",
        )

    if item.estado_cocina not in EN_CURSO:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Producto ya preparado: agrega un producto nuevo"
        )

    existing = db.execute(
        select(OrderItemOption).where(
            OrderItemOption.order_item_id == item.id,
            OrderItemOption.option_id == option_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Esa opción no está en la línea; agrégala con «Agregar adición».",
        )
    if existing.complementary_sale_id is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Esa adición ya fue cobrada; anula la línea para rehacerla.",
        )

    option = get_or_404(db, Option, existing.option_id, "Option not found")
    variant = get_or_404(db, ProductVariant, item.product_variant_id, "Variant not found")

    remaining_rows = [o for o in item.options if o.id != existing.id]
    remaining_options = {
        o.id: o for o in db.execute(
            select(Option).where(Option.id.in_({r.option_id for r in remaining_rows}))
        ).scalars().all()
    }
    remaining_chosen = [
        ChosenOption(remaining_options[r.option_id], r.quantity, r.per_line)
        for r in remaining_rows if r.option_id in remaining_options
    ]
    # Propaga tal cual el 422 de `validate_option_selection` (mismo texto/estructura
    # que ya usa `add_addition_to_order_item` al validar una selección).
    validate_option_selection(db, variant, remaining_chosen, quantity=item.quantity)

    if existing.per_line:
        item.addons_total = item.addons_total - option.extra_price * existing.quantity
    else:
        item.unit_price = item.unit_price - option.extra_price * existing.quantity

    if not existing.added_after_sale:
        # FR-005: igual que al agregarla -- solo hay algo que devolver si de verdad se
        # había descontado (nunca se difirió a un pago complementario).
        if has_module_access(db, tenant, "inventario"):
            chosen = ChosenOption(option, existing.quantity, existing.per_line)
            reverse_addition(db, item, chosen, user.id, reference_id=order.id)
        from app.api.v1.orders.checkout import refresh_order_item_discounts
        refresh_order_item_discounts(db, order)
    # `added_after_sale=True` sin liquidar: nunca se dedujo inventario (diferido) ni
    # tocó la `Sale` ya emitida (FR-007) -- quitarla aquí solo baja el saldo pendiente
    # (computado desde las filas, no hay nada más que revertir).

    db.delete(existing)
    db.commit()

    return db.execute(
        select(CustomerOrder).where(CustomerOrder.id == order.id)
    ).scalar_one()
