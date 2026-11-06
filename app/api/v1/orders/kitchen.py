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
from app.core.domain_errors import ConflictError
from app.core.models import Tenant, User
from app.core import events
from app.core.plan_limits import has_module_access
from app.core.timezone import utc_now_naive
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
from app.models.dining_table import DiningTable
from app.models.product import Product

logger = logging.getLogger(__name__)

#: `order_type` → `origin` de la tarjeta del tablero (contracts/kitchen-board.md,
#: data-model.md §CustomerOrder). `None`/cualquier otro valor cae en `"caja"`
#: (mostrador), el mismo origen que un pedido de mostrador sin `order_type`
#: explícito.
_KITCHEN_CARD_ORIGIN_BY_ORDER_TYPE = {
    "DINE_IN": "mesa",
    "TAKEAWAY": "caja",
    "DELIVERY": "domicilio",
}


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


def deliver_order(
    db: Session, order_id: UUID, tenant_id: int | None = None, *, tenant: Tenant | None = None,
) -> CustomerOrder:
    """Entregar un pedido completo con una sola acción, idempotente (spec 147,
    FR-001/FR-002a/FR-011, research.md R3).

    `SELECT ... FOR UPDATE` serializa dos entregas casi simultáneas sobre el
    mismo pedido (mismo patrón que `checkout.py` para `CustomerOrder`): ambas
    responden éxito, un único cambio de estado efectivo (FR-011/SC-006).

    - `status == 'cancelada'` → rechazo, transición inválida (FR-010).
    - Spec 153 (FR-004/FR-006/FR-009): si el tenant tiene el módulo de Cocina
      activo y el pedido es de mesa (`dining_table_id` no nulo), exige
      `kitchen_status == 'LISTO'` → 409 `ORDER_NOT_READY` si no lo está. Sin
      módulo activo, o sin mesa, no se añade ninguna validación nueva --
      mismo comportamiento que antes de esta spec.
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
    if (
        tenant is not None
        and order.dining_table_id is not None
        and order.kitchen_status != "LISTO"
        and has_module_access(db, tenant, "cocina")
    ):
        raise ConflictError(
            "El pedido aún no está listo en cocina.", code="ORDER_NOT_READY",
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


def _apply_kitchen_send(order: CustomerOrder) -> bool:
    """Transición compartida por `send_to_kitchen` (acción manual) y el envío
    automático tras cobro de sesión de mesa (spec 153, FR-001/FR-002,
    `table_sessions/service._charge_session`): `kitchen_status: NULL →
    'SIN_PREPARAR'` + `sent_to_kitchen_at`, solo si `status != 'cancelada'`,
    `kitchen_status IS NULL` (idempotencia) y `payment_status == 'PAID'` --
    las mismas tres condiciones que ya valida `send_to_kitchen` una por una
    para devolver el código de error correspondiente. No hace commit ni
    emite evento: responsabilidad del caller. Devuelve `True` si transicionó."""
    if (
        order.status == "cancelada"
        or order.kitchen_status is not None
        or order.payment_status != "PAID"
    ):
        return False
    order.kitchen_status = "SIN_PREPARAR"
    order.sent_to_kitchen_at = utc_now_naive()
    return True


def send_to_kitchen(db: Session, order_id: UUID, tenant_id: int | None = None) -> CustomerOrder:
    """Envía un pedido confirmado y pagado a la cola de preparación de cocina
    (spec 150, FR-001/FR-003/FR-004). `SELECT ... FOR UPDATE` serializa dos
    envíos casi simultáneos sobre el mismo pedido (doble clic o reintento de
    red, FR-008/SC-003): la segunda transacción ve `kitchen_status` ya
    escrito por la primera tan pronto como adquiere el lock, y cae en
    `ALREADY_IN_KITCHEN`.

    Orden determinístico de validaciones (data-model.md, research.md
    Decisión 6):
    1. Pedido no existe → 404.
    2. `status == 'cancelada'` (research.md Decisión 0: "orden confirmada" es
       cualquier pedido vivo) → 409 `ORDER_NOT_CONFIRMED`.
    3. `kitchen_status IS NOT NULL` → 409 `ALREADY_IN_KITCHEN`.
    4. `payment_status != 'PAID'` → 409 `ORDER_NOT_PAID`.
    5. Éxito: `kitchen_status = 'SIN_PREPARAR'`, `sent_to_kitchen_at` con la
       hora del servidor, commit, emite `events.order_sent_to_kitchen`
       post-commit."""
    order = db.execute(
        select(CustomerOrder)
        .where(CustomerOrder.id == order_id)
        .with_for_update(of=CustomerOrder)
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    if order.status == "cancelada":
        raise ConflictError(
            "El pedido no está en un estado válido para enviarse a cocina.",
            code="ORDER_NOT_CONFIRMED",
        )
    if order.kitchen_status is not None:
        raise ConflictError("El pedido ya está en la cola de cocina.", code="ALREADY_IN_KITCHEN")
    if order.payment_status != "PAID":
        raise ConflictError("El pedido no está pagado.", code="ORDER_NOT_PAID")

    _apply_kitchen_send(order)
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Error enviando el pedido a cocina")
        raise
    db.refresh(order)

    events.order_sent_to_kitchen(
        tenant_id, order_id=order.id, table_session_id=order.table_session_id
    )

    return order


def _kitchen_card_product_names(db: Session, orders: list[CustomerOrder]) -> dict[UUID, str]:
    """Nombres de producto para las líneas de todas las órdenes del tablero,
    resueltos en un solo `SELECT` (sin N+1 por tarjeta, data-model.md §Vista
    del tablero)."""
    variant_ids = {it.product_variant_id for o in orders for it in o.items}
    if not variant_ids:
        return {}
    rows = db.execute(
        select(ProductVariant.id, Product.name)
        .join(Product, Product.id == ProductVariant.product_id)
        .where(ProductVariant.id.in_(variant_ids))
    ).all()
    return dict(rows)


def _kitchen_card_table_numbers(db: Session, orders: list[CustomerOrder]) -> dict[UUID, int]:
    table_ids = {o.dining_table_id for o in orders if o.dining_table_id is not None}
    if not table_ids:
        return {}
    rows = db.execute(
        select(DiningTable.id, DiningTable.number).where(DiningTable.id.in_(table_ids))
    ).all()
    return dict(rows)


def _serialize_kitchen_card(
    order: CustomerOrder, *, table_numbers: dict[UUID, int], product_names: dict[UUID, str]
) -> dict:
    """Arma el `CardResponse` de una orden (contracts/kitchen-board.md,
    FR-012). Un ítem `estado_cocina == 'cancelado'` (anulado) no se incluye --
    ya no hace parte de lo que cocina debe preparar."""
    origin = _KITCHEN_CARD_ORIGIN_BY_ORDER_TYPE.get(order.order_type, "caja")
    items = []
    for it in order.items:
        if it.estado_cocina == "cancelado":
            continue
        addons = [
            {"name": opt.name, "quantity": opt.quantity} for opt in it.options
        ] + [
            {"name": tp.name, "quantity": tp.quantity} for tp in it.toppings
        ]
        items.append({
            "product_name": product_names.get(it.product_variant_id, ""),
            "quantity": it.quantity,
            "addons": addons,
            "notes": it.notes,
        })
    return {
        "order_id": order.id,
        "table_order_number": order.table_order_number,
        "kitchen_status": order.kitchen_status,
        "version": order.version,
        "sent_to_kitchen_at": order.sent_to_kitchen_at,
        "origin": origin,
        "table_number": table_numbers.get(order.dining_table_id) if origin == "mesa" else None,
        "customer_name": order.customer_name,
        "delivery_address": order.delivery_address if origin == "domicilio" else None,
        "notes": order.notes,
        "items": items,
    }


def get_kitchen_board(db: Session) -> dict:
    """Lectura agregada del tablero de cocina (data-model.md §Vista del
    tablero, FR-001/FR-002/FR-003/FR-011): un solo `SELECT` filtrado por
    `kitchen_status IS NOT NULL AND status NOT IN ('cancelada', 'entregada')`,
    ordenado FIFO (`sent_to_kitchen_at ASC`, desempate `id ASC`), agrupado en
    Python en las tres columnas fijas."""
    orders = db.execute(
        select(CustomerOrder)
        .options(
            selectinload(CustomerOrder.items).selectinload(OrderItem.options),
            selectinload(CustomerOrder.items).selectinload(OrderItem.toppings),
        )
        .where(CustomerOrder.kitchen_status.isnot(None))
        .where(CustomerOrder.status.notin_(["cancelada", "entregada"]))
        .order_by(CustomerOrder.sent_to_kitchen_at.asc(), CustomerOrder.id.asc())
    ).scalars().all()

    table_numbers = _kitchen_card_table_numbers(db, orders)
    product_names = _kitchen_card_product_names(db, orders)

    columns: dict[str, list[dict]] = {"SIN_PREPARAR": [], "EN_PROGRESO": [], "LISTO": []}
    for order in orders:
        columns[order.kitchen_status].append(
            _serialize_kitchen_card(order, table_numbers=table_numbers, product_names=product_names)
        )
    return columns


def change_kitchen_status(
    db: Session, order_id: UUID, kitchen_status: str, version: int, tenant_id: int | None = None
) -> dict:
    """Mueve una tarjeta entre columnas del tablero de cocina (spec 151,
    contracts/move-kitchen-status.md). `SELECT ... FOR UPDATE` serializa dos
    movimientos casi simultáneos sobre la misma orden (FR-008), mismo patrón
    que `send_to_kitchen`/`receive_order`/`deliver_order`.

    Orden determinístico de validaciones (data-model.md §Reglas de
    transición):
    1. Pedido no existe → 404.
    2. `kitchen_status IS NULL` (nunca se envió a cocina) → 409
       `NOT_IN_KITCHEN`.
    3. `version` recibido != `order.version` vigente → 409
       `KITCHEN_STATUS_CONFLICT` (payload incluye el `kitchen_status`
       vigente).
    4. `payment_status != 'PAID'` → 409 `PAYMENT_NOT_PAID`.
    5. `status IN ('cancelada', 'entregada')` → 409 `ORDER_NOT_ACTIVE`.
    6. Éxito: `kitchen_status` actualizado, `version += 1` (sin tocar
       `sent_to_kitchen_at` -- FR-002), commit, emite
       `events.order_kitchen_status_changed` post-commit."""
    order = db.execute(
        select(CustomerOrder)
        .options(
            selectinload(CustomerOrder.items).selectinload(OrderItem.options),
            selectinload(CustomerOrder.items).selectinload(OrderItem.toppings),
        )
        .where(CustomerOrder.id == order_id)
        .with_for_update(of=CustomerOrder)
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    if order.kitchen_status is None:
        raise ConflictError(
            "El pedido no está en la cola de cocina.", code="NOT_IN_KITCHEN"
        )
    if order.version != version:
        raise ConflictError(
            "La orden ya cambió de estado.", code="KITCHEN_STATUS_CONFLICT",
            details={"kitchen_status": order.kitchen_status},
        )
    if order.payment_status != "PAID":
        raise ConflictError("El pedido no está pagado.", code="PAYMENT_NOT_PAID")
    if order.status in ("cancelada", "entregada"):
        raise ConflictError("El pedido ya no está activo.", code="ORDER_NOT_ACTIVE")

    order.kitchen_status = kitchen_status
    order.version += 1
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Error moviendo el pedido en el tablero de cocina")
        raise
    db.refresh(order)

    events.order_kitchen_status_changed(
        tenant_id, order_id=order.id, table_session_id=order.table_session_id,
        kitchen_status=order.kitchen_status, version=order.version,
    )

    return _serialize_kitchen_card(
        order,
        table_numbers=_kitchen_card_table_numbers(db, [order]),
        product_names=_kitchen_card_product_names(db, [order]),
    )


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
