"""Cobro y ciclo de cierre de una orden de mesa (Fase 7): bloqueo con lock
optimista + validación de cocina, cuenta/split por comensal, pago (crea Sale sin
re-descontar), cancelación con reversa **parcial**, y liberación de mesa.

La reversa de inventario al cancelar es asimétrica y depende del estado de cocina
de cada ítem: solo vuelve al stock lo que cocina no llegó a preparar. Ver
`cancel_order`."""
import logging
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from app.core.audit import record_audit
from app.core.crud import get_or_404
from app.core.models import Tenant, User
from app.core.order_audit import (
    ActorType,
    OrderAuditActor,
    OrderAuditEventType,
    hash_sensitive_or_none,
    record_order_audit_event,
)
from app.core.timezone import utc_now
from app.models.dining_table import DiningTable
from app.models.table_session import TableSession
from app.models.session_participant import SessionParticipant
from app.models.cart import Cart
from app.catalog_engine import (
    ChosenOption,
    compute_addons_total,
    compute_unit_price,
    load_valid_options,
    mark_per_line,
)
from app.catalog_engine.core import format_item_description
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.cash_shift import CashShift
from app.models.sale import Sale
from app.models.customer_order import CustomerOrder
from app.models.order_item import EN_CURSO, OrderItem
from app.models.order_cancel_log import OrderCancelLog
from app.models.order_payment_attempt import OrderPaymentAttempt
from app.models.payment import PaymentMethod
from app.api.v1.sales.builder import SaleLine, build_sale, compute_total, ensure_open_shift
from app.api.v1.sales.schemas import PaymentIn
from app.api.v1.orders.consumption import chosen_from_rows, deduct_order_items, reverse_order_items
from app.api.v1.orders.schemas import (
    BlockIn, CancelIn, CheckoutAndSendIn, PayIn,
    BillResponse, BillOrderLine, BillItemLine, BillSessionLine,
    CheckoutPreviewResponse, DraftPreviewIn,
)
from app.api.v1.promotions import service as promotions
from app.api.v1.orders.service import order_has_sale

logger = logging.getLogger(__name__)

TERMINAL = ("pagada", "cancelada")

# Estados en los que el insumo YA se combinó físicamente: cancelar no lo
# devuelve al stock, es pérdida. El 'out' de la confirmación ya representa esa
# pérdida, así que tampoco se escribe un movimiento extra (sería doble descuento).
_CONSUMED_KITCHEN = ("en_preparacion", "listo")

# Estados de orden en los que el inventario todavía NO se descontó: el descuento
# ocurre al confirmar. Cancelar desde aquí no genera ningún movimiento.
_NOT_DEDUCTED = ("recibida",)


# ------------------------------------------- Auditoría de orden (spec 074)
# Cada emisión ocurre SIEMPRE después del `commit` de su transición (FR-010) y
# nunca dentro del `try` que hace `rollback`: un evento no puede anunciar algo
# que la base de datos no llegó a registrar.

def _cajero_actor(user) -> OrderAuditActor:
    """Actor `cajero` a partir del `User` que cada función de cobro ya recibe.

    `getattr` para `role_name` porque la auditoría no puede romper a quien la
    invoca (FR-011) si el llamador pasa un objeto de usuario más simple."""
    return OrderAuditActor(
        type=ActorType.CAJERO,
        id=str(user.id) if user is not None else None,
        role=getattr(user, "role_name", None),
    )


def _tenant_of(user) -> int | None:
    """`Tenant.id` del usuario que ejecuta la acción (FR-004) — explícito,
    nunca inferido dentro del helper de auditoría."""
    return getattr(user, "tenant_id", None)


def _record_order_confirmed(
    order_id: UUID,
    user,
    trigger: Literal["manual", "automatic_payment"],
    request_id: str | None = None,
) -> None:
    """Emite `order.confirmed` — único punto de emisión para las dos rutas de
    la transición `recibida → abierta` (research.md § 4).

    Vive fuera de `_confirm_order_impl` a propósito: esa función no tiene
    frontera transaccional propia (quien la llama decide el `commit`), así que
    emitir desde dentro publicaría el evento antes del `commit`, justo lo que
    FR-010 prohíbe. Cada llamador la invoca tras su propio `commit`, pasando el
    `trigger` que corresponde a su ruta.

    Con `trigger="automatic_payment"` (la confirmación es efecto colateral de
    confirmar un pago) el actor es `sistema`, no el cajero: ese cajero queda
    registrado en el evento de pago que la disparó, correlacionable por el
    mismo `order_id` (spec 074, US2).

    `request_id` (extensión de logging operativo, FR-021) correlaciona este
    evento con la entrada de log operativo de la misma petición HTTP; es
    `None` cuando quien llama no lo tiene a mano."""
    actor = (
        OrderAuditActor(type=ActorType.SISTEMA)
        if trigger == "automatic_payment"
        else _cajero_actor(user)
    )
    record_order_audit_event(
        event_type=OrderAuditEventType.ORDER_CONFIRMED,
        order_id=order_id,
        tenant_id=_tenant_of(user),
        actor=actor,
        details={"trigger": trigger},
        request_id=request_id,
    )


def _reload_order(db: Session, order_id: UUID) -> CustomerOrder:
    return db.execute(
        select(CustomerOrder)
        .options(
            selectinload(CustomerOrder.items).selectinload(OrderItem.options),
            selectinload(CustomerOrder.payment_attempts)
            .selectinload(OrderPaymentAttempt.payment_method),
        )
        .where(CustomerOrder.id == order_id)
    ).scalar_one()


# --------------------------------------------------------------------- Bloqueo

def block_order(db: Session, order_id: UUID, data: BlockIn) -> CustomerOrder:
    try:
        order = db.execute(
            select(CustomerOrder).where(CustomerOrder.id == order_id).with_for_update()
        ).scalar_one_or_none()
        if order is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
        if order.status != "abierta":
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"La orden no está abierta (status={order.status})"
            )
        if order.version != data.version:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"error": "Conflicto de versión (la orden cambió)", "version_actual": order.version},
            )

        pendientes = db.execute(
            select(OrderItem).where(
                OrderItem.order_id == order.id,
                OrderItem.estado_cocina.in_(EN_CURSO),
            )
        ).scalars().all()
        if pendientes:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": "Hay ítems sin terminar en cocina; anúlalos o espera a que estén listos.",
                    "items": [
                        {
                            "order_item_id": str(it.id),
                            "product_variant_id": str(it.product_variant_id),
                            "estado_cocina": it.estado_cocina,
                            "participant_id": str(it.participant_id) if it.participant_id else None,
                        }
                        for it in pendientes
                    ],
                },
            )

        order.status = "bloqueada"
        order.version += 1
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Error bloqueando orden para cobro")
        raise

    return _reload_order(db, order_id)


# ----------------------------------------------------------------- Cuenta/split

def compute_bill(db: Session, table_id: UUID) -> BillResponse:
    table = get_or_404(db, DiningTable, table_id, "Table not found")

    orders = db.execute(
        select(CustomerOrder)
        .options(selectinload(CustomerOrder.items))
        .where(
            CustomerOrder.dining_table_id == table.id,
            CustomerOrder.status != "cancelada",
        )
        .order_by(CustomerOrder.created_at)
    ).scalars().all()

    # nombres de comensal por sesión
    names = dict(db.execute(
        select(SessionParticipant.id, SessionParticipant.display_label)
    ).all())

    total = Decimal("0")
    order_lines: list[BillOrderLine] = []
    split: dict[UUID | None, Decimal] = {}

    for order in orders:
        subtotal = Decimal("0")
        items: list[BillItemLine] = []
        for it in order.items:
            if it.estado_cocina == "anulado":
                continue
            # spec 089: `unit_price × quantity + addons_total` (addons_total = 0 en histórico).
            line_total = it.line_total
            subtotal += line_total
            split[it.participant_id] = split.get(it.participant_id, Decimal("0")) + line_total
            items.append(BillItemLine(
                order_item_id=it.id, product_variant_id=it.product_variant_id,
                participant_id=it.participant_id, quantity=it.quantity,
                unit_price=it.unit_price, line_total=line_total,
                estado_cocina=it.estado_cocina,
            ))
        total += subtotal
        order_lines.append(BillOrderLine(
            order_id=order.id, status=order.status, subtotal=subtotal, items=items,
        ))

    split_lines = [
        BillSessionLine(
            participant_id=sid,
            display_label=names.get(sid) if sid else None,
            subtotal=amount,
        )
        for sid, amount in split.items()
    ]

    return BillResponse(
        dining_table_id=table.id, total=total, orders=order_lines, split=split_lines,
    )


# ------------------------------------------------------------------------ Pago

#: Centinela para "sin filtrar por comensal". No sirve `None`: en el cobro split,
#: `participant_id=None` significa justamente "las líneas sin comensal asignado"
#: (las que metió el mesero), que es un filtro `IS NULL`, no la ausencia de filtro.
ALL_PARTICIPANTS = object()


def order_sale_lines(
    db: Session, order_id: UUID, *, participant_id=ALL_PARTICIPANTS
) -> list[SaleLine]:
    """Líneas cobrables de un pedido, convertidas al formato del constructor de
    venta (con el snapshot inmutable de descripción y opciones).

    Con `participant_id` devuelve solo las de ese comensal (o las sin asignar si
    se pasa `None`): es lo que hace posible el cobro `split`, una venta por
    persona."""
    stmt = (
        select(OrderItem)
        .options(selectinload(OrderItem.options))
        .where(OrderItem.order_id == order_id, OrderItem.estado_cocina != "anulado")
    )
    if participant_id is not ALL_PARTICIPANTS:
        stmt = stmt.where(OrderItem.participant_id == participant_id)

    lines: list[SaleLine] = []
    for it in db.execute(stmt).scalars():
        variant = db.get(ProductVariant, it.product_variant_id)
        product = db.get(Product, variant.product_id) if variant else None
        description = format_item_description(product, variant)
        lines.append(SaleLine(
            product_variant_id=it.product_variant_id,
            description=description,
            options=[
                {"option_id": str(chosen.option.id), "name": chosen.option.name,
                 "extra_price": str(chosen.option.extra_price), "quantity": chosen.quantity,
                 # spec 089: ausente = por unidad (snapshots anteriores). Solo se escribe
                 # la clave en los adicionales de la regla nueva.
                 **({"per_line": True} if chosen.per_line else {})}
                for chosen in chosen_from_rows(db, it)
            ],
            quantity=it.quantity,
            unit_price=Decimal(it.unit_price),
            addons_total=Decimal(it.addons_total or 0),
            combo_id=it.combo_id,
            line_id=it.id,
        ))
    return lines


def promo_lines_for(db: Session, lines: list[SaleLine]) -> list[dict]:
    """`promo_lines` para `evaluate_variant_sets` (spec 063,
    contracts/motor-y-persistencia.md §1): un dict por línea con
    `product_variant_id` (pertenencia al conjunto), `unit_price`,
    `base_unit_price` (spec 083, FR-027: `unit_price` sin toppings — base del
    descuento `percent`), `quantity`, `line_id` (desempate determinista),
    `_variant_active` (FR-011), `combo_id` (filtro defensivo — histórico) y
    `description` (para `applied_promotions`).

    Ya NO se traen `product_id` / `category_id` / `presentation_id` (targets y
    presentación eliminados, spec 063 FR-003)."""
    promo_lines: list[dict] = []
    for line in lines:
        variant = db.get(ProductVariant, line.product_variant_id)
        promo_lines.append({
            "product_variant_id": line.product_variant_id,
            "unit_price": line.unit_price,
            "base_unit_price": line.base_unit_price,
            "quantity": line.quantity,
            "line_id": getattr(line, "line_id", None),
            "combo_id": line.combo_id,
            "_variant_active": bool(variant.active) if variant else False,
            "description": line.description,
        })
    return promo_lines


def auto_discount(
    db: Session, lines: list[SaleLine], now: datetime, tenant: Tenant | None = None,
) -> tuple[Decimal, UUID | None, list[dict]]:
    """`(descuento_automático_total, promotion_id, applied)` — motor por conjunto
    de variantes (spec 063). `promotion_id = applied[0].promotion_id` si una sola
    promoción explica el descuento, si no `None` (`applied_promotions` lo cubre —
    A-29 resuelto). `applied` es el snapshot serializable para
    `Sale/Invoice/CustomerOrder.applied_promotions` (FR-021). `tenant` (spec
    127): si el caller lo tiene resuelto, evalúa vigencia en SU zona horaria."""
    r = promotions.evaluate_variant_sets(db, promo_lines_for(db, lines), now, tenant)
    return r.total, r.single_promotion_id, promotions.applied_to_dicts(r.applied)


def promotion_evaluation_instant(
    orders: list[CustomerOrder], *, now: datetime,
) -> datetime:
    """spec 073 (FR-009/FR-012/FR-012a, A-70): el instante contra el que se
    evalúa la vigencia **TEMPORAL** de las promociones. El más antiguo
    `promotion_evaluated_at` no nulo de `orders` (FR-012a: rondas sucesivas de
    una misma cuenta usan un único instante, el del pedido más antiguo
    pendiente). Si ninguna orden lo tiene (todas anteriores a esta spec,
    FR-012), cae a `now` — la hora del cobro, comportamiento actual sin cambios.

    Todo aware UTC: la columna es `DateTime(timezone=True)` (D1) y `now` en cada
    call site es `datetime.now(timezone.utc)` / `utc_now()`. `min()` y el retorno
    son homogéneos; `local_now()` los convierte a hora local del tenant. El
    guard defensivo normaliza cualquier naive por si un call site futuro lo
    pasara.

    Para `group_bill` (mesas fusionadas) se llama **por pedido**
    (`promotion_evaluation_instant([o], now=now)` dentro del bucle), no sobre
    todo el grupo (FR-018a): esos pedidos se cobran individualmente y el preview
    consolidado debe coincidir con el cobro per-pedido."""
    now = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    frozen = [
        (
            o.promotion_evaluated_at
            if o.promotion_evaluated_at.tzinfo is not None
            else o.promotion_evaluated_at.replace(tzinfo=timezone.utc)
        )
        for o in orders
        if o.promotion_evaluated_at is not None
    ]
    return min(frozen) if frozen else now


def line_discount_snapshot(
    result, index: int, line_total: Decimal, quantity: int, addons_total: Decimal = Decimal(0)
):
    """`(discounted_unit_price, discounted_line_total)` del ítem `index` a partir
    del `by_line` de **una sola** llamada a `evaluate_variant_sets`, o
    `(None, None)` si esa línea no recibió descuento.

    spec 089: `discounted_unit_price` conserva su significado "precio por unidad de
    producto": los adicionales (`addons_total`, cobrados una vez) no se reparten entre
    las unidades. Sin adicionales es idéntico al cálculo anterior.

    spec 118 (research.md D2): relocada desde `cart/service.py::_cart_line_discount`
    -- misma fórmula, ahora pública porque los write-paths de `orders/` también la
    necesitan, no solo `cart.service.submit_cart`."""
    d = result.by_line.get(index, Decimal(0))
    if d <= 0:
        return None, None
    discounted_line_total = (line_total - d).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    discounted_unit_price = ((discounted_line_total - Decimal(addons_total or 0)) / quantity).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return discounted_unit_price, discounted_line_total


def refresh_order_item_discounts(
    db: Session, order: CustomerOrder, tenant: Tenant | None = None
) -> None:
    """spec 118 (research.md D1-D3, contracts/order-item-discount-snapshot.md):
    recalcula y sobrescribe `discounted_unit_price`/`discounted_line_total` de
    **todas** las líneas vigentes (`estado_cocina != "anulado"`) de `order`, no
    solo la que cambió -- el motor de promociones puede redistribuir el
    descuento entre filas existentes cuando se agrega o se quita una unidad
    (`_distribute_group_discount`). Escribe `(None, None)` cuando una línea ya
    no califica (FR-006).

    Se llama al final de cada mutación de líneas de un pedido abierto
    (`create_order`, `consolidate_table`, `add_item_to_order`, `void_item`),
    antes del `db.commit()` de esa misma transacción -- nunca sobre un pedido
    ya cobrado. Usa el mismo instante congelado que `compute_checkout_preview`/
    `checkout_and_send`/`pay_order` (`promotion_evaluation_instant`), para que
    el número coincida centavo a centavo con lo que cobrará el módulo de cobro
    (FR-002)."""
    lines = order_sale_lines(db, order.id)
    instant = promotion_evaluation_instant([order], now=datetime.now(timezone.utc))
    result = promotions.evaluate_variant_sets(db, promo_lines_for(db, lines), instant, tenant)
    for idx, line in enumerate(lines):
        discounted_unit_price, discounted_line_total = line_discount_snapshot(
            result, idx, line.line_total, line.quantity, line.addons_total,
        )
        item = db.get(OrderItem, line.line_id)
        item.discounted_unit_price = discounted_unit_price
        item.discounted_line_total = discounted_line_total


def compute_checkout_preview(
    db: Session, order_id: UUID, tenant: Tenant | None = None
) -> CheckoutPreviewResponse:
    """spec 073 (FR-001…FR-007a): desglose autoritativo de un pedido **ya
    persistido** y aún no cobrado — mesa individual atendida por la Terminal,
    para llevar o domicilio. Solo lectura: sin `db.commit()`, sin `build_sale`,
    sin lock, sin turno de caja.

    Reusa literalmente el mismo motor que el cobro real (`order_sale_lines` +
    `auto_discount` + `compute_total`), evaluando la vigencia contra el instante
    congelado del pedido (`promotion_evaluation_instant`, FR-009/FR-012) — el
    monto que este preview muestra es el que `pay_order`/`checkout_and_send`
    cobrará. Contrato: contracts/preview-cobro-pedido.md."""
    order = get_or_404(db, CustomerOrder, order_id, "Order not found")
    if order.status in TERMINAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"El pedido no está en un estado cobrable (status={order.status})",
        )

    lines = order_sale_lines(db, order.id)
    raw_subtotal = sum((line.line_total for line in lines), start=Decimal("0"))

    instant = promotion_evaluation_instant([order], now=datetime.now(timezone.utc))
    promo_discount, _, _ = auto_discount(db, lines, instant, tenant)

    delivery_fee = order.delivery_fee or Decimal("0")
    total = max(
        Decimal("0"),
        compute_total(raw_subtotal, promo_discount, delivery_fee=delivery_fee),
    )

    return CheckoutPreviewResponse(
        subtotal=raw_subtotal,
        discount=promo_discount,
        delivery_fee=delivery_fee,
        total=total,
        promotion_evaluated_at=instant,
    )


def compute_draft_preview(
    db: Session, data: DraftPreviewIn, tenant: Tenant | None = None
) -> CheckoutPreviewResponse:
    """spec 073 (FR-013 a FR-015a): desglose de un pedido **hipotético** —
    todavía sin `CustomerOrder` — mientras el cajero arma una orden manual.
    Reusa el mismo motor que el cobro real (`auto_discount` + `compute_total`),
    construyendo las líneas directamente desde `data.items` con el mismo precio
    unitario que `create_order` pondría en `OrderItem.unit_price`
    (`compute_line_price`), para que el subtotal coincida centavo a centavo con
    el que tendrá el pedido una vez creado.

    `promotion_evaluated_at` es siempre la hora de la llamada (sin congelar): el
    borrador no tiene todavía un pedido cuyo instante congelar — el pedido
    congela el suyo al crearse (FR-008). Contrato:
    contracts/preview-borrador-orden-manual.md."""
    lines: list[SaleLine] = []
    for item in data.items:
        variant = db.get(ProductVariant, item.product_variant_id)
        if variant is None or not variant.active:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Variante inexistente o inactiva: {item.product_variant_id}",
            )
        options = load_valid_options(db, item.options, variant=variant, quantity=item.quantity)
        # spec 117 (A-126): el borrador de pedido manual nunca es canal QR.
        options = mark_per_line(db, options, qr_addons_per_line=False)
        product = db.get(Product, variant.product_id)
        description = format_item_description(product, variant)
        lines.append(SaleLine(
            product_variant_id=item.product_variant_id,
            description=description,
            options=[
                {"option_id": str(chosen.option.id), "name": chosen.option.name,
                 "extra_price": str(chosen.option.extra_price), "quantity": chosen.quantity,
                 **({"per_line": True} if chosen.per_line else {})}
                for chosen in options
            ],
            quantity=item.quantity,
            unit_price=compute_unit_price(variant, options),
            addons_total=compute_addons_total(options),
        ))

    raw_subtotal = sum((line.line_total for line in lines), start=Decimal("0"))
    now = datetime.now(timezone.utc)  # sin congelar (FR-008)
    promo_discount, _, _ = auto_discount(db, lines, now, tenant)
    delivery_fee = data.delivery_fee or Decimal("0")
    total = max(
        Decimal("0"),
        compute_total(raw_subtotal, promo_discount, delivery_fee=delivery_fee),
    )

    return CheckoutPreviewResponse(
        subtotal=raw_subtotal,
        discount=promo_discount,
        delivery_fee=delivery_fee,
        total=total,
        promotion_evaluated_at=now,
    )


def pay_order(
    db: Session, order_id: UUID, data: PayIn, cashier: User, tenant: Tenant | None = None
) -> Sale:
    order = get_or_404(db, CustomerOrder, order_id, "Order not found")
    if order.status != "bloqueada":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "La orden debe estar bloqueada para cobrar (bloquea primero).",
        )
    shift = ensure_open_shift(db, data.cash_shift_id)

    try:
        # spec 073 (FR-009, A-70): la vigencia temporal se evalúa contra el
        # instante congelado del pedido (o la hora del cobro si es anterior a
        # esta spec — FR-012).
        instant = promotion_evaluation_instant([order], now=datetime.now(timezone.utc))
        lines = order_sale_lines(db, order.id)

        # Descuento automático (RF-012): motor por conjunto de variantes (spec 063).
        promo_discount, final_promotion_id, applied = auto_discount(db, lines, instant, tenant)

        sale = build_sale(
            db,
            lines=lines,
            shift=shift,
            cashier=cashier,
            payments=data.payments,
            discount=Decimal(data.discount) + promo_discount,
            tax=data.tax, tip=data.tip,
            delivery_fee=order.delivery_fee or Decimal("0"),
            customer_name=order.customer_name,
            dining_table_id=order.dining_table_id,
            table_session_id=order.table_session_id,
            participant_id=order.participant_id,
            customer_order_id=order.id,
            promotion_id=final_promotion_id,
            applied_promotions=applied,
            promotion_evaluated_at=instant,  # FR-011a
        )
        order.status = "pagada"
        # FR-021: el descuento agregado + la lista de promociones también en la orden.
        order.discount = Decimal(data.discount) + promo_discount
        order.applied_promotions = applied
        # NO se descuenta inventario aquí: ya se hizo al confirmar el pedido.

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Error cobrando la orden de mesa")
        raise

    return db.execute(
        select(Sale)
        .options(selectinload(Sale.items), selectinload(Sale.payments))
        .where(Sale.id == sale.id)
    ).scalar_one()


# --------------------------------------------------------------- Confirmación

def _deduct_and_open(db: Session, order: CustomerOrder, user: User) -> CustomerOrder:
    """Núcleo de `recibida` → `abierta`: descuenta inventario y abre el pedido
    a cocina, sobre una orden **ya cargada** (con o sin lock — lo decide quien
    llama) y con `order.items` ya en memoria. Sin `commit`/`rollback` propios.

    Extraído de `_confirm_order_impl` (spec 028, T016) para que
    `checkout_and_send` pueda reusar exactamente el mismo descuento de
    inventario sin pasar por la exigencia de `OrderPaymentAttempt` confirmado
    (spec 024, FR-017), que es exclusiva del comensal anónimo por QR: aquí el
    cobro ya se resolvió en la misma llamada, vía `build_sale`, no vía un
    intento de pago aparte."""
    entries = [
        (it, chosen_from_rows(db, it))
        for it in order.items
        if it.estado_cocina != "anulado"
    ]
    if not entries:
        raise HTTPException(status.HTTP_409_CONFLICT, "El pedido no tiene ítems")

    deduct_order_items(db, entries, user.id, reference_id=order.id)

    order.status = "abierta"
    order.version += 1
    return order


def _confirm_order_impl(db: Session, order_id: UUID, user: User) -> CustomerOrder:
    """Lógica de `recibida` → `abierta` (descuento de inventario incluido), sin
    `commit`/`rollback` propios — quien la invoque decide la frontera de la
    transacción. Extraída de `confirm_order` (spec 026, FR-001/FR-002,
    research.md Decisión 1) para que `confirm_cash_payment_attempt` y
    `approve_payment_attempt` puedan ejecutarla dentro de su propia transacción
    y así confirmar el pago y enviar el pedido a cocina de forma atómica: si
    falta stock, el `rollback` del llamador revierte también la confirmación
    del pago, sin dejar nada a medias.

    Mismas consultas y mismo orden de validación que antes (spec 028, T016):
    solo se movió el descuento propiamente dicho a `_deduct_and_open`, sin
    tocar el resto de este cuerpo — el comportamiento observable de los tres
    llamadores actuales (`confirm_order`, `approve_payment_attempt`,
    `confirm_cash_payment_attempt`) no cambia."""
    order = db.execute(
        select(CustomerOrder)
        .options(selectinload(CustomerOrder.items).selectinload(OrderItem.options))
        .where(CustomerOrder.id == order_id)
        .with_for_update(of=CustomerOrder)
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    if order.status != "recibida":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Solo se confirman pedidos en 'recibida' (status={order.status})",
        )

    # spec 024, FR-017: una orden solo avanza a comanda con un intento de pago
    # confirmado. Se verifica antes de tocar inventario a propósito — si el
    # pago no está confirmado, esta llamada no debe tener ningún efecto
    # secundario (research.md spec 024, Decisión 5).
    has_confirmed_payment = db.execute(
        select(OrderPaymentAttempt.id).where(
            OrderPaymentAttempt.order_id == order.id,
            OrderPaymentAttempt.status == "confirmado",
        )
    ).scalar_one_or_none()
    if has_confirmed_payment is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "La orden no tiene un pago confirmado"
        )

    return _deduct_and_open(db, order, user)


def confirm_order(
    db: Session, order_id: UUID, user: User, request_id: str | None = None
) -> CustomerOrder:
    """`recibida` → `abierta`: el staff acepta el pedido que envió el comensal y
    **aquí es donde se compromete el inventario**.

    Es el único punto de descuento de los pedidos por QR (`_confirm_order_impl`).
    La validación de stock es la real (con lock por fila, en orden canónico de id
    para no deadlockear); el chequeo del carrito era solo preventivo y pudo quedar
    obsoleto. Si falta stock de un solo insumo, la transacción entera hace
    rollback y el pedido sigue `recibida`, listo para reintentar o cancelar.

    Endpoint público (`POST /orders/{id}/confirm`) sin cambios de contrato. Desde
    spec 026, el flujo normal de la Terminal de Mesas ya no lo invoca
    manualmente — confirmar el pago (`confirm_cash_payment_attempt`/
    `approve_payment_attempt`) dispara esta misma lógica automáticamente. Se
    mantiene expuesto como vía de recuperación (spec 026, research.md
    Decisión 2)."""
    try:
        _confirm_order_impl(db, order_id, user)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Error confirmando el pedido")
        raise

    # spec 074: ruta manual (de recuperación) — el actor es el cajero que la
    # ejecutó, no el sistema.
    _record_order_confirmed(order_id, user, "manual", request_id)

    return _reload_order(db, order_id)


# ---------------------------------------------------------- Cobra y envía (T016)

def checkout_and_send(
    db: Session,
    order_id: UUID,
    data: CheckoutAndSendIn,
    cashier: User,
    request_id: str | None = None,
    tenant: Tenant | None = None,
) -> Sale:
    """Cobra y envía a cocina, en una sola transacción, una comanda creada con
    `hold_for_payment=True` (spec 028, T012/T013): el mostrador/mesero cobra
    **antes** de mandar el pedido a preparar, así que aquí se funden en un
    solo paso lo que en el flujo QR son dos pasos separados y con su propio
    `OrderPaymentAttempt` — confirmar el pago y despachar a cocina
    (`_confirm_order_impl`, spec 026 research.md Decisión 1).

    Reusa el mismo motor de venta que `pay_order` (líneas, promociones/combos,
    `build_sale`) y el mismo núcleo de descuento que `_confirm_order_impl`
    (`_deduct_and_open`), sin su exigencia de `OrderPaymentAttempt` confirmado
    —irrelevante aquí, porque el cobro ya ocurrió en esta misma llamada—.
    Toma el lock de fila igual que `block_order`/`_confirm_order_impl`, y el
    chequeo de `version` es la guarda de idempotencia contra un doble clic: un
    segundo intento con la misma versión ya vencida es 409, sin crear una
    segunda venta. Si falta stock al enviar a cocina, la transacción entera
    revierte —tampoco queda registrada la venta— igual que si `build_sale`
    hubiera fallado."""
    try:
        order = db.execute(
            select(CustomerOrder)
            .options(selectinload(CustomerOrder.items).selectinload(OrderItem.options))
            .where(CustomerOrder.id == order_id)
            .with_for_update(of=CustomerOrder)
        ).scalar_one_or_none()
        if order is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
        if order.status != "recibida":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Solo se cobra y envía pedidos en 'recibida' (status={order.status})",
            )
        if order.version != data.version:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"error": "Conflicto de versión (la orden cambió)", "version_actual": order.version},
            )

        shift = ensure_open_shift(db, data.cash_shift_id)

        # spec 073 (FR-009, A-70): igual que `pay_order` — vigencia temporal
        # contra el instante congelado del pedido.
        instant = promotion_evaluation_instant([order], now=datetime.now(timezone.utc))
        lines = order_sale_lines(db, order.id)

        # Descuento automático (RF-012), igual que `pay_order`: motor por
        # conjunto de variantes (spec 063).
        promo_discount, final_promotion_id, applied = auto_discount(db, lines, instant, tenant)

        sale = build_sale(
            db,
            lines=lines,
            shift=shift,
            cashier=cashier,
            payments=data.payments,
            discount=Decimal(data.discount) + promo_discount,
            tax=data.tax, tip=data.tip,
            delivery_fee=order.delivery_fee or Decimal("0"),
            customer_name=data.billing_customer_name or "Consumidor Final",
            dining_table_id=order.dining_table_id,
            table_session_id=order.table_session_id,
            participant_id=order.participant_id,
            customer_order_id=order.id,
            promotion_id=final_promotion_id,
            applied_promotions=applied,
            promotion_evaluated_at=instant,  # FR-011a
        )
        order.discount = Decimal(data.discount) + promo_discount
        order.applied_promotions = applied

        # Envía a cocina en la misma transacción: si falta stock, el
        # `rollback` de abajo revierte también la venta recién construida —
        # no queda ni cobrada ni a medio enviar.
        _deduct_and_open(db, order, cashier)

        # spec 035, A-52 (registro-de-anomalias.md): a diferencia de
        # `_deduct_and_open` en sus otros dos llamadores (QR, sin venta
        # todavía en ese instante), aquí la `Sale` ya se construyó arriba en
        # esta misma transacción — el pedido queda `'pagada'` de una vez, no
        # `'abierta'`. `tables_advanced.py` ya no trata `'pagada'` como "sin
        # nada pendiente": sigue bloqueando mientras queden ítems por
        # preparar.
        order.status = "pagada"

        # spec 074: copiado antes del `commit` (ver `approve_payment_attempt`).
        order_id_for_audit = order.id

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Error cobrando y enviando la orden a cocina")
        raise

    # spec 074 (FR-014, adenda post-implementación): el cobro ya está
    # comprometido —con su venta y su despacho a cocina— cuando salen estos
    # dos eventos. `_deduct_and_open` aquí hace el mismo trabajo de apertura
    # que `_confirm_order_impl`, así que este camino también cuenta como una
    # confirmación, con actor `sistema` igual que los demás pagos que
    # confirman la orden como efecto colateral (research.md § 4).
    payment_method_types: list[str] = []
    for payment in data.payments:
        method = db.get(PaymentMethod, payment.payment_method_id)
        if method is not None and method.type not in payment_method_types:
            payment_method_types.append(method.type)
    record_order_audit_event(
        event_type=OrderAuditEventType.PAYMENT_CHECKOUT_AND_SEND,
        order_id=order_id_for_audit,
        tenant_id=_tenant_of(cashier),
        actor=_cajero_actor(cashier),
        details={
            "payment_method_types": payment_method_types,
            "total_amount": float(sum(p.amount for p in data.payments)),
            "payment_count": len(data.payments),
        },
        request_id=request_id,
    )
    _record_order_confirmed(order_id_for_audit, cashier, "automatic_payment", request_id)

    return db.execute(
        select(Sale)
        .options(selectinload(Sale.items), selectinload(Sale.payments))
        .where(Sale.id == sale.id)
    ).scalar_one()


# ------------------------------------------------------------------ Cancelación

def cancel_order(
    db: Session,
    order_id: UUID,
    data: CancelIn,
    user: User | None,
    participant: SessionParticipant | None = None,
    tenant_id: int | None = None,
    request_id: str | None = None,
) -> CustomerOrder:
    """Cancela un pedido y ajusta el inventario **según lo que se alcanzó a
    consumir**, no como una reversa simétrica:

    - orden aún sin confirmar (`recibida`): nunca se descontó → cero movimientos;
    - ítem `pendiente`: descontado pero no preparado → entrada real ('in');
    - ítem `en_preparacion`/`listo`: el insumo ya se combinó → **no vuelve al
      stock**. El 'out' de la confirmación ya es esa pérdida; escribir otro
      movimiento la descontaría dos veces. Se traza en `audit_logs`;
    - ítem `anulado`: ya lo resolvió `void_item`.

    Devolver todo al stock (comportamiento anterior) sobrestimaba el inventario en
    silencio hasta el conteo físico.

    El actor es `user` (staff) **o** `participant` (el propio comensal desde el QR,
    que no es un usuario del sistema). Quién puede cancelar en qué estado lo decide
    quien llama: aquí no hay restricción de estado más allá de los terminales,
    porque el staff sí puede cancelar en cualquier momento.

    `tenant_id` (spec 074) es solo para el log de auditoría: en la ruta del
    comensal no hay `User` del que leerlo, así que lo pasa quien llama (el
    router ya lo tiene resuelto). Opcional para no cambiar el contrato de
    ningún llamador existente.
    """
    order = db.execute(
        select(CustomerOrder)
        .options(selectinload(CustomerOrder.items).selectinload(OrderItem.options))
        .where(CustomerOrder.id == order_id)
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    if order.status in TERMINAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"La orden ya es terminal (status={order.status})"
        )
    # Spec 029, hotfix #4: en los caminos QR/mostrador el pago NO mueve
    # `status` a "pagada" (research.md D2) — sin este chequeo, este endpoint
    # podía cancelar un pedido ya cobrado y dejar su `Sale` huérfana.
    if order_has_sale(db, order_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "El pedido ya fue pagado y no puede rechazarse"
        )

    deducted = order.status not in _NOT_DEDUCTED

    try:
        a_revertir: list[tuple[OrderItem, list[ChosenOption]]] = []
        perdidos: list[dict] = []

        for it in order.items:
            if it.estado_cocina == "anulado":
                # Ya se resolvió al anular el ítem (void_item revirtió si procedía).
                continue
            if not deducted:
                # La orden nunca llegó a descontar stock: no hay nada que revertir.
                continue
            if it.estado_cocina in _CONSUMED_KITCHEN:
                # Insumo ya consumido: no vuelve a stock. Se registra como pérdida.
                perdidos.append({
                    "order_item_id": str(it.id),
                    "product_variant_id": str(it.product_variant_id),
                    "quantity": it.quantity,
                    "estado_cocina": it.estado_cocina,
                })
                continue
            a_revertir.append((it, chosen_from_rows(db, it)))

        actor_id = user.id if user is not None else None
        reverse_order_items(db, a_revertir, actor_id, reference_id=order.id)
        revertidos = [str(it.id) for it, _ in a_revertir]

        order.status = "cancelada"
        db.add(OrderCancelLog(
            order_id=order.id, motivo=data.motivo,
            user_id=actor_id,
            user_name=user.name if user is not None else None,
            participant_id=participant.id if participant is not None else None,
        ))

        # Spec 044: rechazar un pedido con pago QR pendiente (efectivo, o
        # transferencia sin comprobante aún) también resuelve ese intento —
        # sin esto quedaba "pendiente" para siempre en una orden ya
        # cancelada. Mismos campos que `reject_payment_attempt` (arriba),
        # pero buscado por `order_id` porque este endpoint no recibe
        # `attempt_id`; el índice único parcial garantiza a lo sumo un
        # `pendiente` por orden, así que `scalar_one_or_none()` es seguro.
        pending_attempt = db.execute(
            select(OrderPaymentAttempt)
            .where(
                OrderPaymentAttempt.order_id == order.id,
                OrderPaymentAttempt.status == "pendiente",
            )
            .with_for_update(of=OrderPaymentAttempt)
        ).scalar_one_or_none()
        if pending_attempt is not None:
            pending_attempt.status = "rechazado"
            pending_attempt.rejection_reason = data.motivo
            pending_attempt.resolved_by_user_id = actor_id
            pending_attempt.resolved_at = utc_now()

        if perdidos:
            # La pérdida no genera movimiento de inventario (ya está descontada);
            # queda trazada en auditoría para el reporte de mermas.
            logger.warning(
                "Cancelación de orden %s con %d ítem(s) ya consumidos: pérdida sin reversa",
                order.id, len(perdidos),
            )
            record_audit(
                db,
                action="order.cancel.loss",
                entity="customer_orders",
                entity_id=order.id,
                user=user,
                payload={
                    "motivo": data.motivo,
                    "items_perdidos": perdidos,
                    "items_revertidos": revertidos,
                    "cancelado_por_comensal": participant.id is not None
                    if participant is not None else False,
                },
            )

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Error cancelando la orden")
        raise

    # spec 074 (FR-009/FR-010): con la cancelación ya comprometida, el evento
    # deja constancia de quién la inició y de si quedó pérdida de inventario
    # (ítems ya consumidos que no volvieron al stock).
    if user is not None:
        cancel_actor, initiated_by = _cajero_actor(user), "staff"
    elif participant is not None:
        cancel_actor = OrderAuditActor(type=ActorType.COMENSAL, id=str(participant.id))
        initiated_by = "comensal"
    else:
        cancel_actor, initiated_by = OrderAuditActor(type=ActorType.SISTEMA), None
    record_order_audit_event(
        event_type=OrderAuditEventType.ORDER_CANCELLED,
        order_id=order_id,
        tenant_id=tenant_id if tenant_id is not None else _tenant_of(user),
        actor=cancel_actor,
        details={
            "initiated_by": initiated_by,
            "reason": data.motivo,
            "inventory_loss": bool(perdidos),
        },
        request_id=request_id,
    )

    return _reload_order(db, order_id)


# ------------------------------------------------------------- Liberar mesa

def close_participants(db: Session, ts: TableSession) -> int:
    """Echa a los comensales de una sesión y abandona sus carritos, **sin tocar la
    sesión**. Devuelve cuántos cerró.

    Se usa suelta cuando una sesión lleva demasiado abierta pero todavía tiene algo
    que cobrar: no interesa que nadie siga pidiendo con un token viejo, pero cerrar
    la sesión dejaría la mesa sin forma de cobrarse.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    participants = db.execute(
        select(SessionParticipant).where(
            SessionParticipant.table_session_id == ts.id,
            SessionParticipant.status == "open",
        )
    ).scalars().all()

    for p in participants:
        p.status = "closed"
        if p.closed_at is None:
            p.closed_at = now
        for cart in db.execute(
            select(Cart).where(Cart.participant_id == p.id, Cart.status == "abierto")
        ).scalars():
            cart.status = "abandonado"

    return len(participants)


def close_table_sessions(
    db: Session, table_id: UUID, *, closed_by: User | None = None
) -> list[TableSession]:
    """Cierra en cascada las sesiones `active` de una mesa: la sesión de mesa, sus
    comensales y los carritos que quedaran abiertos.

    No hace commit (se une a la transacción del caller) ni valida órdenes
    pendientes — eso es responsabilidad de quien llama (`release_table`, el cierre
    con `billing_mode`, o el job de sesiones huérfanas, que pasa `closed_by=None`).
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    sessions = db.execute(
        select(TableSession).where(
            TableSession.dining_table_id == table_id,
            TableSession.status == "active",
        )
    ).scalars().all()

    for ts in sessions:
        ts.status = "closed"
        if ts.closed_at is None:
            ts.closed_at = now
        if closed_by is not None:
            ts.closed_by_user_id = closed_by.id
            ts.closed_by_user_name = closed_by.name

        close_participants(db, ts)

    return sessions


def delete_orphan_carts(db: Session, sessions: list[TableSession]) -> None:
    """Elimina físicamente los `Cart` de los participantes de `sessions`, sin
    importar su `status` (spec 039, FR-001/FR-002/FR-004). Se invoca en el
    call-site, exactamente donde la mesa ya quedó `libre` — nunca dentro de
    `close_table_sessions`/`close_participants` (research.md Decisión 1), para no
    borrar carritos cuando la mesa no termina liberándose (`_sweep_schema`,
    RN-SCHED-04). No hace `commit()`/`flush()` propio: se une a la transacción
    del caller.
    """
    if not sessions:
        return

    participant_ids = select(SessionParticipant.id).where(
        SessionParticipant.table_session_id.in_([s.id for s in sessions])
    )
    db.execute(delete(Cart).where(Cart.participant_id.in_(participant_ids)))


def release_table(
    db: Session, table_id: UUID, *, closed_by: User | None = None,
    closed_out: list | None = None,
) -> DiningTable:
    """Libera la mesa (regla dura: cero órdenes no-terminales).

    spec 089 (A-95): `closed_out`, si se pasa, recibe las sesiones que cerró para que el llamador
    avise al comensal DESPUÉS del commit (`notify_sessions_closed`); el commit ocurre aquí dentro."""
    table = get_or_404(db, DiningTable, table_id, "Table not found")

    blocking = db.execute(
        select(CustomerOrder)
        .options(selectinload(CustomerOrder.items))
        .where(
            CustomerOrder.dining_table_id == table.id,
            CustomerOrder.status.notin_(TERMINAL),
        )
    ).scalars().all()
    if blocking:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "La mesa tiene órdenes sin cerrar (paga o cancela primero).",
                "orders": [
                    {
                        "order_id": str(o.id),
                        "status": o.status,
                        "items": len([i for i in o.items if i.estado_cocina != "anulado"]),
                    }
                    for o in blocking
                ],
            },
        )

    try:
        table.status = "libre"
        sessions = close_table_sessions(db, table.id, closed_by=closed_by)
        delete_orphan_carts(db, sessions)
        db.commit()
        if closed_out is not None:
            closed_out.extend(sessions)
    except Exception:
        db.rollback()
        logger.exception("Error liberando la mesa")
        raise

    db.refresh(table)
    return table


# ------------------------------------------------------- Intentos de pago (spec 024)
# Revisión del cajero: aprobar/rechazar comprobante, confirmar efectivo (US2/US3).
# `confirm_order` (arriba) es el único punto que hace avanzar la orden a comanda;
# estas tres funciones solo resuelven el intento, nunca tocan `CustomerOrder.status`
# ni descuentan inventario (contracts/cashier-payment-review.md).

def _load_pending_attempt_for_update(db: Session, attempt_id: UUID) -> OrderPaymentAttempt:
    """Bloqueo pesimista sobre el intento, solo si sigue `pendiente` — mismo
    patrón que `confirm_order` usa sobre la orden (research.md spec 024,
    Decisión 9). Garantiza FR-018/SC-007: una segunda resolución casi
    simultánea del mismo intento no tiene efecto."""
    attempt = db.execute(
        select(OrderPaymentAttempt)
        .where(OrderPaymentAttempt.id == attempt_id)
        .with_for_update(of=OrderPaymentAttempt)
    ).scalar_one_or_none()
    if attempt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Intento de pago no encontrado")
    if attempt.status != "pendiente":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"El intento de pago ya fue resuelto (status={attempt.status})",
        )
    return attempt


def list_payment_attempts(db: Session, order_id: UUID) -> list[OrderPaymentAttempt]:
    """Historial completo de intentos de una orden, para cajero/back-office
    (FR-016) — incluye rechazados, con su motivo."""
    get_or_404(db, CustomerOrder, order_id, "Order not found")
    return db.execute(
        select(OrderPaymentAttempt)
        .options(selectinload(OrderPaymentAttempt.payment_method))
        .where(OrderPaymentAttempt.order_id == order_id)
        .order_by(OrderPaymentAttempt.created_at)
    ).scalars().all()


def approve_payment_attempt(
    db: Session,
    attempt_id: UUID,
    cash_shift_id: UUID,
    user: User,
    request_id: str | None = None,
) -> OrderPaymentAttempt:
    """Aprueba un comprobante de transferencia (US2, Acceptance Scenario 4).

    Spec 026, FR-001: aprobar el comprobante confirma el intento de pago y, en
    la misma transacción, envía el pedido a cocina (`_confirm_order_impl`) — si
    falta stock, ninguna de las dos cosas queda registrada (FR-002,
    research.md Decisión 1).

    Spec 028: además genera la venta/factura en esta misma transacción (antes
    solo se generaba al "Cobrar y cerrar mesa" — spec 010 `close_session` —,
    botón que esta spec retira del flujo QR; sin esto, un pedido QR nunca
    llegaba a tener una `Sale`/`Invoice`, y "Reimprimir Factura POS" y
    "Liberar Mesa" quedaban permanentemente rotos para ese pedido). Reusa
    exactamente el mismo motor de venta que `pay_order`/`checkout_and_send`
    (líneas, promociones/combos, `build_sale`) — un único pago por el total
    exacto de la orden, con el método del comprobante ya aprobado."""
    try:
        attempt = _load_pending_attempt_for_update(db, attempt_id)
        method = get_or_404(db, PaymentMethod, attempt.payment_method_id, "Payment method not found")
        if method.is_cash:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Un método en efectivo se confirma con confirm-cash, no con approve",
            )
        if not attempt.receipt_file_url:
            raise HTTPException(status.HTTP_409_CONFLICT, "El intento no tiene comprobante todavía")

        # spec 074: se copian antes del `commit` (que expira los atributos del
        # intento) para auditarlos después, con la transición ya comprometida.
        order_id = attempt.order_id
        receipt_file_url = attempt.receipt_file_url

        shift = ensure_open_shift(db, cash_shift_id)

        attempt.status = "confirmado"
        attempt.resolved_by_user_id = user.id
        attempt.resolved_at = utc_now()
        # La sesión tiene autoflush=False (ver `with_db`); sin este flush,
        # el chequeo `has_confirmed_payment` de `_confirm_order_impl` lee el
        # `status` todavía persistido ("pendiente") y siempre rechaza con
        # 409, aunque el intento ya esté confirmado en memoria.
        db.flush()
        order = _confirm_order_impl(db, attempt.order_id, user)

        # spec 073 (FR-009/FR-018, A-70): vigencia temporal contra el instante
        # congelado del pedido (el flujo QR también lo congela — research.md D2).
        instant = promotion_evaluation_instant([order], now=utc_now())
        lines = order_sale_lines(db, order.id)
        promo_discount, final_promotion_id, applied = auto_discount(db, lines, instant)
        # Spec 056, research.md Decisión 5: el domicilio debe quedar incluido
        # en el ÚNICO pago que se autogenera aquí — de lo contrario queda
        # corto exactamente en ese valor y el propio chequeo `paid < total`
        # de build_sale (ya con el domicilio sumado) rechazaría este pago.
        delivery_fee = order.delivery_fee or Decimal("0")
        total = (
            sum((line.line_total for line in lines), Decimal("0"))
            - promo_discount + delivery_fee
        )

        build_sale(
            db,
            lines=lines,
            shift=shift,
            cashier=user,
            payments=[PaymentIn(payment_method_id=attempt.payment_method_id, amount=total)],
            discount=promo_discount,
            delivery_fee=delivery_fee,
            customer_name=order.customer_name,
            dining_table_id=order.dining_table_id,
            table_session_id=order.table_session_id,
            participant_id=order.participant_id,
            customer_order_id=order.id,
            promotion_id=final_promotion_id,
            applied_promotions=applied,
            promotion_evaluated_at=instant,  # FR-011a
        )
        order.discount = promo_discount
        order.applied_promotions = applied

        # spec 035, A-52 (registro-de-anomalias.md): la Sale ya se construyó
        # arriba en esta misma transacción — igual que en checkout_and_send,
        # el pedido queda 'pagada' de una vez, no 'abierta' (aunque cocina
        # todavía no haya terminado de prepararlo).
        order.status = "pagada"

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Error aprobando comprobante de pago")
        raise

    # spec 074: el comprobante viaja como hash (FR-005/FR-012) — el mismo que
    # ya llevó `order.payment_attempt.created` para este intento.
    record_order_audit_event(
        event_type=OrderAuditEventType.PAYMENT_TRANSFER_APPROVED,
        order_id=order_id,
        tenant_id=_tenant_of(user),
        actor=_cajero_actor(user),
        details={"receipt_hash": hash_sensitive_or_none(receipt_file_url)},
        request_id=request_id,
    )
    # La aprobación disparó la confirmación de la orden dentro de la misma
    # transacción: su actor es `sistema`, no este cajero (US2).
    _record_order_confirmed(order_id, user, "automatic_payment", request_id)

    db.refresh(attempt)
    return attempt


def reject_payment_attempt(
    db: Session,
    attempt_id: UUID,
    reason: str,
    user: User,
    request_id: str | None = None,
) -> OrderPaymentAttempt:
    """Rechaza un comprobante con motivo obligatorio (FR-014, US2 Acceptance
    Scenario 5-6). El motivo queda visible solo para cajero/back-office
    (Clarification 3) — el router del comensal nunca lo serializa."""
    try:
        attempt = _load_pending_attempt_for_update(db, attempt_id)
        method = get_or_404(db, PaymentMethod, attempt.payment_method_id, "Payment method not found")
        if method.is_cash:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Un método en efectivo se confirma con confirm-cash, no se rechaza",
            )

        # spec 074: copiados antes del `commit` (ver `approve_payment_attempt`).
        order_id = attempt.order_id
        receipt_file_url = attempt.receipt_file_url

        attempt.status = "rechazado"
        attempt.rejection_reason = reason
        attempt.resolved_by_user_id = user.id
        attempt.resolved_at = utc_now()
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Error rechazando comprobante de pago")
        raise

    # spec 074: mismo `receipt_hash` que el evento de creación del intento —
    # reconoce el comprobante sin revelarlo (FR-012). El motivo del rechazo es
    # texto operativo del cajero, no dato personal: viaja tal cual.
    record_order_audit_event(
        event_type=OrderAuditEventType.PAYMENT_TRANSFER_REJECTED,
        order_id=order_id,
        tenant_id=_tenant_of(user),
        actor=_cajero_actor(user),
        details={
            "receipt_hash": hash_sensitive_or_none(receipt_file_url),
            "rejection_reason": reason,
        },
        request_id=request_id,
    )

    db.refresh(attempt)
    return attempt


def confirm_cash_payment_attempt(
    db: Session,
    attempt_id: UUID,
    amount_received: Decimal,
    cash_shift_id: UUID,
    user: User,
    request_id: str | None = None,
) -> OrderPaymentAttempt:
    """Confirma un pago en efectivo y calcula el cambio (FR-009/FR-010,
    US3). FR-010a: impide confirmar si `amount_received < total_orden`.

    Spec 026, FR-001: confirmar el efectivo confirma el intento de pago y, en
    la misma transacción, envía el pedido a cocina (`_confirm_order_impl`) — si
    falta stock, ninguna de las dos cosas queda registrada (FR-002,
    research.md Decisión 1).

    Spec 028: además genera la venta/factura en esta misma transacción (mismo
    motivo que `approve_payment_attempt` — ver su docstring). El monto
    recibido en efectivo se manda tal cual a `build_sale`, que calcula
    `change_given` con el mismo criterio (`pagado - total`) que ya usa
    `attempt.change_amount` — ambos coinciden por construcción.

    Spec 073, US7 (FR-021/FR-023, A-70, research.md D13): el chequeo previo del
    "monto recibido" compara contra el `Total` autoritativo —subtotal menos
    descuento por promoción (instante congelado) más domicilio— que devuelve
    `compute_checkout_preview`, la misma función de solo lectura que muestra el
    panel "Pagos por confirmar". Antes usaba `_order_total` (suma sin descuento),
    que rechazaba con 422 un monto que sí cubría la venta con promoción.
    `attempt.change_amount` sale de ese mismo `total` → coincide al peso con
    `Sale.change_given` de `build_sale`."""
    try:
        attempt = _load_pending_attempt_for_update(db, attempt_id)
        method = get_or_404(db, PaymentMethod, attempt.payment_method_id, "Payment method not found")
        if not method.is_cash:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Un método de transferencia se aprueba/rechaza, no se confirma con confirm-cash",
            )

        # spec 073, US7 (FR-023, research.md D13): el `Total` real con descuento
        # por promoción (instante congelado) y domicilio — la misma cuenta
        # autoritativa que el panel muestra y que `build_sale` registrará.
        total = compute_checkout_preview(db, attempt.order_id).total
        if amount_received < total:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"El monto recibido ({amount_received}) es menor al total de la orden ({total})",
            )

        shift = ensure_open_shift(db, cash_shift_id)

        # spec 074: copiado antes del `commit` (ver `approve_payment_attempt`).
        order_id = attempt.order_id

        attempt.amount_received = amount_received
        attempt.change_amount = amount_received - total
        attempt.status = "confirmado"
        attempt.resolved_by_user_id = user.id
        attempt.resolved_at = utc_now()
        # Mismo motivo que en `approve_payment_attempt`: autoflush=False no
        # deja ver este `UPDATE` a la `SELECT` de `_confirm_order_impl` sin
        # este flush explícito (mismo patrón que ya usan
        # `table_sessions/service.py` y `sales/service.py`).
        db.flush()
        order = _confirm_order_impl(db, attempt.order_id, user)

        # spec 073 (FR-009/FR-018, A-70): vigencia temporal contra el instante
        # congelado del pedido.
        instant = promotion_evaluation_instant([order], now=utc_now())
        lines = order_sale_lines(db, order.id)
        promo_discount, final_promotion_id, applied = auto_discount(db, lines, instant)

        build_sale(
            db,
            lines=lines,
            shift=shift,
            cashier=user,
            payments=[PaymentIn(payment_method_id=attempt.payment_method_id, amount=amount_received)],
            discount=promo_discount,
            delivery_fee=order.delivery_fee or Decimal("0"),
            customer_name=order.customer_name,
            dining_table_id=order.dining_table_id,
            table_session_id=order.table_session_id,
            participant_id=order.participant_id,
            customer_order_id=order.id,
            promotion_id=final_promotion_id,
            applied_promotions=applied,
            promotion_evaluated_at=instant,  # FR-011a
        )
        order.discount = promo_discount
        order.applied_promotions = applied

        # spec 035, A-52 (registro-de-anomalias.md): igual que en
        # approve_payment_attempt/checkout_and_send — la Sale ya existe en
        # esta misma transacción.
        order.status = "pagada"

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Error confirmando pago en efectivo")
        raise

    # spec 074: el pago ya está comprometido (con su venta) cuando sale el
    # evento. `float` porque Sentry Logs solo preserva escalares como atributo
    # filtrable — un `Decimal` viajaría como el `repr()` de Python.
    record_order_audit_event(
        event_type=OrderAuditEventType.PAYMENT_CASH_CONFIRMED,
        order_id=order_id,
        tenant_id=_tenant_of(user),
        actor=_cajero_actor(user),
        details={
            "amount_received": float(amount_received),
            "change": float(amount_received - total),
        },
        request_id=request_id,
    )
    # Confirmar el efectivo disparó la confirmación de la orden en la misma
    # transacción: ese evento lleva actor `sistema`, no este cajero (US2).
    _record_order_confirmed(order_id, user, "automatic_payment", request_id)

    db.refresh(attempt)
    return attempt
