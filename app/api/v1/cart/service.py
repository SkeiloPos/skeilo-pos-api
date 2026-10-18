"""Service del carrito borrador de un comensal.

Unión a la sesión de mesa (con token) y CRUD de líneas con chequeo preventivo de
disponibilidad. **No descuenta inventario**: eso ocurre al confirmar el pedido."""
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core import events
from app.core.asset_refs import resolve_receipt_key
from app.core.config import settings
from app.core.crud import get_or_404
from app.core.models import Tenant
from app.core.order_audit import (
    ActorType,
    OrderAuditActor,
    OrderAuditEventType,
    hash_sensitive_or_none,
    record_order_audit_event,
)
from app.core.qr_context import close_participant
from app.core.qr_token import mint_session_token
from app.core.storage import (
    CONTENT_TYPE_EXTENSIONS,
    build_object_key,
    generate_presigned_put_url,
    public_url_for,
)
from app.api.v1.table_sessions.service import notify_sessions_closed, try_release_if_empty
from app.models.dining_table import DiningTable
from app.models.table_session import TableSession
from app.models.session_participant import SessionParticipant
from app.models.cart import Cart
from app.models.cart_item import CartItem, CartItemOption
from app.models.customer_order import CustomerOrder
from app.models.order_item import OrderItem, OrderItemOption
from app.models.order_payment_attempt import OrderPaymentAttempt
from app.models.option import Option
from app.models.option_group import OptionGroup
from app.catalog_engine import (
    ChosenOption,
    compute_addons_total,
    compute_unit_price,
    line_total as compute_line_total,
    mark_per_line,
)
from app.models.payment import PaymentMethod
from app.models.product_variant import ProductVariant
from app.api.v1.catalog.line_pricing import (
    check_availability,
    ensure_product_available,
    ensure_products_available,
    load_valid_options,
    required_consumption,
)
from app.api.v1.cash.service import resolve_dine_in_table_order
from app.api.v1.orders.checkout import line_discount_snapshot
from app.api.v1.orders.schemas import CancelIn
from app.api.v1.promotions import service as promotions
from app.api.v1.cart.schemas import (
    AdjustedOptionOut,
    CartItemIn,
    CartItemUpdate,
    CartItemOptionResponse,
    CartItemResponse,
    CartResponse,
    ReceiptPresignOut,
    SessionOpenResponse,
    SessionTableInfo,
)

#: Estados no terminales de `CustomerOrder` — mientras el comensal tenga una
#: orden en alguno de estos, no puede crear otra (spec 024, FR-005/FR-006;
#: research.md spec 024, Decisión 8, mismo criterio de "terminal" que ya usa
#: `checkout.TERMINAL`/`mark_order_ready`).
_NON_TERMINAL_ORDER_STATUSES = ("recibida", "abierta", "bloqueada")

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------- Apertura sesión

def _get_or_create_table_session(db: Session, table: DiningTable) -> TableSession:
    """Sesión de mesa `active` de la mesa, creándola si no hay ninguna.

    Escanear el QR de una mesa ya ocupada **une** al comensal a la sesión en curso;
    no abre una segunda. El índice parcial `idx_active_session_per_table` garantiza
    la unicidad aunque dos escaneos entren a la vez."""
    ts = db.execute(
        select(TableSession).where(
            TableSession.dining_table_id == table.id,
            TableSession.status == "active",
        )
    ).scalar_one_or_none()
    if ts is None:
        ts = TableSession(dining_table_id=table.id, status="active")
        db.add(ts)
        db.flush()
    return ts


def unique_display_label(db: Session, table_session_id: UUID, display_name: str) -> str:
    """Etiqueta desambiguada para cocina/staff. `display_name` no es único: si ya
    hay una "Ana" en la mesa, la siguiente se muestra como "Ana (2)"."""
    taken = set(db.execute(
        select(SessionParticipant.display_label).where(
            SessionParticipant.table_session_id == table_session_id
        )
    ).scalars())
    if display_name not in taken:
        return display_name
    n = 2
    while f"{display_name} ({n})" in taken:
        n += 1
    return f"{display_name} ({n})"


def open_session(
    db: Session, tenant_id: int, table: DiningTable, display_name: str
) -> SessionOpenResponse:
    """Une al comensal a la sesión activa de la mesa (o la abre si es el primero),
    crea su carrito y emite el token de sesión. Marca la mesa `ocupada`."""
    try:
        table_session = _get_or_create_table_session(db, table)

        participant = SessionParticipant(
            table_session_id=table_session.id,
            dining_table_id=table.id,
            display_name=display_name,
            display_label=unique_display_label(db, table_session.id, display_name),
            status="open",
            expires_at=_now() + timedelta(minutes=settings.SESSION_TTL_MINUTES),
        )
        db.add(participant)
        db.flush()

        cart = Cart(participant_id=participant.id, status="abierto")
        db.add(cart)

        paso_a_ocupada = table.status != "ocupada"
        if paso_a_ocupada:
            table.status = "ocupada"

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Error abriendo sesión de comensal")
        raise

    # Tras el COMMIT. Publica el servicio y no el router porque el router no sabe
    # si la mesa ya estaba ocupada, y repetir el evento con cada comensal que se
    # une haría parpadear el tablero del cajero sin motivo.
    if paso_a_ocupada:
        events.table_status_changed(
            tenant_id, dining_table_id=table.id, table_number=table.number,
            status="ocupada",
        )

    db.refresh(participant)
    db.refresh(cart)

    token = mint_session_token(
        tenant_id, table.id, participant.id, table_session.id,
        ttl_minutes=settings.SESSION_ABS_MAX_MINUTES,
    )
    return SessionOpenResponse(
        participant_id=participant.id,
        table_session_id=table_session.id,
        display_name=participant.display_name,
        display_label=participant.display_label,
        expires_at=participant.expires_at,
        table=SessionTableInfo.model_validate(table),
        cart_id=cart.id,
        session_token=token,
    )


# --------------------------------------------------------------- Carrito helpers

def _load_open_cart_or_none(db: Session, participant_id: UUID) -> Cart | None:
    return db.execute(
        select(Cart)
        .options(selectinload(Cart.items).selectinload(CartItem.options))
        .where(Cart.participant_id == participant_id, Cart.status == "abierto")
    ).scalar_one_or_none()


def _load_open_cart(db: Session, participant_id: UUID) -> Cart:
    cart = _load_open_cart_or_none(db, participant_id)
    if cart is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No hay carrito abierto para el comensal")
    return cart


def _get_or_create_open_cart(db: Session, participant_id: UUID) -> Cart:
    """Carrito 'abierto' vigente del comensal, creándolo si no existe. Al enviar
    un pedido el carrito anterior queda 'confirmado', así que el comensal reabre
    uno nuevo para la siguiente ronda. El índice parcial
    `idx_open_cart_per_participant` garantiza uno solo abierto por comensal."""
    cart = db.execute(
        select(Cart)
        .options(selectinload(Cart.items).selectinload(CartItem.options))
        .where(Cart.participant_id == participant_id, Cart.status == "abierto")
    ).scalar_one_or_none()
    if cart is None:
        cart = Cart(participant_id=participant_id, status="abierto")
        db.add(cart)
        db.commit()
        db.refresh(cart)
    return cart


def _chosen_from_cart_rows(db: Session, item: CartItem) -> list[ChosenOption]:
    """`ChosenOption` de una línea del carrito **leyendo la marca `per_line` de cada fila**
    (spec 089): la regla con la que se creó la línea manda, no el `pricing_type` vigente."""
    rows = {o.option_id: o for o in item.options}
    if not rows:
        return []
    options = db.execute(select(Option).where(Option.id.in_(rows.keys()))).scalars().all()
    return [ChosenOption(opt, rows[opt.id].quantity, rows[opt.id].per_line) for opt in options]


def _mark_addons(db: Session, options: list[ChosenOption]) -> list[ChosenOption]:
    """spec 089 (A-94) + spec 117 (A-126): marca `per_line=True` las opciones de grupos
    con recargo del Menú QR (089) y, en todo canal, las de un grupo "Por cantidad" (117)
    -- envoltorio de `mark_per_line` (`catalog_engine/pricing.py`), que generaliza esta
    función. Con `QR_ADDONS_PER_LINE=false` solo se apaga el eje 089; el eje nuevo de
    `selection_mode='cantidad'` nunca se apaga con esa bandera (research.md D1)."""
    return mark_per_line(db, options, qr_addons_per_line=settings.QR_ADDONS_PER_LINE)


def _cart_consumption(
    db: Session, cart: Cart, *, exclude_item_id: UUID | None = None
) -> dict[UUID, Decimal]:
    """Consumo agregado (por insumo) de todas las líneas del carrito, opcionalmente
    excluyendo una línea (para el caso de edición)."""
    total: dict[UUID, Decimal] = defaultdict(Decimal)
    for item in cart.items:
        if exclude_item_id is not None and item.id == exclude_item_id:
            continue
        chosen = _chosen_from_cart_rows(db, item)
        for iid, need in required_consumption(
            db, item.product_variant_id, item.quantity, chosen
        ).items():
            total[iid] += need
    return total


def _cart_promo_lines(db: Session, cart: Cart) -> list[dict]:
    """`promo_lines` para `evaluate_variant_sets` (spec 063,
    contracts/motor-y-persistencia.md §6). Un dict por ítem del carrito, en el
    mismo orden que `cart.items` — el índice es la clave de `by_line`.

    `base_unit_price` (spec 083, FR-027): `unit_price` sin el precio de los
    toppings elegidos (`CartItemOption` -> `Option.extra_price`), la base
    sobre la que un descuento `percent` debe aplicarse.

    spec 089 (D3): solo se restan las opciones `per_line=False`. En una línea nueva del
    Menú QR el `unit_price` ya no contiene los adicionales (van en `addons_total`), así
    que la base es el propio `unit_price` y una promoción nunca descuenta un adicional."""
    rows = db.execute(
        select(ProductVariant.id, ProductVariant.active)
        .where(ProductVariant.id.in_({it.product_variant_id for it in cart.items}))
    ).all()
    active = {r.id: bool(r.active) for r in rows}

    option_ids = {o.option_id for it in cart.items for o in it.options}
    extra_prices = dict(db.execute(
        select(Option.id, Option.extra_price).where(Option.id.in_(option_ids))
    ).all()) if option_ids else {}

    promo_lines: list[dict] = []
    for it in cart.items:
        unit_price = Decimal(it.unit_price)
        toppings = sum(
            (
                Decimal(extra_prices.get(o.option_id, 0)) * o.quantity
                for o in it.options if not o.per_line
            ),
            Decimal(0),
        )
        promo_lines.append({
            "product_variant_id": it.product_variant_id,
            "unit_price": unit_price,
            "base_unit_price": unit_price - toppings,
            "quantity": it.quantity,
            "line_id": it.id,
            "combo_id": it.combo_id,
            "_variant_active": active.get(it.product_variant_id, False),
            "description": "",
        })
    return promo_lines


def serialize_cart(
    db: Session, cart: Cart, participant: SessionParticipant, tenant: Tenant | None = None
) -> CartResponse:
    now = datetime.now(timezone.utc)
    # spec 063: motor por conjunto de variantes, evaluado UNA sola vez sobre las
    # líneas del carrito. No se persiste (FR-020); se recalcula en cada `GET /cart`.
    # spec 127: `tenant`, cuando el caller lo tiene resuelto (siempre,
    # `SessionContext.tenant`), evalúa la vigencia en SU zona horaria real.
    result = promotions.evaluate_variant_sets(db, _cart_promo_lines(db, cart), now, tenant)

    items: list[CartItemResponse] = []
    total = Decimal("0")
    any_discount = False
    for idx, it in enumerate(cart.items):
        # spec 089: `unit_price × quantity + addons_total` (con `addons_total = 0` es la
        # fórmula de siempre para toda línea histórica).
        line_total = compute_line_total(it.unit_price, it.quantity, it.addons_total)
        total += line_total
        discounted_unit_price, discounted_line_total = line_discount_snapshot(
            result, idx, line_total, it.quantity, it.addons_total,
        )
        if discounted_line_total is not None:
            any_discount = True
        items.append(CartItemResponse(
            id=it.id,
            product_variant_id=it.product_variant_id,
            quantity=it.quantity,
            unit_price=it.unit_price,
            addons_total=it.addons_total,
            line_total=line_total,
            discounted_unit_price=discounted_unit_price,
            discounted_line_total=discounted_line_total,
            notes=it.notes,
            combo_id=it.combo_id,
            options=[CartItemOptionResponse.model_validate(o) for o in it.options],
        ))

    discounted_total = total - result.total
    return CartResponse(
        id=cart.id, participant_id=cart.participant_id, status=cart.status,
        display_name=participant.display_name,
        display_label=participant.display_label,
        total=total,
        discounted_total=discounted_total if (any_discount or result.total > 0) else None,
        items=items,
    )


def get_cart(db: Session, participant_id: UUID, tenant: Tenant | None = None) -> CartResponse:
    """Carrito abierto del comensal, con su nombre. El nombre va en la respuesta
    porque el token de sesión no lo lleva: al recargar el menú, `GET /cart` es lo
    único que permite al front repintar el saludo."""
    participant = get_or_404(db, SessionParticipant, participant_id, "Comensal no encontrado")
    return serialize_cart(db, _get_or_create_open_cart(db, participant_id), participant, tenant)


# --------------------------------------------------------------- CRUD de líneas

def add_item(
    db: Session, participant_id: UUID, data: CartItemIn, tenant: Tenant | None = None
) -> CartResponse:
    cart = _get_or_create_open_cart(db, participant_id)

    variant = get_or_404(db, ProductVariant, data.product_variant_id, "Variant not found")
    if not variant.active:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Variante inactiva: {variant.id}")
    ensure_product_available(variant)

    options = _mark_addons(
        db, load_valid_options(db, data.options, variant=variant, quantity=data.quantity)
    )

    # Disponibilidad: consumo del carrito actual + la línea nueva.
    required = _cart_consumption(db, cart)
    for iid, need in required_consumption(db, variant.id, data.quantity, options).items():
        required[iid] += need
    check_availability(db, required, extra_context="carrito")

    try:
        item = CartItem(
            cart_id=cart.id,
            product_variant_id=variant.id,
            quantity=data.quantity,
            # spec 089 (A-94): precio de UNA unidad + adicionales cobrados una vez por línea.
            unit_price=compute_unit_price(variant, options),
            addons_total=compute_addons_total(options),
            notes=data.notes,
        )
        db.add(item)
        db.flush()
        for chosen in options:
            db.add(CartItemOption(
                cart_item_id=item.id, option_id=chosen.option.id, quantity=chosen.quantity,
                per_line=chosen.per_line,
            ))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Error agregando ítem al carrito")
        raise

    return get_cart(db, participant_id, tenant)


def _auto_adjust_cantidad_options(
    db: Session, options: list[ChosenOption], new_qty: int
) -> tuple[list[ChosenOption], list[AdjustedOptionOut]]:
    """Spec 117 (A-126, D4/D5): al reducir la cantidad de producto de una línea con
    adiciones "Por cantidad" ya elegidas, reduce automáticamente las que excedan su
    tope ya escalado por la nueva cantidad (contracts/cart-item-auto-adjust.md).

    Un grupo sin ningún tope configurado nunca entra aquí (FR-008 de spec 065 sigue
    vigente sin cambios). Un grupo "conteo" nunca entra aquí (no tiene topes de
    cantidad -- su límite es `min_select`/`max_select`, ajeno a este algoritmo)."""
    if not options:
        return options, []
    group_ids = {c.option.option_group_id for c in options}
    groups = {
        g.id: g for g in db.execute(
            select(OptionGroup).where(OptionGroup.id.in_(group_ids))
        ).scalars()
    }
    result = list(options)
    by_group: dict[UUID, list[int]] = defaultdict(list)
    for idx, chosen in enumerate(result):
        group = groups.get(chosen.option.option_group_id)
        if group is not None and group.selection_mode == "cantidad":
            by_group[group.id].append(idx)

    adjusted: list[AdjustedOptionOut] = []
    for gid, idxs in by_group.items():
        group = groups[gid]
        previous = {i: result[i].quantity for i in idxs}

        # Paso 2 (D4): tope individual escalado.
        if group.max_quantity_per_option is not None:
            tope = group.max_quantity_per_option * new_qty
            for i in idxs:
                if result[i].quantity > tope:
                    result[i] = result[i]._replace(quantity=tope)

        # Paso 3 (D4): tope total escalado, orden alfabético descendente sobre el
        # nombre normalizado, desempate por option_id, una unidad a la vez.
        if group.max_total_quantity is not None:
            tope_total = group.max_total_quantity * new_qty
            total = sum(result[i].quantity for i in idxs)
            if total > tope_total:
                ordered = sorted(
                    idxs,
                    key=lambda i: (result[i].option.name.casefold(), str(result[i].option.id)),
                    reverse=True,
                )
                pos = 0
                while total > tope_total and pos < len(ordered):
                    i = ordered[pos]
                    if result[i].quantity <= 0:
                        pos += 1
                        continue
                    result[i] = result[i]._replace(quantity=result[i].quantity - 1)
                    total -= 1
                    if result[i].quantity <= 0:
                        pos += 1

        for i in idxs:
            if result[i].quantity != previous[i]:
                adjusted.append(AdjustedOptionOut(
                    option_id=result[i].option.id,
                    option_name=result[i].option.name,
                    previous_quantity=previous[i],
                    new_quantity=result[i].quantity,
                ))
    return result, adjusted


def update_item(
    db: Session,
    participant_id: UUID,
    item_id: UUID,
    data: CartItemUpdate,
    tenant: Tenant | None = None,
) -> CartResponse:
    cart = _load_open_cart(db, participant_id)
    item = next((i for i in cart.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Línea de carrito no encontrada")

    new_qty = data.quantity if data.quantity is not None else item.quantity
    variant = get_or_404(db, ProductVariant, item.product_variant_id, "Variant not found")
    ensure_product_available(variant)

    adjusted_options: list[AdjustedOptionOut] = []
    if data.options is not None:
        # Editar la selección adopta la regla nueva (spec 089: líneas nuevas o editadas).
        options = _mark_addons(
            db, load_valid_options(db, data.options, variant=variant, quantity=new_qty)
        )
    else:
        # Selección ya guardada: no se revalida, o un cambio de min/max en el catálogo
        # impediría hasta bajar la cantidad de una línea que ya estaba en el carrito.
        # spec 089: conserva la marca `per_line` de cada fila (una línea histórica sigue
        # con la regla histórica al cambiar solo la cantidad).
        options = _chosen_from_cart_rows(db, item)
        if new_qty < item.quantity:
            # Spec 117 (A-126, D4): único camino de backend donde puede reducirse la
            # cantidad de una línea ya creada con adiciones "cantidad" ya elegidas.
            options, adjusted_options = _auto_adjust_cantidad_options(db, options, new_qty)

    # Disponibilidad: resto del carrito (sin esta línea) + la línea editada.
    required = _cart_consumption(db, cart, exclude_item_id=item_id)
    for iid, need in required_consumption(db, variant.id, new_qty, options).items():
        required[iid] += need
    check_availability(db, required, extra_context="carrito")

    try:
        item.quantity = new_qty
        item.unit_price = compute_unit_price(variant, options)
        item.addons_total = compute_addons_total(options)
        if data.notes is not None:
            item.notes = data.notes
        if data.options is not None:
            for o in list(item.options):
                db.delete(o)
            db.flush()
            for chosen in options:
                db.add(CartItemOption(
                    cart_item_id=item.id, option_id=chosen.option.id, quantity=chosen.quantity,
                    per_line=chosen.per_line,
                ))
        elif adjusted_options:
            # Spec 117 (A-126, D4): mismas filas de siempre (no se tocó `data.options`),
            # solo se corrige la cantidad de las que el ajuste automático redujo.
            rows_by_option = {o.option_id: o for o in item.options}
            for a in adjusted_options:
                rows_by_option[a.option_id].quantity = a.new_quantity
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Error editando ítem del carrito")
        raise

    response = get_cart(db, participant_id, tenant)
    if adjusted_options:
        # Spec 117 (A-126, D5): solo en la respuesta de ESTE `PATCH` -- `GET /cart`
        # (vía `get_cart` arriba) y cualquier lectura posterior nunca lo repiten.
        for out_item in response.items:
            if out_item.id == item_id:
                out_item.adjusted_options = adjusted_options
                break

    return response


def list_my_orders(db: Session, participant_id: UUID) -> list[CustomerOrder]:
    """Pedidos que envió este comensal, para restaurar la UI al reingresar. Se
    devuelve desde el backend en vez de reconstruirlo con estado local del
    navegador, que es justo lo que se pierde al recargar o cambiar de móvil."""
    return db.execute(
        select(CustomerOrder)
        .options(
            selectinload(CustomerOrder.items).selectinload(OrderItem.options),
            selectinload(CustomerOrder.payment_attempts)
            .selectinload(OrderPaymentAttempt.payment_method),
        )
        .where(CustomerOrder.participant_id == participant_id)
        .order_by(CustomerOrder.created_at.desc())
    ).scalars().all()


def cancel_my_order(
    db: Session, participant: SessionParticipant, order_id: UUID, motivo: str,
    tenant_id: int | None = None,
    request_id: str | None = None,
) -> CustomerOrder:
    """Cancelación por el propio comensal. Solo hasta que cocina empieza:

    - `recibida`: siempre (no se descontó nada);
    - `abierta`: solo si **todos** sus ítems siguen `pendiente`;
    - a partir de `en_preparacion` el insumo ya se combinó y la decisión de cuánto
      se pierde es del staff, no del cliente.

    Delega la política de inventario en `checkout.cancel_order`, que ya sabe qué
    revertir y qué contar como pérdida — y que emite el evento de auditoría
    `order.cancelled` con actor `comensal` (spec 074), por lo que aquí solo se
    le reenvía el `tenant_id` que el router ya resolvió — y, desde la extensión
    de logging operativo (FR-021), también el `request_id` de la petición."""
    from app.api.v1.orders import checkout

    order = db.execute(
        select(CustomerOrder)
        .options(selectinload(CustomerOrder.items))
        .where(CustomerOrder.id == order_id)
    ).scalar_one_or_none()
    if order is None or order.participant_id != participant.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pedido no encontrado")

    if order.status in checkout.TERMINAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"El pedido ya es terminal (status={order.status})"
        )
    if order.status != "recibida":
        en_cocina = [
            it for it in order.items
            if it.estado_cocina not in ("pendiente", "anulado")
        ]
        if en_cocina or order.status != "abierta":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "El pedido ya está en preparación; pide al personal que lo cancele.",
            )

    result = checkout.cancel_order(
        db, order_id, CancelIn(motivo=motivo), user=None, participant=participant,
        tenant_id=tenant_id, request_id=request_id,
    )

    # Si era el último pedido y el comensal ya se había ido, la mesa queda vacía:
    # cubre el "pedí, me arrepentí y me fui" sin esperar al barrido.
    closed = try_release_if_empty(db, participant.table_session_id)
    if closed:
        db.commit()
        # spec 089 (A-95): el aviso sale DESPUÉS del commit, nunca antes.
        notify_sessions_closed(tenant_id, closed, reason="empty")

    return result


def leave_session(
    db: Session, participant: SessionParticipant, tenant_id: int | None = None
) -> None:
    """El comensal se va de la mesa.

    Cierra su carrito y, si era el último y no quedó nada que cobrar, libera la
    mesa en el acto. No falla nunca: salir es idempotente y el comensal ya se está
    marchando — un error aquí no le sirve de nada a nadie.
    """
    close_participant(db, participant)
    closed = try_release_if_empty(db, participant.table_session_id)
    db.commit()
    # spec 089 (A-95): si su salida dejó la mesa vacía y se liberó, el aviso sale DESPUÉS del commit.
    notify_sessions_closed(tenant_id, closed, reason="empty")


def submit_cart(
    db: Session,
    participant: SessionParticipant,
    payment_method_id: UUID,
    receipt_file_url: str | None = None,
    tenant_id: int | None = None,
    request_id: str | None = None,
    tenant_schema: str | None = None,
    tenant: Tenant | None = None,
) -> CustomerOrder:
    """Envía el carrito del comensal como pedido: crea una `CustomerOrder` en
    estado `recibida` con sus líneas **y** su primer `OrderPaymentAttempt`, en
    una sola transacción (spec 025, contracts/submit-cart-with-payment.md).
    Antes de esta llamada no existe ningún registro del pedido — no hay
    ningún estado intermedio "sin pago" que el staff pueda ver
    (research.md spec 025, Decisión 1/3).

    **No toca inventario.** El descuento ocurre cuando el staff confirma el pedido
    (`POST /orders/{id}/confirm`); hasta entonces el comensal puede cancelarlo sin
    ningún movimiento de stock. El chequeo de disponibilidad de aquí es preventivo
    (sin lock ni reserva): la validación real y atómica es la de la confirmación.

    El comensal puede enviar varios pedidos en la misma sesión: al confirmar,
    este carrito se elimina físicamente y `_get_or_create_open_cart` le abre
    otro, limpio, para la siguiente ronda (spec 038, FR-003/FR-004).

    `tenant_id` (spec 074) es solo para el log de auditoría: el comensal no es
    un `User`, así que el tenant lo pasa el router, que ya lo tiene resuelto
    por el token firmado. Opcional para no cambiar el contrato de ningún
    llamador existente.

    `tenant_schema` (spec 088, FR-008) es el esquema del negocio contra el que se
    valida el comprobante de un método que no es efectivo (prefijo del negocio +
    carpeta `comprobantes` + existencia en R2); en base de datos se guarda la
    **key**. Obligatorio solo cuando llega un comprobante.

    `tenant` (spec 127) es el objeto `Tenant` completo (distinto de `tenant_id`,
    que solo es su id para el log) — el router ya lo tiene resuelto
    (`ctx.tenant`); se reenvía al motor de promociones para evaluar la
    vigencia en la zona horaria real del tenant en vez del respaldo global."""
    cart = _load_open_cart_or_none(db, participant.id)

    # spec 024, FR-005: no permitir una segunda orden activa del mismo
    # comensal. "Activa" = no terminal (research.md spec 024, Decisión 8).
    # spec 025: además de este chequeo de aplicación (mensaje claro en el
    # camino feliz), `idx_active_order_per_participant` es la garantía de
    # última instancia ante una confirmación duplicada casi simultánea
    # (FR-013, research.md Decisión 4) — capturada más abajo, alrededor del
    # `commit()`.
    #
    # spec 038, FR-007 (research.md Decisión 3): se calcula **antes** de
    # decidir qué error devolver, porque tras cualquier confirmación exitosa
    # (FR-003 ya eliminó la fila del carrito) un segundo intento cae en
    # `cart is None` — sin este orden, ese caso —el más frecuente en
    # producción— mostraría el 404/409 genérico de "no hay carrito", no el
    # mensaje explícito de "ya fue enviado".
    active_order = db.execute(
        select(CustomerOrder.id).where(
            CustomerOrder.participant_id == participant.id,
            CustomerOrder.status.in_(_NON_TERMINAL_ORDER_STATUSES),
        )
    ).scalar_one_or_none()

    if cart is None or not cart.items:
        if active_order is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Tu pedido ya fue enviado; revisa el estado en Mis pedidos.",
            )
        if cart is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "No hay carrito abierto para el comensal"
            )
        raise HTTPException(status.HTTP_409_CONFLICT, "El carrito está vacío")

    if active_order is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Ya tienes una orden activa; espera a que finalice antes de enviar otra.",
        )

    # spec 028, FR-013 (T015): simetría inversa de la comanda de
    # mostrador/mesero (`orders.service.create_order`, T014) — una mesa no
    # mezcla orígenes de pedido a la vez. Si el staff ya abrió una comanda
    # manual activa en esta sesión, el pedido por QR no puede coexistir con
    # ella.
    active_manual_order = db.execute(
        select(CustomerOrder.id).where(
            CustomerOrder.table_session_id == participant.table_session_id,
            CustomerOrder.channel == "POS",
            CustomerOrder.status.in_(_NON_TERMINAL_ORDER_STATUSES),
        )
    ).scalar_one_or_none()
    if active_manual_order is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Esta mesa ya tiene una comanda de mostrador/mesero activa; "
            "ciérrala antes de enviar un pedido por QR.",
        )

    method = get_or_404(db, PaymentMethod, payment_method_id, "Método de pago no encontrado")
    if not method.active:
        raise HTTPException(status.HTTP_409_CONFLICT, "El método de pago ya no está disponible")
    if method.is_cash:
        if receipt_file_url is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Un pago en efectivo no lleva comprobante",
            )
    elif not receipt_file_url:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Este método de pago exige cargar un comprobante",
        )
    else:
        # spec 088 (FR-008): el comprobante deja de ser texto libre. Se valida DESPUÉS de los
        # 422 de arriba y ANTES de comprobar disponibilidad y de crear nada: un 422/503 no crea
        # orden ni intento ni borra el carrito. Se persiste (y se audita) la key resuelta.
        receipt_file_url = resolve_receipt_key(receipt_file_url, _require_receipt_schema(tenant_schema))

    variants = [
        get_or_404(db, ProductVariant, i.product_variant_id, "Variant not found")
        for i in cart.items
    ]

    # spec 093 (FR-021/FR-022, escenario 7): si algún producto del carrito se
    # marcó agotado DESPUÉS de que el comensal lo agregara, se rechaza el envío
    # completo aquí -- nombrando todos los agotados a la vez -- antes de crear
    # la orden; el carrito no se toca, así que el comensal puede ajustarlo.
    ensure_products_available(variants)

    # spec 128 (FR-007, research.md Decisión 5): si la categoría de un
    # producto dejó de ser visible en QR (se ocultó o se desactivó) DESPUÉS
    # de que el comensal lo agregara al carrito, se rechaza el envío --
    # pero a diferencia de agotados, aquí el carrito SÍ se actualiza: las
    # líneas afectadas se eliminan como parte de esta misma llamada, para
    # que el comensal no tenga que encontrarlas y quitarlas él mismo. Ningún
    # pedido se crea en esta llamada.
    items_by_variant_id = {i.product_variant_id: i for i in cart.items}
    ocultos = {
        (v.product.name): items_by_variant_id[v.id]
        for v in variants
        if not (v.product.category.active and v.product.category.visible_en_qr)
    }
    if ocultos:
        for item in ocultos.values():
            db.delete(item)
        db.commit()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "Algunos productos ya no están disponibles y se quitaron de tu carrito",
                "productos_no_disponibles": sorted(ocultos.keys()),
            },
        )

    check_availability(db, _cart_consumption(db, cart), extra_context="envío de pedido")

    # spec 087 (FR-006, A-86): segundo punto de creación de CustomerOrder que
    # necesita la numeración estable de pedidos de mesa -- mismo criterio de
    # "segundo punto de creación" que ya usa spec 073 abajo, mismo helper que
    # `orders/service.py::create_order`.
    cash_shift_id, table_order_number = resolve_dine_in_table_order(db)

    try:
        order = CustomerOrder(
            table_session_id=participant.table_session_id,
            participant_id=participant.id,
            dining_table_id=participant.dining_table_id,
            customer_name=participant.display_label or participant.display_name,
            channel="QR_MENU",
            order_type="DINE_IN",
            status="recibida",
            user_id=None,  # lo envió el comensal, no un usuario del sistema
            cash_shift_id=cash_shift_id,
            table_order_number=table_order_number,
            # spec 073 (FR-008/FR-018, A-70): segundo punto de creación de
            # CustomerOrder — el flujo del carrito QR también congela su instante
            # de vigencia de promociones (aware UTC), igual que
            # `orders/service.py::create_order` (research.md D2).
            promotion_evaluated_at=datetime.now(timezone.utc),
        )
        db.add(order)
        db.flush()

        # spec 038, FR-002/FR-013 (research.md D7): mismo motor que
        # `serialize_cart`/`GET /cart`, evaluado una sola vez — el snapshot de
        # descuento del pedido recién confirmado debe coincidir exactamente con
        # lo que el carrito mostraba justo antes.
        now = datetime.now(timezone.utc)
        result = promotions.evaluate_variant_sets(db, _cart_promo_lines(db, cart), now, tenant)

        for idx, ci in enumerate(cart.items):
            line_total = compute_line_total(ci.unit_price, ci.quantity, ci.addons_total)
            discounted_unit_price, discounted_line_total = line_discount_snapshot(
                result, idx, line_total, ci.quantity, ci.addons_total,
            )
            item = OrderItem(
                order_id=order.id,
                participant_id=participant.id,
                product_variant_id=ci.product_variant_id,
                quantity=ci.quantity,
                unit_price=ci.unit_price,  # snapshot copiado del carrito
                addons_total=ci.addons_total,  # spec 089: copiado, nunca recalculado
                discounted_unit_price=discounted_unit_price,
                discounted_line_total=discounted_line_total,
                notes=ci.notes,
                combo_id=ci.combo_id,
                estado_cocina="pendiente",
            )
            db.add(item)
            db.flush()
            for o in ci.options:
                db.add(OrderItemOption(
                    order_item_id=item.id, option_id=o.option_id, quantity=o.quantity,
                    per_line=o.per_line,
                ))

        db.add(OrderPaymentAttempt(
            order_id=order.id,
            payment_method_id=payment_method_id,
            receipt_file_url=receipt_file_url,
        ))

        # spec 038, FR-003/FR-004: el carrito se elimina físicamente (no se
        # archiva) en la misma transacción del pedido — la cascada ya
        # declarada (`Cart.items`, `ondelete="CASCADE"` en `cart_items`/
        # `cart_item_options`) se lleva sus líneas sin código adicional
        # (research.md Decisión 1). Si algo de lo anterior falla, el
        # `except`/`rollback()` de abajo deshace también este borrado.
        db.delete(cart)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        # Carrera entre dos envíos casi simultáneos del mismo comensal
        # (FR-013) — `idx_active_order_per_participant` la resolvió por
        # nosotros; se traduce al mismo 409 del chequeo de aplicación de
        # arriba, sin dejar ningún pedido a medias.
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Ya tienes una orden activa; espera a que finalice antes de enviar otra.",
        )
    except Exception:
        db.rollback()
        logger.exception("Error enviando el pedido del comensal")
        raise

    # spec 074 (FR-001/FR-010): el pedido ya está comprometido. El nombre del
    # comensal nunca viaja en texto plano — solo su HMAC (FR-005).
    audit_actor = OrderAuditActor(type=ActorType.COMENSAL, id=str(participant.id))
    record_order_audit_event(
        event_type=OrderAuditEventType.ORDER_CREATED,
        order_id=order.id,
        tenant_id=tenant_id,
        actor=audit_actor,
        details={
            "channel": "QR_MENU",
            "order_type": order.order_type,
            "diner_name_hash": hash_sensitive_or_none(
                participant.display_name or participant.display_label
            ),
        },
        request_id=request_id,
    )
    # El pedido nace con su primer intento de pago en esta misma transacción
    # (spec 025), así que ese intento también se audita aquí: es el único
    # punto donde se registra el del flujo QR — `create_payment_attempt` solo
    # cubre los reintentos posteriores a un rechazo.
    record_order_audit_event(
        event_type=OrderAuditEventType.PAYMENT_ATTEMPT_CREATED,
        order_id=order.id,
        tenant_id=tenant_id,
        actor=audit_actor,
        details={
            "payment_method_type": method.type,
            "payment_method_name": method.name,
            "receipt_hash": hash_sensitive_or_none(receipt_file_url),
        },
        request_id=request_id,
    )

    return db.execute(
        select(CustomerOrder)
        .options(
            selectinload(CustomerOrder.items).selectinload(OrderItem.options),
            selectinload(CustomerOrder.payment_attempts)
            .selectinload(OrderPaymentAttempt.payment_method),
        )
        .where(CustomerOrder.id == order.id)
    ).scalar_one()


def remove_item(
    db: Session, participant_id: UUID, item_id: UUID, tenant: Tenant | None = None
) -> CartResponse:
    cart = _load_open_cart(db, participant_id)
    item = next((i for i in cart.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Línea de carrito no encontrada")
    db.delete(item)
    db.commit()
    return get_cart(db, participant_id, tenant)


# --------------------------------------------------------- Pagos del comensal (spec 024)

def list_payment_methods(db: Session) -> list[PaymentMethod]:
    """Métodos de pago activos del tenant (FR-004) — un método desactivado
    nunca aparece aquí, aunque el comensal ya tuviera la pantalla abierta.
    Carga `catalog` de una vez (spec 034): `DinerPaymentMethod.fields` lo lee
    de ahí vía `PaymentMethod.fields` (propiedad, sin columna propia)."""
    return db.execute(
        select(PaymentMethod)
        .options(selectinload(PaymentMethod.catalog))
        .where(PaymentMethod.active.is_(True))
        .order_by(PaymentMethod.name)
    ).scalars().all()


def presign_payment_receipt(
    db: Session, tenant_schema: str, participant_id: UUID, content_type: str
) -> ReceiptPresignOut:
    """Presign genérico, sin ligar a ningún `attempt_id` (spec 025, US3/US4,
    contracts/payment-receipt-presign.md, research.md Decisión 2) — a
    diferencia de `presign_receipt`, se usa *antes* de que exista cualquier
    orden o intento al que asociar el archivo. Mismas primitivas de
    `app/core/storage.py`, sin ninguna validación de recurso previo: el único
    comensal autenticado (`participant_id`, del `x-session-token`) ya es
    suficiente contexto."""
    extension = CONTENT_TYPE_EXTENSIONS.get(content_type)
    if extension is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"content_type no soportado: {content_type}",
        )

    key = build_object_key(tenant_schema, "comprobantes", extension)
    return ReceiptPresignOut(
        upload_url=generate_presigned_put_url(key, content_type),
        key=key,
        public_url=public_url_for(key),
        expires_in=settings.R2_PRESIGN_EXPIRE_SECONDS,
    )


def _load_own_order(db: Session, participant_id: UUID, order_id: UUID) -> CustomerOrder:
    order = db.get(CustomerOrder, order_id)
    if order is None or order.participant_id != participant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pedido no encontrado")
    return order


def create_payment_attempt(
    db: Session, participant_id: UUID, order_id: UUID, payment_method_id: UUID,
    tenant_id: int | None = None,
    request_id: str | None = None,
) -> OrderPaymentAttempt:
    """Crea un intento de pago nuevo para una orden propia (FR-008 efectivo,
    FR-012 transferencia). FR-015a: no se permite si ya hay uno `pendiente`
    — el índice único parcial `idx_pending_payment_attempt_per_order` es la
    garantía última; este chequeo solo da un 409 con mensaje claro en vez de
    dejar que la violación de índice llegue como error crudo."""
    order = _load_own_order(db, participant_id, order_id)
    if order.status != "recibida":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"La orden no admite un nuevo intento de pago (status={order.status})",
        )
    method = get_or_404(db, PaymentMethod, payment_method_id, "Método de pago no encontrado")

    existing_pending = db.execute(
        select(OrderPaymentAttempt.id).where(
            OrderPaymentAttempt.order_id == order_id,
            OrderPaymentAttempt.status == "pendiente",
        )
    ).scalar_one_or_none()
    if existing_pending is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Ya hay un intento de pago pendiente de revisión para esta orden",
        )

    try:
        attempt = OrderPaymentAttempt(order_id=order_id, payment_method_id=payment_method_id)
        db.add(attempt)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Error creando intento de pago")
        raise

    db.refresh(attempt)

    # spec 074 (FR-001/FR-010): el intento ya existe. El comprobante (que en
    # este camino se adjunta después, vía `attach_receipt`) nunca viaja en
    # texto plano — solo su HMAC, si ya estuviera presente (FR-005/FR-012).
    record_order_audit_event(
        event_type=OrderAuditEventType.PAYMENT_ATTEMPT_CREATED,
        order_id=order_id,
        tenant_id=tenant_id,
        actor=OrderAuditActor(type=ActorType.COMENSAL, id=str(participant_id)),
        details={
            "payment_method_type": method.type,
            "payment_method_name": method.name,
            "receipt_hash": hash_sensitive_or_none(attempt.receipt_file_url),
        },
        request_id=request_id,
    )
    return attempt


def _load_own_pending_attempt(
    db: Session, participant_id: UUID, attempt_id: UUID
) -> OrderPaymentAttempt:
    attempt = db.get(OrderPaymentAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Intento de pago no encontrado")
    order = db.get(CustomerOrder, attempt.order_id)
    if order is None or order.participant_id != participant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Intento de pago no encontrado")
    if attempt.status != "pendiente":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"El intento de pago ya fue resuelto (status={attempt.status})",
        )
    return attempt


def presign_receipt(
    db: Session, tenant_schema: str, participant_id: UUID, attempt_id: UUID, content_type: str
) -> ReceiptPresignOut:
    """Presign directo contra `app/core/storage.py`, sin pasar por
    `POST /uploads/presign` (gateado por `require_tenant_admin`, inaccesible
    para el comensal — research.md spec 024, Decisión 6)."""
    attempt = _load_own_pending_attempt(db, participant_id, attempt_id)
    method = get_or_404(
        db, PaymentMethod, attempt.payment_method_id, "Método de pago no encontrado"
    )
    if method.is_cash:
        raise HTTPException(status.HTTP_409_CONFLICT, "Un pago en efectivo no lleva comprobante")

    extension = CONTENT_TYPE_EXTENSIONS.get(content_type)
    if extension is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"content_type no soportado: {content_type}",
        )

    key = build_object_key(tenant_schema, "comprobantes", extension)
    return ReceiptPresignOut(
        upload_url=generate_presigned_put_url(key, content_type),
        key=key,
        public_url=public_url_for(key),
        expires_in=settings.R2_PRESIGN_EXPIRE_SECONDS,
    )


def _require_receipt_schema(tenant_schema: str | None) -> str:
    if not tenant_schema:
        raise ValueError("tenant_schema es obligatorio para validar el comprobante")
    return tenant_schema


def attach_receipt(
    db: Session, participant_id: UUID, attempt_id: UUID, file_url: str,
    *, tenant_schema: str | None = None,
) -> OrderPaymentAttempt:
    """Asocia el archivo ya subido a R2 con el intento (FR-012). Un
    comprobante por intento — un reintento crea un intento nuevo, no
    reemplaza el archivo de uno existente.

    spec 088 (FR-008): el `409` "ya tiene comprobante" se evalúa **antes** que la validación;
    luego el comprobante debe ser un archivo existente de la carpeta `comprobantes` del propio
    negocio (`tenant_schema`), y se guarda la **key**."""
    attempt = _load_own_pending_attempt(db, participant_id, attempt_id)
    method = get_or_404(
        db, PaymentMethod, attempt.payment_method_id, "Método de pago no encontrado"
    )
    if method.is_cash:
        raise HTTPException(status.HTTP_409_CONFLICT, "Un pago en efectivo no lleva comprobante")
    if attempt.receipt_file_url:
        raise HTTPException(status.HTTP_409_CONFLICT, "El intento ya tiene un comprobante adjunto")

    receipt_key = resolve_receipt_key(file_url, _require_receipt_schema(tenant_schema))

    try:
        attempt.receipt_file_url = receipt_key
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Error adjuntando comprobante de pago")
        raise

    db.refresh(attempt)
    return attempt
