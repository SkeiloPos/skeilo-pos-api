"""Ciclo de vida de la sesión de mesa: consulta, cuenta consolidada y cierre con
elección de `billing_mode`.

El cierre es el punto donde la mesa se cobra y se libera. `unified` emite una sola
venta con todo lo consumido; `split` emite una venta por comensal, agrupando por
`order_items.participant_id` — que es asignación **por ítem**, así que el reparto
es exacto aunque un pedido mezcle comensales.
"""
import logging
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core import events
from app.core.crud import get_or_404
from app.core.models import Tenant, User
from app.core.notifications.dispatch import notify_payment_completed
from app.core.plan_limits import has_module_access
from app.core.timezone import utc_now
from app.models.customer_order import CustomerOrder
from app.models.dining_table import DiningTable
from app.models.order_item import EN_CURSO, OrderItem, OrderItemOption
from app.models.sale import Sale
from app.models.session_participant import SessionParticipant
from app.models.table_session import TableSession
from app.api.v1.orders import checkout
from app.api.v1.orders.kitchen import _apply_kitchen_send
from app.api.v1.orders.service import order_pending_balance
from app.api.v1.sales.builder import build_sale, ensure_open_shift
from app.api.v1.table_sessions.schemas import (
    BillingMode, CloseSessionIn, CloseSessionResponse,
    ReleaseSessionResponse,
    SessionBillItem, SessionBillLine, SessionBillResponse, TableSessionResponse,
)

logger = logging.getLogger(__name__)


def _load(db: Session, table_session_id: UUID, *, lock: bool = False) -> TableSession:
    """Carga la sesión. Con `lock=True` toma `FOR UPDATE` sobre la fila.

    El lock es obligatorio en el cierre: sin él, dos `POST /close` concurrentes leen
    ambos `status == 'active'` y cobran la mesa dos veces. El check de estado no vale
    de nada si se lee sin bloquear.
    """
    stmt = select(TableSession).where(TableSession.id == table_session_id)
    if lock:
        # `selectinload` emite una segunda consulta y no se puede combinar con
        # FOR UPDATE en la misma; los participantes se cargan luego, ya con el lock.
        stmt = stmt.with_for_update()
    else:
        stmt = stmt.options(selectinload(TableSession.participants))
    ts = db.execute(stmt).scalar_one_or_none()
    if ts is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sesión de mesa no encontrada")
    return ts


def get_session(db: Session, table_session_id: UUID) -> TableSession:
    return _load(db, table_session_id)


# ------------------------------------------------------ Liberar mesa abandonada

def open_orders(db: Session, table_session_id: UUID) -> list[CustomerOrder]:
    """Órdenes "abiertas" de la sesión (spec 147, FR-016): `status IN
    ('pendiente', 'confirmado')` OR (`status == 'entregada'` AND
    `payment_status == 'UNPAID'`). Las `'cancelada'` nunca cuentan como
    abiertas — ni siquiera un pedido `'entregada'`+`'PAID'` (cerrado, FR-006)
    cuenta. `'confirmado'` (a pedido del usuario) cuenta igual que
    `'pendiente'`. Reemplaza el criterio de `has_billable_orders` (antes:
    "¿hay algo por cobrar?", que dependía además de si ya existía una `Sale`,
    un rodeo necesario porque `status` rara vez llegaba a `'pagada'` por sí
    solo) — con `payment_status` la señal de "ya se cobró" ya no necesita ese
    rodeo."""
    return db.execute(
        select(CustomerOrder).where(
            CustomerOrder.table_session_id == table_session_id,
            CustomerOrder.status.in_(("pendiente", "confirmado"))
            | (
                (CustomerOrder.status == "entregada")
                & (CustomerOrder.payment_status == "UNPAID")
            ),
        )
    ).scalars().all()


def has_open_orders(db: Session, table_session_id: UUID) -> bool:
    """¿Existe alguna orden abierta (FR-016) en la sesión? Ver `open_orders`."""
    return db.execute(
        select(CustomerOrder.id).where(
            CustomerOrder.table_session_id == table_session_id,
            CustomerOrder.status.in_(("pendiente", "confirmado"))
            | (
                (CustomerOrder.status == "entregada")
                & (CustomerOrder.payment_status == "UNPAID")
            ),
        ).limit(1)
    ).scalar() is not None


def has_pending_balance(db: Session, table_session_id: UUID) -> bool:
    """¿Alguna orden de la sesión (no cancelada) tiene saldo pendiente por
    adiciones agregadas después de facturar (spec 123)? Hermana de
    `has_billable_orders` — a propósito NO reusa ese chequeo: un pedido con
    saldo pendiente es justo uno de los que `has_billable_orders` ya excluye
    (tiene una `Sale`), así que necesita su propia consulta."""
    order_ids = db.execute(
        select(CustomerOrder.id).where(
            CustomerOrder.table_session_id == table_session_id,
            CustomerOrder.status != "cancelada",
        )
    ).scalars().all()
    return any(order_pending_balance(db, oid) > 0 for oid in order_ids)


def try_release_if_empty(db: Session, table_session_id: UUID) -> list[TableSession]:
    """Cierra la sesión y devuelve la mesa a `libre` **solo si** ya no queda nadie
    y no hay nada que cobrar. Devuelve las sesiones que cerró (lista vacía = no la
    liberó; su truthiness sigue sirviendo a quien solo evaluaba `if try_release...`).

    spec 089 (A-95): devuelve las sesiones, y no un `bool`, para que el llamador —después
    de su `commit`— avise al comensal con `notify_sessions_closed(..., reason="empty")`. El
    evento nunca sale aquí, antes del commit: anunciaría un cierre que puede hacer rollback.

    Es el único punto que decide esto; lo llaman el "salir" del comensal, la
    expiración de su token, la cancelación de su último pedido y el barrido.

    Sin commit: se une a la transacción del caller.

    Dos condiciones, ambas necesarias:

    - **Nadie activo.** Si queda un comensal `open`, la mesa sigue ocupada aunque
      quien se fue no hubiera pedido nada.
    - **Nada que cobrar.** Si hay pedidos vivos la mesa **no** se libera aunque no
      quede nadie: es un descuadre real que debe ver el personal, no algo que
      convenga tapar automáticamente.
    """
    ts = db.get(TableSession, table_session_id)
    if ts is None or ts.status != "active":
        return []

    # La sesión se abre con `autoflush=False` (ver `with_db`), así que sin este
    # flush las consultas de abajo no verían el comensal que el caller acaba de
    # cerrar en memoria: la mesa nunca se liberaría y el endpoint respondería OK.
    db.flush()

    quedan = db.execute(
        select(SessionParticipant.id).where(
            SessionParticipant.table_session_id == ts.id,
            SessionParticipant.status == "open",
        ).limit(1)
    ).scalar()
    if quedan is not None:
        return []

    if has_open_orders(db, ts.id):
        return []

    sessions = checkout.close_table_sessions(db, ts.dining_table_id, closed_by=None)
    table = db.get(DiningTable, ts.dining_table_id)
    if table is not None:
        table.status = "libre"
        checkout.delete_orphan_carts(db, sessions)
    return sessions


def notify_sessions_closed(tenant_id: int | None, sessions, reason: str) -> None:
    """Avisa en tiempo real al comensal de que su sesión de mesa se cerró (`session.closed`,
    canales `staff` y `session:{id}`), una vez por sesión. spec 089 (A-95).

    **Llamar siempre DESPUÉS del `commit`** que cerró las sesiones. Es *best-effort*: nunca
    lanza (un evento perdido degrada la pantalla al sondeo de respaldo, que sigue existiendo),
    porque el cierre ya está comprometido. `reason` ∈ `paid | swept | released | empty`.
    Sin `tenant_id` o sin sesiones no hace nada."""
    if tenant_id is None:
        return
    for ts in sessions or []:
        try:
            events.session_closed(
                tenant_id,
                table_session_id=ts.id,
                dining_table_id=ts.dining_table_id,
                reason=reason,
            )
        except Exception:
            logger.warning("No se pudo publicar session.closed de %s", getattr(ts, "id", "?"), exc_info=True)


def list_sessions(db: Session, *, only_active: bool = True) -> list[TableSession]:
    stmt = select(TableSession).options(selectinload(TableSession.participants))
    if only_active:
        stmt = stmt.where(TableSession.status == "active")
    return db.execute(stmt.order_by(TableSession.opened_at.desc())).scalars().all()


def _billable_orders(db: Session, table_session_id: UUID) -> list[CustomerOrder]:
    """Pedidos de la sesión que entran en la cuenta: ni cancelados, ni ya
    pagados, ni con una `Sale` ya emitida (spec 028 — mismo motivo que
    `has_billable_orders`: evita facturar dos veces un pedido QR que ya se
    cobró al aprobar/confirmar su pago, aunque su `status` siga en
    `"entregada"`/`"pendiente"` mientras cocina lo termina). Spec 147: ya no
    existe `status == "pagada"` — "ya pagado" se lee en `payment_status`."""
    ya_facturados = select(Sale.customer_order_id).where(Sale.customer_order_id.isnot(None))
    return db.execute(
        select(CustomerOrder)
        .options(selectinload(CustomerOrder.items))
        .where(
            CustomerOrder.table_session_id == table_session_id,
            CustomerOrder.status != "cancelada",
            CustomerOrder.payment_status != "PAID",
            CustomerOrder.id.notin_(ya_facturados),
        )
        .order_by(CustomerOrder.created_at)
    ).scalars().all()


def _resolve_orders_to_charge(
    db: Session, table_session_id: UUID, order_ids: list[UUID] | None = None,
) -> list[CustomerOrder]:
    """Acota `_billable_orders` a un subconjunto explícito (spec 156). Sin
    `order_ids` (`None`), comportamiento actual sin cambios: todos los
    facturables de la sesión (Principio II). Con `order_ids`, cualquier id
    solicitado que no aparezca entre los facturables (no existe, es de otra
    sesión, ya está `PAID`, cancelado, o ya tiene una `Sale`) hace fallar la
    petición completa con 409 -- mismo patrón de error que
    `release_paid_session` para órdenes abiertas."""
    orders = _billable_orders(db, table_session_id)
    if order_ids is None:
        return orders

    wanted = set(order_ids)
    filtered = [o for o in orders if o.id in wanted]
    found = {o.id for o in filtered}
    missing = [oid for oid in order_ids if oid not in found]
    if missing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "Algunos pedidos seleccionados no se pueden cobrar.",
                "order_ids": [str(oid) for oid in missing],
            },
        )
    return filtered


def compute_bill(
    db: Session, table_session_id: UUID, tenant: Tenant | None = None,
    order_ids: list[UUID] | None = None,
) -> SessionBillResponse:
    """Cuenta de la sesión, ya con descuentos automáticos (promociones/combos)
    aplicados — mismo cálculo por comensal que usa `_close_split` al cobrar, para
    que este preview coincida con lo que realmente se cobrará.

    `order_ids` (spec 156) acota la cuenta a ese subconjunto de pedidos en vez
    de a todos los facturables de la sesión; sin él, comportamiento actual."""
    ts = _load(db, table_session_id)
    orders = _resolve_orders_to_charge(db, ts.id, order_ids)

    labels = {p.id: (p.display_label or p.display_name) for p in ts.participants}

    participant_ids: list[UUID | None] = []
    seen: set[UUID | None] = set()
    for order in orders:
        for it in order.items:
            if it.estado_cocina == "cancelado" or it.participant_id in seen:
                continue
            seen.add(it.participant_id)
            participant_ids.append(it.participant_id)

    # spec 073 (FR-012a, A-70): un único instante para toda la cuenta de la
    # mesa — el del pedido más antiguo pendiente de cobro (o la hora del cobro
    # si ninguno lo tiene congelado). La agrupación de líneas por comensal no
    # cambia.
    instant = checkout.promotion_evaluation_instant(orders, now=utc_now())
    total = Decimal("0")
    split: list[SessionBillLine] = []
    for pid in participant_ids:
        lines = []
        for order in orders:
            lines.extend(checkout.order_sale_lines(db, order.id, participant_id=pid))
        raw_subtotal = sum((line.line_total for line in lines), Decimal("0"))
        # spec 063: el motor por conjunto agrupa el subconjunto de líneas
        # evaluadas juntas — aquí, por comensal (research.md D7).
        # spec 138 (FR-006): la vigencia se evalúa en la zona del tenant dueño de la mesa.
        promo_discount, _, _ = checkout.auto_discount(db, lines, instant, tenant)
        subtotal = raw_subtotal - promo_discount
        total += subtotal
        split.append(SessionBillLine(
            participant_id=pid,
            display_label=labels.get(pid) if pid else None,
            subtotal=subtotal,
            # spec 026, FR-006: expone lo que este cálculo ya tenía en memoria
            # (líneas y descuento) — no cambia ningún valor, solo lo serializa.
            items=[
                SessionBillItem(
                    description=line.description,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    line_total=line.line_total,
                )
                for line in lines
            ],
            discount=promo_discount,
        ))

    # spec 123 (contracts/pending-balance-exposure.md): a propósito, **no**
    # se reusa `orders` (viene de `_billable_orders`, que excluye cualquier
    # pedido con `Sale` ya emitida) -- un pedido con saldo pendiente por
    # adiciones es justo uno de esos pedidos ya facturados. Se recorren todas
    # las órdenes no canceladas de la sesión, sin ese filtro.
    pending_order_ids = db.execute(
        select(CustomerOrder.id).where(
            CustomerOrder.table_session_id == ts.id,
            CustomerOrder.status != "cancelada",
        )
    ).scalars().all()
    pending_balance = sum(
        (order_pending_balance(db, oid) for oid in pending_order_ids), Decimal("0")
    )

    return SessionBillResponse(
        table_session_id=ts.id,
        dining_table_id=ts.dining_table_id,
        total=total,
        order_ids=[o.id for o in orders],
        split=split,
        pending_balance=pending_balance,
    )


def _assert_closable(
    db: Session, orders: list[CustomerOrder], *, tenant: Tenant | None = None,
) -> None:
    """Una sesión no se cierra con pedidos sin confirmar ni con comida en curso.

    Spec 153 (FR-001/FR-003/FR-006): el chequeo de "comida en curso" (ningún
    ítem `estado_cocina=='pendiente'`) se omite cuando el tenant tiene el
    módulo de Cocina activo -- cobrar ya no exige entregar antes, FR-001 deja
    que el pedido llegue a cocina recién al cobrarse. El chequeo de "pedidos
    sin confirmar" se mantiene siempre, con o sin módulo activo: es un
    requisito de flujo ajeno a cocina. `tenant=None` (valor por defecto, usado
    por `release_paid_session`, que nunca lo pasa) conserva el comportamiento
    de siempre -- esta omisión solo aplica al cobro (`_charge_session`)."""
    sin_confirmar = [o for o in orders if o.status == "pendiente"]
    if sin_confirmar:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "Hay pedidos recibidos sin confirmar; confírmalos o cancélalos.",
                "order_ids": [str(o.id) for o in sin_confirmar],
            },
        )

    if tenant is not None and has_module_access(db, tenant, "cocina"):
        return

    en_cocina = [
        {"order_id": str(o.id), "order_item_id": str(it.id),
         "estado_cocina": it.estado_cocina}
        for o in orders for it in o.items
        if it.estado_cocina in EN_CURSO
    ]
    if en_cocina:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "Hay ítems sin terminar en cocina; anúlalos o espera a que estén listos.",
                "items": en_cocina,
            },
        )


def _charge_session(
    db: Session, table_session_id: UUID, data: CloseSessionIn, cashier: User,
    *, invoice_prefix: str = "", tenant: Tenant | None = None,
    order_ids: list[UUID] | None = None,
) -> tuple[TableSession, list[Sale]]:
    """Cobra los pedidos facturables de la sesión -- crea la(s) `Sale` y marca
    `payment_status='PAID'` -- sin tocar `TableSession.status` ni la mesa.
    `close_session` encadena el cierre+liberación después de esto;
    `charge_session` (a propósito, spec 147 ciclo 2) no: cobrar no significa
    que el cliente se vaya de inmediato, la mesa sigue ocupada hasta que el
    cajero la libere con `POST /release` (mismo mecanismo que ya usa el modo
    híbrido de spec 028 cobrado pedido por pedido).

    `order_ids` (spec 156) acota el cobro a ese subconjunto -- recibido como
    parámetro explícito, no leído de `data`, para que `close_session` (D2 de
    research.md: `/close` jamás admite cobro parcial) pueda omitirlo siempre
    sin importar qué traiga el body.

    No hace commit: cada caller decide qué más entra en su transacción.
    """
    # Con lock: el check de estado que sigue solo sirve si nadie más puede estar
    # cobrando esta misma sesión a la vez.
    ts = _load(db, table_session_id, lock=True)
    if ts.status != "active":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"La sesión ya está {ts.status}"
        )

    shift = ensure_open_shift(db, data.cash_shift_id)
    orders = _resolve_orders_to_charge(db, ts.id, order_ids)
    if not orders:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "La sesión no tiene pedidos que cobrar"
        )
    _assert_closable(db, orders, tenant=tenant)

    # A pedido del usuario (spec 147 ciclo 2): un pedido QR puede llegar
    # aquí sin haber pasado nunca por la revisión de pago (aprobar
    # comprobante/confirmar efectivo) -- "Entregar" ya es independiente del
    # pago. Acotado a channel == 'QR_MENU' a propósito: un pedido de mesero
    # (`POS`) ya descuenta de inmediato al agregar cada ítem
    # (`consolidation.add_item_to_order`, un camino que `ensure_inventory_
    # deducted` detecta como "ya descontado" y no vuelve a tocar), así que
    # ensanchar este chequeo a todo canal solo sumaría una llamada que nunca
    # hace falta.
    for order in orders:
        if order.channel == "QR_MENU":
            checkout.ensure_inventory_deducted(db, order, cashier)

    if data.billing_mode is BillingMode.UNIFIED:
        sales = [_close_unified(db, ts, orders, data, shift, cashier, invoice_prefix, tenant)]
    else:
        sales = _close_split(db, ts, orders, data, shift, cashier, invoice_prefix, tenant)

    # Spec 147: `_assert_closable` ya exige que ningún pedido esté
    # `'pendiente'` (status) ni tenga ítems `'pendiente'` (estado_cocina,
    # `EN_CURSO`) — todos están ya `'entregada'` aquí. Solo falta el pago.
    for order in orders:
        order.payment_status = "PAID"

    # Spec 153 (FR-001/FR-002, contracts/table-session-charge.md): con el
    # módulo de Cocina activo, cobrar y enviar a cocina son un solo paso --
    # cada pedido de mesa que esta operación deja en `payment_status='PAID'`
    # se envía de inmediato, reusando la misma transición idempotente de
    # `send_to_kitchen` (`_apply_kitchen_send`, no-op si ya tiene
    # `kitchen_status`). Acotado a `orders` (lo que esta llamada acaba de
    # cobrar): un pedido ya `PAID` antes de esta operación -- p. ej. cuenta
    # dividida vía `OrderPaymentAttempt` que todavía no complete el 100% de
    # la sesión -- no está en `orders` (`_billable_orders` lo excluye) y no
    # se toca aquí; se evaluará en el próximo cobro que sí lo incluya.
    if tenant is not None and has_module_access(db, tenant, "cocina"):
        for order in orders:
            if order.dining_table_id is not None:
                _apply_kitchen_send(order)

    ts.billing_mode = data.billing_mode.value
    return ts, sales


def close_session(
    db: Session, table_session_id: UUID, data: CloseSessionIn, cashier: User,
    *, invoice_prefix: str = "", tenant_id: int | None = None, tenant: Tenant | None = None,
) -> CloseSessionResponse:
    """Cobra y cierra la sesión de mesa. Solo staff (el comensal anónimo no puede).

    Todo en una transacción: las ventas, el paso de los pedidos a `pagada`, el
    cierre de la sesión y sus comensales, y la liberación de la mesa. Si un pago
    no cubre su parte, no se cierra nada.

    **No escribe movimientos de caja.** `cash_movements` es solo para
    ingresos/egresos manuales; las ventas del turno las deriva `reconcile` desde
    `Payment`. Insertar además un movimiento contaría el dinero dos veces.
    """
    table = None
    try:
        # Sin order_ids a propósito (D2 de research.md): /close siempre cobra y
        # libera TODO lo facturable, ignorando cualquier `data.order_ids` que
        # traiga el body -- el cobro parcial solo existe vía /charge.
        ts, sales = _charge_session(db, table_session_id, data, cashier, invoice_prefix=invoice_prefix, tenant=tenant)
        sessions = checkout.close_table_sessions(db, ts.dining_table_id, closed_by=cashier)

        table = db.get(DiningTable, ts.dining_table_id)
        if table is not None:
            table.status = "libre"
            checkout.delete_orphan_carts(db, sessions)

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Error cerrando la sesión de mesa")
        raise

    # Publica el servicio y no el router porque la cascada necesita los objetos
    # `Sale` con su factura, que solo existen dentro de esta función. Siempre
    # después del COMMIT: es un único commit para toda la cascada, así que hasta
    # aquí nada de esto era definitivo.
    if tenant_id is not None:
        for sale in sales:
            invoice = (
                {"prefix": sale.invoice.prefix, "number": sale.invoice.number}
                if getattr(sale, "invoice", None) else None
            )
            events.payment_completed(
                tenant_id,
                sale_id=sale.id,
                table_session_id=ts.id,
                total=sale.total,
                customer_name=getattr(sale, "customer_name", None),
                billing_mode=data.billing_mode.value,
                invoice=invoice,
            )
            notify_payment_completed(
                db, tenant_id,
                sale_id=sale.id,
                table_session_id=ts.id,
                total=sale.total,
                customer_name=getattr(sale, "customer_name", None),
                billing_mode=data.billing_mode.value,
                invoice=invoice,
                origin_client_id=data.origin_client_id,
            )
        events.session_closed(
            tenant_id,
            table_session_id=ts.id,
            dining_table_id=ts.dining_table_id,
            reason="paid",
        )
        if table is not None:
            events.table_status_changed(
                tenant_id, dining_table_id=table.id, table_number=table.number,
                status="libre",
            )

    return CloseSessionResponse(
        table_session=TableSessionResponse.model_validate(_load(db, table_session_id)),
        sale_ids=[s.id for s in sales],
    )


def charge_session(
    db: Session, table_session_id: UUID, data: CloseSessionIn, cashier: User,
    *, invoice_prefix: str = "", tenant_id: int | None = None, tenant: Tenant | None = None,
) -> CloseSessionResponse:
    """Cobra la sesión SIN cerrarla ni liberar la mesa (a diferencia de
    `close_session`, A-??? spec 147 ciclo 2): un pedido entregado no implica
    que el cliente se vaya al instante, así que cobrar y liberar la mesa son
    dos decisiones separadas del cajero. La mesa se libera después con
    `POST /{id}/release` (`release_paid_session`), que ya acepta exactamente
    este estado -- `payment_status='PAID'` deja de contar la orden como
    "abierta" para ese chequeo, sin tocarlo.
    """
    try:
        ts, sales = _charge_session(
            db, table_session_id, data, cashier, invoice_prefix=invoice_prefix,
            tenant=tenant, order_ids=data.order_ids,
        )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Error cobrando la sesión de mesa")
        raise

    if tenant_id is not None:
        for sale in sales:
            invoice = (
                {"prefix": sale.invoice.prefix, "number": sale.invoice.number}
                if getattr(sale, "invoice", None) else None
            )
            events.payment_completed(
                tenant_id,
                sale_id=sale.id,
                table_session_id=ts.id,
                total=sale.total,
                customer_name=getattr(sale, "customer_name", None),
                billing_mode=data.billing_mode.value,
                invoice=invoice,
            )
            notify_payment_completed(
                db, tenant_id,
                sale_id=sale.id,
                table_session_id=ts.id,
                total=sale.total,
                customer_name=getattr(sale, "customer_name", None),
                billing_mode=data.billing_mode.value,
                invoice=invoice,
                origin_client_id=data.origin_client_id,
            )
        # A propósito SIN events.session_closed ni table_status_changed: la
        # sesión sigue activa y la mesa sigue ocupada -- eso lo decide
        # `release_paid_session` después, no este cobro.

    return CloseSessionResponse(
        table_session=TableSessionResponse.model_validate(_load(db, table_session_id)),
        sale_ids=[s.id for s in sales],
    )


def release_paid_session(
    db: Session, table_session_id: UUID, cashier: User,
    *, tenant_id: int | None = None,
) -> ReleaseSessionResponse:
    """Libera una mesa manualmente (spec 028, T027; criterio reescrito spec
    147, FR-015/FR-016/FR-017) — único camino que pone la mesa en `'libre'`
    por una sesión con actividad real; ningún temporizador lo hace (FR-015,
    SC-005; el barrido de sesiones huérfanas es un mecanismo distinto para
    sesiones ya sin comensales, research.md R6).

    Se rechaza si existe alguna orden **abierta** (`open_orders`, FR-016):
    `'pendiente'`, o `'entregada'` con `payment_status == 'UNPAID'`. El 409
    incluye esa lista (id, status, payment_status) para que el cajero las
    cobre o cancele sin buscarlas por separado (FR-017/SC-004).

    Aun con todas las órdenes cerradas, la comida puede seguir en curso en
    cocina sobre un pedido `'entregada'` con un ítem `'pendiente'` nacido
    después de la entrega (FR-002a): se cargan **todos los pedidos de la
    sesión salvo los cancelados** y se reusa `_assert_closable` sin modificar
    para que ese chequeo de cocina siga aplicando.

    Bugfix (spec 050): un pedido `'cancelada'` no anula el `estado_cocina` de
    sus ítems (`cancel_order`, `orders/checkout.py` — deliberado, para no
    interferir con el ajuste de inventario), así que antes de excluirlo aquí,
    un ítem que se quedó `'pendiente'` al cancelar su pedido bloqueaba esta
    función para siempre, sin ninguna acción posible desde la UI (el pedido ya
    es terminal).
    """
    ts = _load(db, table_session_id, lock=True)
    if ts.status != "active":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"La sesión ya está {ts.status}"
        )

    abiertas = open_orders(db, ts.id)
    if abiertas:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "Hay órdenes abiertas en esta mesa; cóbralas o cancélalas antes de liberarla.",
                "orders": [
                    {"id": str(o.id), "status": o.status, "payment_status": o.payment_status}
                    for o in abiertas
                ],
            },
        )

    if has_pending_balance(db, ts.id):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Hay un saldo pendiente por cobrar",
        )

    orders = db.execute(
        select(CustomerOrder)
        .options(selectinload(CustomerOrder.items))
        .where(
            CustomerOrder.table_session_id == ts.id,
            CustomerOrder.status != "cancelada",
        )
    ).scalars().all()
    _assert_closable(db, orders)

    try:
        sessions = checkout.close_table_sessions(db, ts.dining_table_id, closed_by=cashier)

        table = db.get(DiningTable, ts.dining_table_id)
        if table is not None:
            table.status = "libre"
            checkout.delete_orphan_carts(db, sessions)

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Error liberando la sesión ya pagada")
        raise

    if tenant_id is not None:
        events.session_closed(
            tenant_id,
            table_session_id=ts.id,
            dining_table_id=ts.dining_table_id,
            reason="paid",
        )
        if table is not None:
            events.table_status_changed(
                tenant_id, dining_table_id=table.id, table_number=table.number,
                status="libre",
            )

    return ReleaseSessionResponse(dining_table_id=ts.dining_table_id, status="libre")


def _participantes_con_consumo(orders: list[CustomerOrder]) -> set:
    """Comensales con algo que cobrar. `None` agrupa lo que añadió el mesero."""
    return {
        it.participant_id
        for o in orders for it in o.items
        if it.estado_cocina != "cancelado"
    }


# -------------------------------------------- Reparto de la cuenta (staff)

def _unique_label(db: Session, table_session_id: UUID, nombre: str) -> str:
    """Import local: `cart.service` ya importa de este módulo, así que a nivel de
    módulo sería un ciclo (mismo motivo que `issue_for_sale` en `sales/builder.py`)."""
    from app.api.v1.cart.service import unique_display_label

    return unique_display_label(db, table_session_id, nombre)


def add_participant(
    db: Session, table_session_id: UUID, display_name: str,
    *, tenant_id: int | None = None,
) -> SessionParticipant:
    """Crea un comensal desde el POS, sin QR.

    Es lo que permite dividir la cuenta cuando una sola persona pidió por todos: el
    `participant_id` de los ítems solo se poblaba al escanear, así que sin esto el
    desglose tenía una única línea y el cobro dividido era inalcanzable.

    Sin `expires_at` y sin carrito **a propósito**: no hay token que emitir, así que
    nadie puede entrar por el QR como esta persona. Es una etiqueta de cobro.
    """
    ts = _load(db, table_session_id)
    if ts.status != "active":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"La sesión ya está {ts.status}: no se pueden agregar comensales.",
        )

    nombre = display_name.strip()
    if not nombre:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "El nombre no puede estar vacío")

    participant = SessionParticipant(
        table_session_id=ts.id,
        dining_table_id=ts.dining_table_id,
        display_name=nombre,
        # Reutiliza el desambiguador del QR: dos "Ana" en la misma mesa quedan
        # "Ana" y "Ana (2)", vengan de donde vengan.
        display_label=_unique_label(db, ts.id, nombre),
        status="open",
    )
    db.add(participant)
    db.commit()
    db.refresh(participant)
    _notify_bill_changed(ts, tenant_id)
    return participant


def remove_participant(
    db: Session, table_session_id: UUID, participant_id: UUID,
    *, tenant_id: int | None = None,
) -> None:
    """Quita un comensal de la sesión.

    Se niega si tiene consumo: borrarlo dejaría sus líneas apuntando a una fila que ya
    no existe y el cobro fallaría al armar el desglose. Primero hay que reasignar."""
    ts = _load(db, table_session_id)
    participant = db.get(SessionParticipant, participant_id)
    if participant is None or participant.table_session_id != ts.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Comensal no encontrado en esta sesión")

    asignados = db.execute(
        select(func.count(OrderItem.id)).where(OrderItem.participant_id == participant_id)
    ).scalar_one()
    if asignados:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": (
                    f"«{participant.display_label or participant.display_name}» tiene "
                    f"{asignados} producto(s) asignado(s). Reasígnalos antes de quitarlo."
                ),
                "items": asignados,
            },
        )

    db.delete(participant)
    db.commit()
    _notify_bill_changed(ts, tenant_id)


def set_assignments(
    db: Session, table_session_id: UUID, assignments: list,
    *, tenant_id: int | None = None, tenant: Tenant | None = None,
) -> SessionBillResponse:
    """Reparte líneas entre comensales, en una sola transacción.

    Devuelve la cuenta ya recalculada para que el POS no tenga que pedirla aparte.
    """
    ts = _load(db, table_session_id)
    if ts.status != "active":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"La sesión ya está {ts.status}: la cuenta no se puede repartir.",
        )

    validos = {p.id for p in ts.participants}
    # Los ítems cobrables de esta sesión: todo lo demás se rechaza por id, así se
    # evita repartir líneas de otra mesa o de un pedido ya cobrado.
    items = {
        it.id: it
        for it in db.execute(
            select(OrderItem)
            .join(CustomerOrder, CustomerOrder.id == OrderItem.order_id)
            .where(
                CustomerOrder.table_session_id == ts.id,
                CustomerOrder.status != "cancelada",
                CustomerOrder.payment_status != "PAID",
            )
        ).scalars()
    }

    # Una línea de 2 unidades repartida entre dos personas llega como DOS entradas del
    # mismo ítem, así que hay que verlas juntas para poder validar y partir.
    por_item: dict[UUID, list] = {}
    for a in assignments:
        item = items.get(a.order_item_id)
        if item is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"El producto {a.order_item_id} no pertenece a la cuenta de esta mesa.",
            )
        if item.estado_cocina == "cancelado":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Un producto anulado no se cobra, así que no se puede asignar.",
            )
        if a.participant_id is not None and a.participant_id not in validos:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"El comensal {a.participant_id} no es de esta mesa.",
            )
        por_item.setdefault(a.order_item_id, []).append(a)

    for item_id, entradas in por_item.items():
        item = items[item_id]
        pedidas = [(e.quantity if e.quantity is not None else item.quantity) for e in entradas]

        # **La invariante que hace segura esta operación**: partir 2 en 1+1 no cambia
        # lo consumido, y el inventario se descontó al confirmar según esa cantidad.
        # Una suma de más inventaría unidades que nadie pidió; una de menos las haría
        # desaparecer de la cuenta. Ninguna puede pasar en silencio.
        if sum(pedidas) != item.quantity:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"El reparto de este producto suma {sum(pedidas)} unidad(es) y la "
                f"línea tiene {item.quantity}.",
            )

        if len(entradas) == 1:
            item.participant_id = entradas[0].participant_id
            continue

        # Partir un ítem a medio preparar duplicaría la línea con la comida sin
        # terminar. No estorba: cobrar ya exige que esté listo.
        if item.estado_cocina in EN_CURSO:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "No se puede repartir por unidades un producto que cocina aún no ha "
                "terminado. Espera a que esté listo.",
            )

        # La primera entrada se queda en la fila original; las demás nacen como filas
        # nuevas copiando TODO lo que define la línea (precio, sabores, notas, estado):
        # sin las opciones, media comanda perdería su sabor.
        #
        # spec 089 (A-94): los adicionales cobrados una vez por línea (`addons_total` y las
        # opciones `per_line`) se quedan SOLO en la fila original: no se duplican ni se
        # reparten entre los comensales. Las filas nuevas nacen con `addons_total = 0` y sin
        # opciones `per_line`; las opciones por unidad se clonan como siempre. (A-97: este
        # clon pierde además `OrderItemOption.quantity` en las opciones por unidad; defecto
        # previo, documentado y sin corregir aquí.)
        item.participant_id = entradas[0].participant_id
        item.quantity = pedidas[0]
        for entrada, cantidad in zip(entradas[1:], pedidas[1:]):
            nueva = OrderItem(
                order_id=item.order_id,
                participant_id=entrada.participant_id,
                product_variant_id=item.product_variant_id,
                quantity=cantidad,
                unit_price=item.unit_price,
                estado_cocina=item.estado_cocina,
                void_de=item.void_de,
                notes=item.notes,
            )
            db.add(nueva)
            db.flush()
            for opcion in item.options:
                if opcion.per_line:
                    continue
                db.add(OrderItemOption(order_item_id=nueva.id, option_id=opcion.option_id))

    db.commit()
    _notify_bill_changed(ts, tenant_id)
    return compute_bill(db, ts.id, tenant)


def _notify_bill_changed(ts: TableSession, tenant_id: int | None) -> None:
    """Avisa al POS de que el desglose cambió. Best-effort: el reparto ya está
    guardado, y no publicar el evento solo significa que el cajero verá el cambio al
    refrescar."""
    if tenant_id is None:
        return
    try:
        events.bill_changed(tenant_id, table_session_id=ts.id)
    except Exception:
        logger.warning("No se pudo publicar bill_changed de %s", ts.id, exc_info=True)


def _nombre_cuenta(
    db: Session, ts: TableSession, orders: list[CustomerOrder], data: CloseSessionIn
) -> str | None:
    """A nombre de quién se emite la factura de una cuenta unificada.

    Manda lo que escriba el cajero —puede ser una empresa que pide la factura a
    su nombre—; si no escribe nada se usan los comensales de la sesión, y si
    tampoco hay (mesa atendida solo por el mesero) la propia mesa. Una factura
    sin nombre no le sirve a nadie."""
    escrito = (data.customer_name or "").strip()
    if escrito:
        return escrito[:255]

    con_consumo = _participantes_con_consumo(orders)
    nombres = [
        p.display_label or p.display_name
        for p in ts.participants
        if p.id in con_consumo
    ]
    if nombres:
        return ", ".join(sorted(nombres))[:255]

    table = db.get(DiningTable, ts.dining_table_id)
    return f"Mesa {table.number}" if table is not None else None


def _close_unified(
    db: Session, ts: TableSession, orders: list[CustomerOrder],
    data: CloseSessionIn, shift, cashier: User, invoice_prefix: str = "",
    tenant: Tenant | None = None,
) -> Sale:
    """Una sola venta con las líneas de todos los pedidos de la sesión."""
    if not data.payments:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "billing_mode='unified' requiere 'payments'",
        )

    lines = []
    for order in orders:
        lines.extend(checkout.order_sale_lines(db, order.id))

    # spec 073 (FR-012a, A-70): un único instante para la cuenta unificada — el
    # del pedido más antiguo pendiente (o la hora del cobro si ninguno está
    # congelado).
    instant = checkout.promotion_evaluation_instant(orders, now=utc_now())
    promo_discount, final_promotion_id, applied = checkout.auto_discount(db, lines, instant, tenant)

    sale = build_sale(
        db,
        lines=lines,
        shift=shift,
        cashier=cashier,
        payments=data.payments,
        discount=Decimal(data.discount) + promo_discount,
        tax=data.tax, tip=data.tip,
        customer_name=_nombre_cuenta(db, ts, orders, data),
        dining_table_id=ts.dining_table_id,
        table_session_id=ts.id,
        # Una venta unificada cubre a varios comensales: no cuelga de ninguno.
        customer_order_id=orders[0].id if len(orders) == 1 else None,
        promotion_id=final_promotion_id,
        applied_promotions=applied,
        promotion_evaluated_at=instant,  # FR-011a
        invoice_prefix=invoice_prefix,
    )
    # FR-021: el agregado + la lista también en cada CustomerOrder de la sesión.
    for order in orders:
        order.discount = Decimal(data.discount) + promo_discount if len(orders) == 1 else Decimal("0")
        order.applied_promotions = applied
    db.flush()
    return sale


def _close_split(
    db: Session, ts: TableSession, orders: list[CustomerOrder],
    data: CloseSessionIn, shift, cashier: User, invoice_prefix: str = "",
    tenant: Tenant | None = None,
) -> list[Sale]:
    """Una venta por comensal. Se exige un bloque de pago por cada comensal con
    consumo: si falta uno, la mesa quedaría medio cobrada."""
    if not data.splits:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "billing_mode='split' requiere 'splits'",
        )

    # En split cada bloque lleva sus propios importes. Los de raíz se ignoraban en
    # silencio: quien mandaba una propina ahí la perdía sin enterarse.
    if data.discount or data.tax or data.tip or data.payments:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "En billing_mode='split', el descuento, impuesto, propina y pagos van "
            "dentro de cada bloque de 'splits', no en la raíz.",
        )

    # Un comensal repetido pasaría los dos chequeos de cobertura (que trabajan con
    # conjuntos) y luego el bucle cobraría SUS MISMAS LÍNEAS dos veces: dos ventas,
    # dos facturas y dos consecutivos por el mismo consumo. Se corta aquí.
    vistos: list[UUID | None] = [s.participant_id for s in data.splits]
    if len(set(vistos)) != len(vistos):
        repetidos = {p for p in vistos if vistos.count(p) > 1}
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "Hay comensales repetidos en el split: cada uno paga una sola vez.",
                "participant_ids": [str(p) if p else None for p in repetidos],
            },
        )

    con_consumo = _participantes_con_consumo(orders)
    cubiertos = set(vistos)
    faltan = con_consumo - cubiertos
    if faltan:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "Faltan comensales por cobrar en el split.",
                "participant_ids": [str(p) if p else None for p in faltan],
            },
        )
    sobran = cubiertos - con_consumo
    if sobran:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "El split incluye comensales sin consumo.",
                "participant_ids": [str(p) if p else None for p in sobran],
            },
        )

    labels = {p.id: (p.display_label or p.display_name) for p in ts.participants}
    table = db.get(DiningTable, ts.dining_table_id)
    # El bloque sin comensal (lo que tecleó el mesero) no tiene etiqueta, y sin esto
    # su venta y su factura salían literalmente sin nombre. `unified` ya evitaba eso
    # con `_nombre_cuenta`; aquí faltaba el mismo cuidado.
    sin_asignar = f"Mesa {table.number}" if table is not None else "Sin asignar"

    # spec 073 (FR-012a, A-70): un único instante para toda la cuenta dividida —
    # el del pedido más antiguo pendiente (o la hora del cobro si ninguno está
    # congelado). Cada comensal sigue evaluando sobre sus propias líneas.
    instant = checkout.promotion_evaluation_instant(orders, now=utc_now())
    sales: list[Sale] = []
    for bloque in data.splits:
        lines = []
        for order in orders:
            lines.extend(checkout.order_sale_lines(
                db, order.id, participant_id=bloque.participant_id
            ))

        # Cada comensal evalúa promociones sobre sus propias líneas: nada se
        # mezcla entre comensales (research.md D7). El importe del descuento
        # automático en el split lo maneja el caller vía `bloque.discount`, igual
        # que antes — aquí se derivan `promotion_id` y la lista `applied` para el
        # snapshot de la Sale/Invoice (FR-021). El `CustomerOrder` no se toca en
        # split: una orden puede abarcar a varios comensales, su descuento por
        # orden no está definido — la Sale y la Invoice son las autoritativas.
        _, final_promotion_id, applied = checkout.auto_discount(db, lines, instant, tenant)

        sales.append(build_sale(
            db,
            lines=lines,
            shift=shift,
            cashier=cashier,
            payments=bloque.payments,
            discount=bloque.discount, tax=bloque.tax, tip=bloque.tip,
            customer_name=labels.get(bloque.participant_id) or sin_asignar,
            dining_table_id=ts.dining_table_id,
            table_session_id=ts.id,
            participant_id=bloque.participant_id,
            promotion_id=final_promotion_id,
            applied_promotions=applied,
            promotion_evaluated_at=instant,  # FR-011a
            invoice_prefix=invoice_prefix,
        ))
    return sales
