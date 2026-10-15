"""Service de comandas del staff (mostrador/mesero): crea una `customer_order`
con sus líneas y opciones, tomando un snapshot del precio.

**Consume inventario al crear, salvo `hold_for_payment`.** Por defecto la
comanda nace en `abierta`, o sea ya confirmada: no vuelve a pasar por
`confirm_order`, así que si no descontara aquí no descontaría nunca —cobrarla
tampoco lo hace— y el stock quedaría sobrestimado en silencio. Es el mismo
punto de descuento que la confirmación, por la otra puerta.

Con `hold_for_payment=True` (spec 028, T013) la comanda nace `recibida` en su
lugar, sin tocar inventario: el staff cobra primero
(`checkout.checkout_and_send`) y ese es el nuevo punto de descuento para este
camino — mismo criterio de "un único punto de descuento", solo que movido.

El pedido anónimo por QR **no** entra por aquí: llega como `recibida` vía
`/cart/submit` y lo descuenta el staff al confirmarlo."""
import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from math import ceil
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import cast, exists, func, or_, select
from sqlalchemy.types import String
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.sql import Select

from app.core.crud import get_or_404
from app.core.order_audit import (
    ActorType,
    OrderAuditActor,
    OrderAuditEventType,
    record_order_audit_event,
)
from app.models.product_variant import ProductVariant
from app.models.option import Option
from app.catalog_engine import ChosenOption, compute_addons_total, compute_unit_price, mark_per_line
from app.models.session_participant import SessionParticipant
from app.models.dining_table import DiningTable
from app.models.customer_order import CustomerOrder
from app.models.order_item import OrderItem, OrderItemOption
from app.models.order_payment_attempt import OrderPaymentAttempt
from app.models.payment import Payment, PaymentMethod
from app.models.sale import Sale
from app.models.table_session import TableSession
from app.core.models import User
from app.api.v1.catalog.line_pricing import (
    ensure_products_available,
    load_valid_options,
)
from app.api.v1.cash.service import resolve_dine_in_table_order
from app.api.v1.orders.consolidation import get_or_create_table_session_id
from app.api.v1.orders.consumption import deduct_order_items_naming_product
from app.api.v1.orders.schemas import (
    AppliedPromotionOut,
    OrderBillingSummary,
    OrderChannel,
    OrderCreate,
    OrderType,
)

logger = logging.getLogger(__name__)

#: Estados en los que un pedido todavía "está pasando" por la mesa: ni
#: recién llegado y sin resolver, ni terminal. Mismo conjunto que
#: `cart.service._NON_TERMINAL_ORDER_STATUSES` — se repite aquí (en vez de
#: importarlo) porque cada módulo de este paquete define el suyo (ver
#: `checkout.TERMINAL`), y `cart.service` ya importa de `table_sessions.service`,
#: que a su vez depende de `orders.checkout`.
_NON_TERMINAL_ORDER_STATUSES = ("recibida", "abierta", "bloqueada")

#: Combinaciones canal × tipo de orden con sentido de negocio (spec 055,
#: FR-006; research.md D4) — validado solo aquí: es el único punto donde
#: ambos valores llegan como datos arbitrarios de un llamador (`OrderCreate`).
#: El flujo QR (`cart.service.submit_cart`) y el de consolidación/mesero
#: (`orders.consolidation.get_or_create_open_order`) construyen su propia
#: combinación fija y ya válida por construcción, sin pasar por aquí.
_COMBINACIONES_CANAL_TIPO_ORDEN: dict[OrderChannel, frozenset[OrderType]] = {
    OrderChannel.POS: frozenset({OrderType.DINE_IN, OrderType.TAKEAWAY, OrderType.DELIVERY}),
    OrderChannel.QR_MENU: frozenset({OrderType.DINE_IN}),
    OrderChannel.WHATSAPP: frozenset({OrderType.TAKEAWAY, OrderType.DELIVERY}),
    OrderChannel.API: frozenset({OrderType.TAKEAWAY, OrderType.DELIVERY}),
}

#: Nombre del cliente que recibe un pedido Mesa/Para llevar de la Terminal cuando el cajero no
#: escribe ninguno (spec 091, A-99) -- distinto y sin relación con el "Consumidor Final" (F
#: mayúscula) que ya usa `checkout.py` como nombre por defecto de la *factura* (research.md D1).
DEFAULT_CUSTOMER_NAME = "Consumidor final"


def order_has_sale(db: Session, order_id: UUID) -> bool:
    """¿Ya existe una `Sale` para este pedido? (spec 029, D2/D3 de research.md)

    Es la señal correcta de "ya está pagado" — a diferencia de
    `CustomerOrder.status`, que nunca llega a `"pagada"` en los caminos QR y
    de mostrador vigentes (`checkout_and_send`/`_confirm_order_impl` dejan la
    orden en `"abierta"` a propósito, con la venta ya emitida). Mismo patrón
    de subconsulta ya probado en `has_billable_orders`
    (`table_sessions/service.py`), sin tocar esa función."""
    return db.execute(
        select(Sale.id).where(Sale.customer_order_id == order_id).limit(1)
    ).scalar() is not None


# ------------------------------------------- Desglose de facturación (spec 094)

#: Valor de `estado_cocina` que excluye una línea de toda suma (FR-019). Es el
#: mismo filtro que `checkout.order_sale_lines` aplica al facturar: por eso la
#: suma de lo visible coincide con el subtotal de la factura.
_ESTADO_ANULADO = "anulado"


def _venta_propia(db: Session, order_id: UUID) -> Sale | None:
    """La `Sale` cuyo `customer_order_id` es este pedido, o `None`.

    Mismo patrón de subconsulta con `.limit(1)` que `order_has_sale`, pero trae
    la fila en vez de un booleano: el caso `factura_propia` necesita sus cuatro
    importes (research.md D4), y preguntar primero por la existencia con
    `order_has_sale` obligaría a una SEGUNDA consulta por la misma fila. Así el
    detalle añade **dos consultas constantes como máximo** (plan.md →
    Performance Goals), ninguna dependiente del número de ítems.

    La ponen los cuatro caminos de cobro **por pedido** (mostrador,
    `checkout-and-send`, aprobación de pago QR, confirmación de efectivo,
    `checkout.py:504,712,1191,1371`) y el cierre unificado de una mesa con un
    solo pedido (`table_sessions/service.py:734`)."""
    return db.execute(
        select(Sale).where(Sale.customer_order_id == order_id).limit(1)
    ).scalar_one_or_none()


def invoice_and_payment_info(db: Session, order_id: UUID) -> tuple[str | None, str | None]:
    """Referencia de factura real y nombre del método de pago de la orden,
    o `(None, None)` si todavía no tiene `Sale` (spec 120, corrección de
    FR-012: el detalle debe mostrar la factura y el método REALES, no un
    código sintético derivado del `id` -- hallazgo reportado por el negocio
    tras ver que "Referencia" no coincidía con la factura registrada).

    `invoice_reference` usa la misma fórmula que `InvoiceResponse.full_number`
    (`app/api/v1/invoices/schemas.py`): `prefix + number` con el número
    relleno a 6 dígitos. `payment_method_name` lee `payments` de la venta en
    vez de `OrderPaymentAttempt` porque los pedidos cobrados por mostrador
    (`pay_order`/`checkout_and_send`) nunca crean un intento de pago -- solo
    los cobrados por QR lo hacen -- así que `current_payment_attempt` queda
    permanentemente vacío para la mayoría de las órdenes pagadas.

    Dos consultas constantes como máximo, ninguna dependiente del número de
    ítems (mismo presupuesto que `_venta_propia`, guardia
    `test_orders_billing_summary::TestTrabajoAcotado`)."""
    sale = db.execute(
        select(Sale).options(selectinload(Sale.invoice)).where(Sale.customer_order_id == order_id).limit(1)
    ).scalar_one_or_none()
    if sale is None:
        return None, None
    invoice_reference = f"{sale.invoice.prefix}{sale.invoice.number:06d}" if sale.invoice else None
    payment_method_name = db.execute(
        select(PaymentMethod.name)
        .join(Payment, Payment.payment_method_id == PaymentMethod.id)
        .where(Payment.sale_id == sale.id)
        .limit(1)
    ).scalar()
    return invoice_reference, payment_method_name


def _sesion_tiene_venta(db: Session, table_session_id: UUID) -> bool:
    """¿La sesión de mesa de este pedido tiene alguna venta emitida?

    Es la señal de `factura_agrupada`: el cierre unificado de VARIOS pedidos
    deja `customer_order_id` en `None` (`table_sessions/service.py:734`) y el
    cierre dividido crea una venta por comensal, ninguna con pedido
    (`:832-847`). En los dos casos el total del pedido no es el total de
    ninguna factura."""
    return db.execute(
        select(Sale.id).where(Sale.table_session_id == table_session_id).limit(1)
    ).scalar() is not None


def _monto(valor) -> Decimal:
    """Importe como `Decimal`, con `NULL` leído como `0`.

    `sales.delivery_fee` y `customer_orders.delivery_fee` son nulables: el
    contrato publica `"0.00"` y la UI no pinta la fila (RN-004). Nunca se pinta
    un "$ 0" inventado."""
    return Decimal(valor) if valor is not None else Decimal("0")


def _promociones(snapshot) -> list[dict]:
    """Las entradas utilizables del JSONB `applied_promotions`.

    Lectura tolerante: la columna nace `'[]'` y las filas históricas pueden
    traer claves ausentes. Se descarta lo que no sea un diccionario en vez de
    reventar al serializar un pedido viejo."""
    if not snapshot:
        return []
    return [e for e in snapshot if isinstance(e, dict)]


def _etiqueta_del_descuento(promociones: list[dict]) -> str | None:
    """El `name` de la única promoción que explica el descuento, o `None`
    (FR-009, FR-010, research.md D6).

    Se agrupa por `promotion_id` **antes** de contar, porque el snapshot guarda
    una entrada por REGLA y no por promoción (`promotions/service.py:126-131`):
    una sola promoción con dos reglas deja dos entradas, y contar entradas
    pintaría "Descuento" donde FR-009 exige el nombre. Las entradas que no
    descontaron nada (`amount <= 0`) se ignoran: no convierten el caso en "dos
    promociones". Se agrupa por `name` cuando `promotion_id` falta, por lectura
    tolerante del JSONB histórico.

    Con dos o más promociones distintas la etiqueta es `None`: repartir el
    agregado entre ellas para pintar una fila por cada una sería recalcular, y
    FR-010 lo prohíbe."""
    por_promocion: dict = {}
    for entrada in promociones:
        if _monto(entrada.get("amount")) <= 0:
            continue
        nombre = entrada.get("name")
        clave = entrada.get("promotion_id") or nombre
        por_promocion.setdefault(clave, nombre)
    if len(por_promocion) != 1:
        return None
    return next(iter(por_promocion.values()))


def build_billing_summary(db: Session, order: CustomerOrder) -> OrderBillingSummary:
    """Desglose económico del pedido, ya resuelto (spec 094, FR-024b).

    Resuelve en un solo sitio las tres preguntas que la pantalla no debe
    responder: de dónde salen los importes (factura 1:1 o pedido), cuánto vale
    cada fila, y cuál de los tres avisos corresponde. La pantalla pinta lo que
    recibe — no suma, no resta y no bifurca (RN-001).

    **El orden de evaluación del estado es el de la tabla de research.md D2 y no
    es intercambiable**:

    1. existe una `Sale` con `customer_order_id = order.id` ⇒ `factura_propia`
       (FR-024: leer de la factura es lo que hace que el total sea idéntico al
       del módulo de Ventas *por construcción*);
    2. si no, el pedido **no está cancelado**, tiene `table_session_id` y existe
       alguna `Sale` con ese mismo `table_session_id` ⇒ `factura_agrupada`
       (FR-024a: se muestra el desglose propio del pedido, no el de esa
       factura);
    3. en cualquier otro caso ⇒ `sin_factura` (FR-023).

    La condición del cancelado de la regla 2 es la garantía G4b: el cierre de
    sesión solo cobra pedidos con `status NOT IN ('cancelada','pagada')`
    (`table_sessions/service.py:180`), así que un pedido cancelado antes del
    cierre se queda en la sesión con su `table_session_id` intacto y sin
    haberse cobrado nunca. Sin la exclusión, la pantalla le mostraría el
    literal de FR-024a —"Este pedido **se cobró** junto con otros…"—
    afirmando un cobro inexistente, que es el tipo de promesa que RN-005
    prohíbe. **No se aplica a la regla 1**: un pedido cancelado con venta
    propia es posible en datos históricos y ahí la factura sí cubre ese
    pedido, así que FR-024 sigue mandando.

    Ni `tax` ni `tip` se suman dentro de ningún otro importe (research.md D5):
    están fuera de alcance y la compuerta de datos midió `0` en todos los
    esquemas antes de implementar esto. Con `tax = tip = 0` la fórmula de
    `build_sale` colapsa exactamente en `Subtotal − Descuento + Envío = Total`,
    que es FR-008.
    """
    items = list(order.items or [])
    cobrables = [it for it in items if it.estado_cocina != _ESTADO_ANULADO]
    #: `line_total` es la propiedad de `models/order_item.py:87-91`
    #: (`unit_price × quantity + addons_total`), el auxiliar único — **no** un
    #: cálculo nuevo. `unit_price × quantity` a secas subcobraría los
    #: adicionales por línea del Menú QR (FR-003).
    suma_de_lineas = sum((it.line_total for it in cobrables), Decimal("0"))

    venta = _venta_propia(db, order.id)
    if venta is not None:
        state, source = "factura_propia", "factura"
        subtotal = _monto(venta.subtotal)
        discount = _monto(venta.discount)
        delivery_fee = _monto(venta.delivery_fee)
        total = _monto(venta.total)
        promociones = _promociones(venta.applied_promotions)
    elif (
        order.status != "cancelada"
        and order.table_session_id is not None
        and _sesion_tiene_venta(db, order.table_session_id)
    ):
        state, source = "factura_agrupada", "pedido"
        subtotal = suma_de_lineas
        discount = _monto(order.discount)
        delivery_fee = _monto(order.delivery_fee)
        total = subtotal - discount + delivery_fee
        promociones = _promociones(order.applied_promotions)
    else:
        state, source = "sin_factura", "pedido"
        subtotal = suma_de_lineas
        # FR-023: el descuento se calcula al cobrar, así que un pedido sin cobrar
        # no tiene descuento que mostrar — ni el que traiga en su columna.
        discount = Decimal("0")
        delivery_fee = _monto(order.delivery_fee)
        total = subtotal + delivery_fee
        promociones = []

    return OrderBillingSummary(
        state=state,
        source=source,
        subtotal=subtotal,
        discount=discount,
        discount_label=_etiqueta_del_descuento(promociones),
        delivery_fee=delivery_fee,
        # FR-008: el total nunca es negativo. Un pedido histórico con un
        # descuento mayor que la suma de sus líneas no debería existir, pero si
        # existe la pantalla no muestra un total en rojo.
        total=max(total, Decimal("0")),
        promotions=[AppliedPromotionOut(**_entrada_publicable(e)) for e in promociones],
        # Condición literal de FR-018 (research.md D8): hay al menos un ítem no
        # anulado y la suma de sus importes es `0`. Las tres precisiones que
        # FR-018 exige por separado salen gratis de esta definición — no se
        # dispara sin líneas, ni con todas anuladas, ni por ausencia de
        # descuento.
        sin_detalle_de_precios=bool(cobrables) and suma_de_lineas == 0,
    )


def _entrada_publicable(entrada: dict) -> dict:
    """Solo las tres claves que el contrato publica.

    `rule_id` existe en el JSONB y **no se publica**: no aporta nada a esta
    pantalla y expone un detalle interno del motor de promociones
    (data-model.md §2)."""
    return {
        "promotion_id": entrada.get("promotion_id"),
        "name": entrada.get("name"),
        "amount": _monto(entrada.get("amount")),
    }


def paid_order_ids(db: Session, order_ids: list[UUID]) -> set[UUID]:
    """Versión en bloque de `order_has_sale`, para no hacer N consultas al
    serializar un listado de pedidos (spec 029, D2 de research.md)."""
    if not order_ids:
        return set()
    rows = db.execute(
        select(Sale.customer_order_id).where(Sale.customer_order_id.in_(order_ids))
    ).scalars().all()
    return set(rows)


def staff_user_names(db: Session, user_ids: list[UUID]) -> dict[UUID, str]:
    """Nombre para mostrar de cada usuario de staff (Cajero o Mesero, sin
    distinción de rol) que aparezca como `CustomerOrder.user_id` — spec 076,
    Historia 4. Versión en bloque (mismo patrón que `paid_order_ids`, spec
    029 D2) para no hacer N consultas al serializar un listado de pedidos.
    No incluye ningún filtro por rol: el pedido de un Cajero se resuelve
    exactamente igual que el de un Mesero (spec 076, Clarifications)."""
    if not user_ids:
        return {}
    rows = db.execute(
        select(User.id, User.name).where(User.id.in_(user_ids))
    ).all()
    return {row[0]: row[1] for row in rows}


def list_orders(
    db: Session, status_filter: str | None = None, active_sessions_only: bool = False,
) -> list[CustomerOrder]:
    """Listado de comandas para la Terminal de Mesas (`GET /orders`).

    `active_sessions_only=True` (spec 029, hotfix): descarta pedidos ya
    pagados cuya `TableSession` ya cerró — sin esto, un pedido de una visita
    ya cobrada y liberada quedaba visible para siempre (ligado a la misma
    `dining_table_id` física) y reaparecía mezclado con la sesión nueva en
    cuanto la mesa se ocupaba de nuevo, porque `status` nunca llega a
    `"pagada"` en los caminos QR/mostrador vigentes (D2 de research.md).
    Mismo patrón de subconsulta que `has_billable_orders`/`_billable_orders`
    (`table_sessions/service.py`), expuesto aquí para el listado general.

    Un pedido **sin pagar** se conserva aunque su sesión ya haya cerrado — es
    el caso huérfano real que `billOrphan` (frontend) existe para señalar,
    no algo que ocultar. Uno sin `table_session_id` (mostrador puro, sin
    mesa) tampoco se toca."""
    q = select(CustomerOrder).options(
        selectinload(CustomerOrder.items).selectinload(OrderItem.options),
        selectinload(CustomerOrder.payment_attempts)
        .selectinload(OrderPaymentAttempt.payment_method),
    ).order_by(CustomerOrder.created_at.desc())
    if status_filter is not None:
        q = q.where(CustomerOrder.status == status_filter)
    if active_sessions_only:
        inactive_sessions = select(TableSession.id).where(TableSession.status != "active")
        pedidos_pagados = select(Sale.customer_order_id).where(Sale.customer_order_id.isnot(None))
        q = q.where(
            or_(
                CustomerOrder.table_session_id.is_(None),
                CustomerOrder.table_session_id.notin_(inactive_sessions),
                CustomerOrder.id.notin_(pedidos_pagados),
            )
        )
    return db.execute(q).scalars().all()


#: Los 6 valores exactos del filtro de estado de la pantalla "Órdenes" — el
#: conjunto cerrado que acepta la rama paginada de `GET /orders` (spec 079,
#: FR-008, data-model.md §3). `None` / ausente = "Todos" (sin predicado). El
#: router valida contra este conjunto y responde 422 fuera de rango.
ESTADOS_MOSTRADOS_ORDENES = ("recibida", "abierta", "bloqueada", "pagada", "cancelada")


def list_orders_query(
    status_mostrado: str | None = None, order_type: str | None = None,
    date_from: date | None = None, date_to: date | None = None, q: str | None = None,
) -> Select:
    """`Select` de comandas para la rama **paginada** de `GET /orders` (spec 079).

    Orden determinista `created_at DESC, id DESC` (FR-003, SC-008): `created_at`
    conserva el orden cronológico que la pantalla tiene hoy; `id` (uuid4, estable
    pero no cronológico) desempata los `created_at` idénticos para que recorrer
    las páginas cubra el conjunto sin huecos ni repeticiones.

    `status_mostrado` traduce el "estado que ve la persona" a predicado — el port
    fiel de `displayOrderStatus()` del frontend, movido al servidor (FR-016,
    data-model.md §3): "cancelada" gana siempre; una orden se ve "Pagada" si
    tiene `Sale` **o** si su `status` crudo ya es `'pagada'` y no está cancelada;
    los estados no terminales conservan su `status` crudo solo si aún no tienen
    venta. `order_type` filtra por la columna homónima; `order_type IS NULL`
    (órdenes históricas sin clasificar) nunca satisface un `=` concreto, así que
    queda fuera al filtrar por un tipo y dentro en "Todos" (FR-018), sin
    `COALESCE` ni centinela.

    `list_orders()` (Terminal/Dashboard) **no se toca**: conserva su firma y su
    salida (plan.md, Constitution Check, Principio III).

    `date_from`/`date_to`/`q` son aditivos (spec 120, research.md §1-§2):
    `date_from`/`date_to` acotan `created_at` (inclusive en ambos extremos,
    `date_to` incluye todo su día); `q` hace *match* parcial insensible a
    mayúsculas contra `customer_name` **o** contra el texto del `id` — el
    mismo criterio que `orderDisplayCode()` usa en el frontend para el código
    corto visible.
    """
    stmt = (
        select(CustomerOrder)
        .options(
            selectinload(CustomerOrder.items).selectinload(OrderItem.options),
            selectinload(CustomerOrder.payment_attempts)
            .selectinload(OrderPaymentAttempt.payment_method),
        )
        .order_by(CustomerOrder.created_at.desc(), CustomerOrder.id.desc())
    )

    tiene_venta = exists(
        select(Sale.id).where(Sale.customer_order_id == CustomerOrder.id)
    )

    if status_mostrado in ("recibida", "abierta", "bloqueada"):
        stmt = stmt.where(
            CustomerOrder.status == status_mostrado, ~tiene_venta,
        )
    elif status_mostrado == "pagada":
        stmt = stmt.where(
            CustomerOrder.status != "cancelada",
            or_(tiene_venta, CustomerOrder.status == "pagada"),
        )
    elif status_mostrado == "cancelada":
        stmt = stmt.where(CustomerOrder.status == "cancelada")

    if order_type is not None:
        stmt = stmt.where(CustomerOrder.order_type == order_type)

    if date_from is not None:
        stmt = stmt.where(CustomerOrder.created_at >= datetime.combine(date_from, time.min))
    if date_to is not None:
        stmt = stmt.where(
            CustomerOrder.created_at < datetime.combine(date_to + timedelta(days=1), time.min)
        )

    q_normalizado = q.strip() if q else None
    if q_normalizado:
        stmt = stmt.where(
            or_(
                CustomerOrder.customer_name.ilike(f"%{q_normalizado}%"),
                cast(CustomerOrder.id, String).ilike(f"%{q_normalizado}%"),
            )
        )

    return stmt


def clamp_page(db: Session, stmt: Select, page: int, size: int) -> int:
    """Página efectiva tras acotar `page` al rango válido del conjunto **ya
    filtrado** de `stmt` (spec 079, FR-005, data-model.md §2, regla 3).

    Cuenta el `total` con el mismo `COUNT(*)` envolvente que usa `paginate()`,
    calcula `pages = ceil(total / size)` y devuelve `1` si el conjunto está
    vacío, si no `min(max(page, 1), pages)`. El router llama
    `paginate(db, stmt, clamp_page(db, stmt, page, size), size)`, de modo que
    una página fuera de rango devuelve la última con resultados (o la 1 vacía) y
    el frontend se recoloca sin `404`. Los dos `COUNT(*)` del camino paginado
    (este + el de `paginate`) recaen sobre índices ya presentes, así que su
    coste no crece de forma apreciable con el total (research.md §3).
    """
    total = db.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar_one()
    if total == 0:
        return 1
    pages = ceil(total / size)
    return min(max(page, 1), pages)


def create_order(
    db: Session,
    data: OrderCreate,
    user_id: UUID | None,
    user=None,
    request_id: str | None = None,
) -> CustomerOrder:
    """`user` (spec 074) es solo para el log de auditoría: el `User` completo
    del cajero, del que salen su rol y su tenant — datos que `user_id` por sí
    solo no lleva. Opcional para no cambiar el contrato de ningún llamador
    existente; el router sí lo pasa siempre.

    `request_id` (spec 074, extensión de logging operativo — FR-021) es igual
    de opcional y por el mismo motivo: correlaciona el evento de auditoría con
    la entrada de log operativo de la misma petición HTTP."""
    # spec 111 (FR-011, RN4): un reintento de red sobre la misma solicitud de creación (el
    # cliente no recibió la respuesta y reintenta) no debe generar un segundo pedido -- mismo
    # patrón que ya usa `invoices.service` (devolver el recurso existente en vez de duplicarlo).
    # Sin `client_request_id` (clientes que aún no lo envían), comportamiento idéntico al actual.
    if data.client_request_id is not None:
        existing = db.execute(
            select(CustomerOrder).where(CustomerOrder.client_request_id == data.client_request_id)
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    # T013 (spec 028): 'hold_for_payment' es el modo "cobra primero, envía
    # después" del mostrador/mesero. El canal QR ya tiene su propio flujo
    # 'recibida' (vía /cart/submit, con OrderPaymentAttempt) — mezclarlo con
    # esta bandera duplicaría ese camino con reglas distintas.
    if data.hold_for_payment and data.channel is OrderChannel.QR_MENU:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "hold_for_payment solo aplica a comandas de mostrador/mesero, no a pedidos por QR.",
        )

    # spec 055, research.md D5: un pedido para llevar o a domicilio nunca
    # lleva mesa asociada.
    if data.order_type in (OrderType.TAKEAWAY, OrderType.DELIVERY) and data.dining_table_id is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Un pedido para llevar o a domicilio no puede tener una mesa asociada.",
        )

    # spec 055, FR-006/FR-007.
    if data.order_type not in _COMBINACIONES_CANAL_TIPO_ORDEN[data.channel]:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"La combinación de canal '{data.channel.value}' y tipo de orden "
            f"'{data.order_type.value}' no es válida.",
        )

    # spec 056, FR-007: un pedido a domicilio requiere nombre del cliente,
    # dirección y valor del domicilio (el teléfono queda fuera a propósito,
    # FR-008 — siempre opcional).
    if data.order_type is OrderType.DELIVERY and (
        not (data.customer_name or "").strip()
        or not (data.delivery_address or "").strip()
        or data.delivery_fee is None
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Un pedido a domicilio requiere nombre del cliente, dirección y "
            "valor del domicilio.",
        )

    customer_name = data.customer_name
    table_id = data.dining_table_id

    participant = None
    if data.participant_id is not None:
        participant = get_or_404(
            db, SessionParticipant, data.participant_id, "Participant not found"
        )
        if participant.status != "open":
            raise HTTPException(status.HTTP_409_CONFLICT, "El comensal ya no está en la mesa")
        table_id = participant.dining_table_id
        customer_name = customer_name or participant.display_label or participant.display_name

    # spec 087 (FR-005, A-88): el nombre del cliente pasa de opcional a
    # obligatorio también para DINE_IN/TAKEAWAY (DELIVERY ya lo exigía,
    # arriba). Corre después de resolver `participant` -- un pedido QR ya
    # trae el nombre autocompletado desde `participant.display_label`/
    # `display_name` en ese punto, así que validar antes rechazaría pedidos
    # que sí traen nombre del comensal.
    #
    # spec 091 (A-99): reabre A-88 solo para la Terminal (`channel=POS`) --
    # un DINE_IN/TAKEAWAY de mostrador/mesero sin nombre ya no se rechaza, se
    # guarda con `DEFAULT_CUSTOMER_NAME`. Cualquier otro canal que admita estos
    # tipos (`WHATSAPP`/`API` para TAKEAWAY; `QR_MENU` nunca llega vacío, ver
    # arriba) sigue exigiendo el nombre exactamente como antes.
    if data.order_type in (OrderType.DINE_IN, OrderType.TAKEAWAY) and not (customer_name or "").strip():
        if data.channel is OrderChannel.POS:
            customer_name = DEFAULT_CUSTOMER_NAME
        else:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "El nombre del cliente es obligatorio.",
            )

    if table_id is not None and participant is None:
        get_or_404(db, DiningTable, table_id, "Table not found")

    # Un pedido de mesa sin sesión no entra en ninguna cuenta y no se podría
    # cobrar, así que se abre la sesión si aún no existe.
    table_session_id = (
        participant.table_session_id if participant is not None
        else get_or_create_table_session_id(db, table_id) if table_id is not None
        else None
    )

    # T014 (spec 028, FR-013): una mesa no mezcla orígenes de pedido a la vez.
    # Si ya hay un pedido QR activo en la sesión, una comanda de
    # mostrador/mesero no puede abrirse encima (y viceversa: ver
    # `cart.service.submit_cart`, T015).
    if table_session_id is not None and data.channel is not OrderChannel.QR_MENU:
        conflicto_qr = db.execute(
            select(CustomerOrder.id).where(
                CustomerOrder.table_session_id == table_session_id,
                CustomerOrder.channel == "QR_MENU",
                CustomerOrder.status.in_(_NON_TERMINAL_ORDER_STATUSES),
            ).limit(1)
        ).scalar()
        if conflicto_qr is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Esta mesa ya tiene un pedido activo por QR; ciérralo antes de "
                "abrir una comanda de mostrador/mesero.",
            )

    # spec 087 (FR-006, A-86): numeración estable de pedidos de mesa, asignada
    # una sola vez aquí -- solo para DINE_IN, nunca recalculada después.
    cash_shift_id = table_order_number = None
    if data.order_type is OrderType.DINE_IN:
        cash_shift_id, table_order_number = resolve_dine_in_table_order(db)

    try:
        order = CustomerOrder(
            participant_id=data.participant_id,
            table_session_id=table_session_id,
            dining_table_id=table_id,
            customer_name=customer_name,
            channel=data.channel.value,
            order_type=data.order_type.value,
            delivery_address=data.delivery_address,
            delivery_phone=data.delivery_phone,
            delivery_fee=data.delivery_fee,
            cash_shift_id=cash_shift_id,
            table_order_number=table_order_number,
            # spec 073 (FR-008, A-70): el instante de vigencia de promociones se
            # congela una sola vez, aquí, al crear el pedido — aware UTC (la
            # columna es DateTime(timezone=True); NO `.replace(tzinfo=None)`,
            # ver data-model.md). Nunca se vuelve a tocar.
            promotion_evaluated_at=datetime.now(timezone.utc),
            # T013: con hold_for_payment nace 'recibida', igual que un pedido
            # QR sin confirmar — no compromete stock ni es visible para cocina
            # hasta que se cobre (`checkout.checkout_and_send`).
            status="recibida" if data.hold_for_payment else "abierta",
            user_id=user_id,
            notes=data.notes,
            client_request_id=data.client_request_id,
        )
        db.add(order)
        db.flush()

        variants_by_line = [
            get_or_404(db, ProductVariant, line.product_variant_id, "Variant not found")
            for line in data.items
        ]
        for variant in variants_by_line:
            if not variant.active:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Variante inactiva: {variant.id}")
        # spec 093 (FR-021/FR-022): barrido de TODOS los ítems antes de crear nada --
        # si uno o más productos están agotados, se nombran todos de una vez en vez
        # de obligar a reintentar uno por uno.
        ensure_products_available(variants_by_line)

        entries: list[tuple[OrderItem, list[ChosenOption]]] = []
        for line, variant in zip(data.items, variants_by_line):
            # Deduplica, exige que estén activas y valida la selección contra los
            # grupos del producto. Antes este bucle cargaba las opciones a mano y se
            # saltaba las tres cosas.
            options = load_valid_options(db, line.options, variant=variant, quantity=line.quantity)
            # spec 117 (A-126): nunca es el carrito QR -- `qr_addons_per_line=False`.
            options = mark_per_line(db, options, qr_addons_per_line=False)

            item = OrderItem(
                order_id=order.id,
                participant_id=data.participant_id,
                product_variant_id=variant.id,
                quantity=line.quantity,
                notes=line.notes,
            )
            db.add(item)
            db.flush()

            for chosen in options:
                db.add(OrderItemOption(
                    order_item_id=item.id, option_id=chosen.option.id, quantity=chosen.quantity,
                    per_line=chosen.per_line,
                ))

            item.unit_price = compute_unit_price(variant, options)
            item.addons_total = compute_addons_total(options)
            entries.append((item, options))

        # spec 118 (research.md D3): persiste el descuento por línea de las
        # promociones por volumen/porcentuales vigentes -- import diferido para
        # romper el ciclo service.py <-> checkout.py (research.md D2).
        from app.api.v1.orders.checkout import refresh_order_item_discounts
        refresh_order_item_discounts(db, order)

        if not data.hold_for_payment:
            # Nace confirmada, así que compromete stock aquí y ahora. Si falta
            # un insumo (o alguna variante no tiene receta) revienta la
            # transacción entera y no se crea la comanda: mejor no venderla
            # que venderla sin descontar.
            #
            # Con hold_for_payment=True (T013) esto se salta a propósito: la
            # comanda nace 'recibida' y el descuento se hace en
            # `checkout.checkout_and_send`, al cobrar — mismo punto único de
            # descuento que usa el flujo QR (`_confirm_order_impl`), por la
            # otra puerta.
            deduct_order_items_naming_product(db, entries, user_id, reference_id=order.id)

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Error creando comanda")
        raise

    # spec 074 (FR-001/FR-010): la comanda manual ya está comprometida — el
    # actor es el cajero/mesero que la creó, con su rol.
    record_order_audit_event(
        event_type=OrderAuditEventType.ORDER_CREATED,
        order_id=order.id,
        tenant_id=getattr(user, "tenant_id", None),
        actor=OrderAuditActor(
            type=ActorType.CAJERO,
            id=str(user_id) if user_id is not None else None,
            role=getattr(user, "role_name", None),
        ),
        details={
            "channel": data.channel.value,
            "order_type": data.order_type.value,
            "hold_for_payment": bool(data.hold_for_payment),
        },
        request_id=request_id,
    )

    return db.execute(
        select(CustomerOrder).where(CustomerOrder.id == order.id)
    ).scalar_one()
