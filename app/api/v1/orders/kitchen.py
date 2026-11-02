"""Ciclo de vida del pedido/ítem en cocina (spec 147: acción única de entrega).

`estado_cocina` es independiente del `payment_status` del pedido. Un ítem no
tiene transición propia (FR-022): siempre sigue la de su pedido, con la única
excepción de un ítem agregado después de que el pedido ya está `entregada`
(FR-002a) — nace `pendiente` hasta la siguiente "Entregar". Editar un ítem ya
`entregado` no es UPDATE silencioso: se anula (`void`) y se crea uno nuevo con
`void_de`."""
import logging
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.crud import get_or_404
from app.core.models import User
from app.core import events
from app.catalog_engine import ChosenOption, compute_addons_total, compute_unit_price, mark_per_line
from app.models.customer_order import CustomerOrder
from app.models.order_item import OrderItem, OrderItemOption
from app.models.order_item_void_log import OrderItemVoidLog
from app.models.product_variant import ProductVariant
from app.api.v1.orders import service
from app.api.v1.orders.consumption import (
    chosen_from_rows, deduct_order_items_naming_product, reverse_order_items,
)
from app.api.v1.catalog.line_pricing import load_valid_options
from app.api.v1.orders.schemas import VoidItemIn

logger = logging.getLogger(__name__)


def receive_order(db: Session, order_id: UUID, tenant_id: int | None = None) -> CustomerOrder:
    """"Recibir" un pedido recién llegado (`pendiente` → `confirmado`), sin
    tocar cocina ni pago (A pedido del usuario: decide el cajero, paso previo
    a "Entregar" -- antes de esto el panel de cobro de la terminal ya se ve,
    pero `_assert_closable` (`table_sessions/service.py`) sigue rechazando
    cobrar mientras el pedido siga sin entregar).

    Deliberadamente DISTINTO de `orders.checkout.confirm_order` (`POST
    /orders/{id}/confirm`, preexistente): ese otro descuenta inventario y
    exige un intento de pago ya confirmado -- este no toca ninguno de los
    dos, es puramente informativo para el cajero.

    Idempotente: recibir un pedido ya `'confirmado'` o `'entregada'` es
    no-op exitoso. `'cancelada'` es la única transición inválida."""
    order = db.execute(
        select(CustomerOrder)
        .where(CustomerOrder.id == order_id)
        .with_for_update(of=CustomerOrder)
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    if order.status == "cancelada":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Transición inválida (status={order.status})"
        )
    if order.status != "pendiente":
        return order

    order.status = "confirmado"
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Error recibiendo el pedido")
        raise
    db.refresh(order)

    events.order_received(tenant_id, order_id=order.id, table_session_id=order.table_session_id)

    return order


def deliver_order(db: Session, order_id: UUID, tenant_id: int | None = None) -> CustomerOrder:
    """Entregar un pedido completo con una sola acción, idempotente (spec 147,
    FR-001/FR-002a/FR-011, research.md R3).

    `SELECT ... FOR UPDATE` serializa dos entregas casi simultáneas sobre el
    mismo pedido (mismo patrón que `checkout.py` para `CustomerOrder`): ambas
    responden éxito, un único cambio de estado efectivo (FR-011/SC-006).

    - `status == 'cancelada'` → rechazo, transición inválida (FR-010).
    - `status == 'entregada'` → no es error: promueve cualquier ítem
      `'pendiente'` (nacido después de la primera entrega, FR-002a) a
      `'entregado'`; si no había ninguno, no-op exitoso sin emitir evento.
    - `status in ('pendiente', 'confirmado')` → pasa a `'entregada'` y todos
      sus ítems `'pendiente'` a `'entregado'`; `payment_status` no se toca
      (FR-001/FR-003). A pedido del usuario: entregar ya no exige pasar antes
      por `confirm_order` -- "Recibir" es un paso informativo del cajero, no
      una validación que el backend imponga."""
    order = db.execute(
        select(CustomerOrder)
        .options(selectinload(CustomerOrder.items))
        .where(CustomerOrder.id == order_id)
        .with_for_update(of=CustomerOrder)
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    if order.status == "cancelada":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Transición inválida (status={order.status})"
        )

    cambiados = [it for it in order.items if it.estado_cocina == "pendiente"]
    was_pendiente = order.status in ("pendiente", "confirmado")

    if not cambiados and not was_pendiente:
        # Ya entregada, sin ítems nuevos que promover: no-op exitoso, sin
        # segundo evento (FR-002a/FR-011).
        return order

    for it in cambiados:
        it.estado_cocina = "entregado"
    order.status = "entregada"

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Error entregando el pedido")
        raise
    db.refresh(order)

    events.order_delivered(tenant_id, order_id=order.id, table_session_id=order.table_session_id)

    return order


def void_item(db: Session, item_id: UUID, data: VoidItemIn, user: User) -> CustomerOrder:
    item = db.execute(
        select(OrderItem)
        .options(selectinload(OrderItem.options))
        .where(OrderItem.id == item_id)
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order item not found")

    if item.estado_cocina == "cancelado":
        raise HTTPException(status.HTTP_409_CONFLICT, "El ítem ya está cancelado")

    order_id = item.order_id
    order_payment_status = db.execute(
        select(CustomerOrder.payment_status).where(CustomerOrder.id == order_id)
    ).scalar_one()
    # spec 029 (Historia 1, A-16), reescrito spec 147: un pedido ya cobrado se
    # asume entregado y ya no es anulable. `payment_status == "PAID"` es el
    # criterio equivalente de pedido cerrado (ya no existe `status == "pagada"`
    # — el pago es independiente del status); `order_has_sale` cubre además el
    # camino QR/mostrador legado por si `payment_status` no llegó a
    # actualizarse (D2/D3 de research.md).
    if order_payment_status == "PAID" or service.order_has_sale(db, order_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "El pedido ya fue pagado y no puede anularse"
        )

    was_pendiente = item.estado_cocina == "pendiente"

    # Validar el reemplazo ANTES de mutar (para un 422 limpio si aplica).
    repl_variant = None
    repl_options: list[ChosenOption] = []
    if data.replacement is not None:
        repl_variant = get_or_404(
            db, ProductVariant, data.replacement.product_variant_id, "Variant not found"
        )
        if not repl_variant.active:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"Variante inactiva: {repl_variant.id}"
            )
        repl_options = load_valid_options(
            db, data.replacement.options, variant=repl_variant, quantity=data.replacement.quantity
        )
        # spec 117 (A-126): nunca es el carrito QR -- `qr_addons_per_line=False`.
        repl_options = mark_per_line(db, repl_options, qr_addons_per_line=False)

    try:
        item.estado_cocina = "cancelado"

        if was_pendiente:
            # Cocina no consumió físicamente: se devuelve el inventario.
            reverse_order_items(
                db, [(item, chosen_from_rows(db, item))], user.id, reference_id=order_id
            )

        db.add(OrderItemVoidLog(
            order_item_id=item.id, motivo=data.motivo,
            user_id=user.id, user_name=user.name,
        ))

        if data.replacement is not None:
            new_item = OrderItem(
                order_id=order_id,
                participant_id=item.participant_id,
                product_variant_id=repl_variant.id,
                quantity=data.replacement.quantity,
                unit_price=compute_unit_price(repl_variant, repl_options),
                addons_total=compute_addons_total(repl_options),
                notes=data.replacement.notes,
                estado_cocina="pendiente",
                void_de=item.id,
            )
            db.add(new_item)
            db.flush()
            for chosen in repl_options:
                db.add(OrderItemOption(
                    order_item_id=new_item.id, option_id=chosen.option.id, quantity=chosen.quantity,
                    per_line=chosen.per_line,
                ))
            # Nuevo consumo (con lock; puede 400 y hacer rollback total).
            # spec 089 (A-96): un insumo agotado nombra el PRODUCTO, no solo el insumo.
            deduct_order_items_naming_product(
                db, [(new_item, repl_options)], user.id, reference_id=order_id
            )

        # spec 118 (research.md D3, FR-006): recalcula el descuento de las
        # líneas vigentes -- la(s) fila(s) restante(s) pierden el descuento si
        # ya no alcanzan el umbral (import diferido, research.md D2).
        from app.api.v1.orders.checkout import refresh_order_item_discounts
        refresh_order_item_discounts(db, item.order)

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Error anulando/reemplazando ítem")
        raise

    return db.execute(
        select(CustomerOrder)
        .options(
            selectinload(CustomerOrder.items).selectinload(OrderItem.options),
            selectinload(CustomerOrder.items).selectinload(OrderItem.toppings),
        )
        .where(CustomerOrder.id == order_id)
    ).scalar_one()
