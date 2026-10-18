from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core import events
from app.core.db import get_db, get_tenant
from app.core.notifications.dispatch import notify_payment_completed
from app.core.crud import get_or_404
from app.core.error_middleware import current_request_id
from app.core.http_cache import json_or_304
from app.core.dependencies import get_current_user, require_tenant_admin
from app.core.models import User, Tenant
from app.core.plan_limits import enforce_plan_limit
from app.core.pagination import Page, paginate
from app.core.qr_token import mint_qr_token
from app.models.dining_table import DiningTable
from app.models.customer_order import CustomerOrder
from app.models.order_item import OrderItem
from app.models.order_payment_attempt import OrderPaymentAttempt
from app.api.v1.orders import additions
from app.api.v1.orders import service
from app.api.v1.orders.consolidation import consolidate_table, add_item_to_order
from app.api.v1.orders import kitchen
from app.api.v1.orders import checkout
from app.api.v1.orders import tables_advanced
from app.api.v1.table_sessions.service import notify_sessions_closed
from app.api.v1.catalog.schemas import OptionSelectionIn, UpdateAdditionQuantityIn
from app.api.v1.sales import complementary
from app.api.v1.sales.schemas import ComplementaryPaymentIn, SaleResponse
from app.api.v1.orders.schemas import (
    TableCreate, TableUpdate, TableResponse, TableQrTokenResponse,
    OrderCreate, OrderResponse, OrderItemIn, OrderType,
    OrderItemResponse, KitchenTransitionIn, VoidItemIn,
    BlockIn, CancelIn, CheckoutAndSendIn, PayIn, BillResponse,
    CheckoutPreviewResponse, DraftPreviewIn,
    TableStatusUpdate, MoveOrderIn, MergeOrdersIn, MergeResponse, GroupBillResponse,
    PaymentAttemptResponse, PaymentAttemptApproveIn, PaymentAttemptRejectIn,
    PaymentAttemptConfirmCashIn,
)

router = APIRouter(prefix="/orders", tags=["orders"])

# A nivel de módulo a propósito: `TypeAdapter` compila el esquema, y construirlo
# por request tiraría por tierra el ahorro que persigue el ETag.
_ORDERS_ADAPTER = TypeAdapter(list[OrderResponse])


def _load_order(db: Session, order_id: UUID) -> CustomerOrder:
    order = db.execute(
        select(CustomerOrder)
        .options(
            selectinload(CustomerOrder.items).selectinload(OrderItem.options),
            selectinload(CustomerOrder.payment_attempts)
            .selectinload(OrderPaymentAttempt.payment_method),
        )
        .where(CustomerOrder.id == order_id)
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    order.paid = service.order_has_sale(db, order.id)  # spec 029, D2
    # spec 123: saldo pendiente generado por adiciones agregadas después de que
    # el pedido ya tenía una `Sale` emitida, aún no cubiertas por un pago
    # complementario. Computado (no una columna), 0 cuando no aplica.
    order.pending_balance = service.order_pending_balance(db, order.id)
    if order.user_id is not None:
        # spec 076, Historia 4
        names = service.staff_user_names(db, [order.user_id])
        order.staff_user_name = names.get(order.user_id)
    # spec 094 (FR-024b, research.md D3): el desglose se arma **solo aquí**, en
    # el detalle. `_decorate_orders`/`list_orders` no lo tocan a propósito:
    # calcularlo por fila sobre hasta 100 pedidos por página sería justo el N+1
    # que la guardia de `test_orders_pagination` vigila, y ninguna historia de
    # esta spec lo pide para el listado. Allí `billing` viaja `null`, que la
    # pantalla trata como "no pintar el resumen" — nunca como cero (D15).
    order.billing = service.build_billing_summary(db, order)
    # spec 120 (corrección FR-012): referencia de factura y método de pago
    # reales, resueltos solo aquí -- mismo motivo que `billing` (D3, no se
    # calcula por fila en el listado).
    order.invoice_reference, order.payment_method_name = service.invoice_and_payment_info(db, order.id)
    # spec 121 (FR-001/FR-002): mismo total que `billing.total`, costo cero
    # (ya está calculado) -- disponible también donde `billing` sigue `null`.
    order.total = order.billing.total
    return order


# ============================ Mesas (staff) ============================
@router.get(
    "/tables",
    response_model=list[TableResponse] | Page[TableResponse],
    summary="Listar mesas",
)
def list_tables(
    page: int | None = Query(None, ge=1),
    size: int | None = Query(None, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Sin `page` ni `size`: array `ORDER BY number` idéntico a hoy (FR-023/
    FR-025 — Terminal, Dashboard, detalle de orden y hoja de QR lo consumen
    completo). Con `page` y/o `size`: `Page[TableResponse]` con el mismo orden
    (`number` es `UNIQUE`, sin desempate) y el clamp de página fuera de rango
    de spec 079 (contracts/tables-list-api.md)."""
    if page is None and size is None:
        return db.execute(select(DiningTable).order_by(DiningTable.number)).scalars().all()
    stmt = select(DiningTable).order_by(DiningTable.number.asc())
    effective_size = size or 20
    effective_page = service.clamp_page(db, stmt, page or 1, effective_size)
    return paginate(db, stmt, effective_page, effective_size)


@router.post("/tables", response_model=TableResponse, status_code=status.HTTP_201_CREATED, summary="Crear mesa (genera qr_token)")
def create_table(
    body: TableCreate,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_tenant),
    _: User = Depends(require_tenant_admin),
):
    dup = db.execute(select(DiningTable).where(DiningTable.number == body.number)).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ya existe una mesa con ese número")
    enforce_plan_limit(db, tenant, "mesas")  # spec 033, FR-005/FR-006
    table = DiningTable(number=body.number, name=body.name)
    db.add(table)
    db.commit()
    db.refresh(table)
    return table


@router.patch("/tables/{table_id}", response_model=TableResponse, summary="Actualizar mesa")
def update_table(
    table_id: UUID, body: TableUpdate,
    db: Session = Depends(get_db), _: User = Depends(require_tenant_admin),
):
    table = get_or_404(db, DiningTable, table_id, "Table not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(table, k, v)
    db.commit()
    db.refresh(table)
    return table


# ============================ Mesas avanzado (RF-051..054) ============================
@router.patch("/tables/{table_id}/status", response_model=TableResponse,
              summary="Cambiar estado operativo de la mesa (RF-051)")
def set_table_status(
    table_id: UUID, body: TableStatusUpdate,
    db: Session = Depends(get_db), _: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    table = tables_advanced.set_table_status(db, table_id, body.status)
    events.table_status_changed(
        tenant.id, dining_table_id=table.id, table_number=table.number, status=table.status,
    )
    return table


@router.post("/{order_id}/move", response_model=OrderResponse,
             summary="Cambiar una orden de mesa (RF-052)")
def move_order(
    order_id: UUID, body: MoveOrderIn,
    db: Session = Depends(get_db), _: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    tables_advanced.move_order(db, order_id, body.dining_table_id)
    order = _load_order(db, order_id)
    # Cambian las dos mesas: la que suelta el pedido y la que lo recibe.
    for table in db.execute(
        select(DiningTable).where(DiningTable.id == body.dining_table_id)
    ).scalars():
        events.table_status_changed(
            tenant.id, dining_table_id=table.id, table_number=table.number,
            status=table.status,
        )
    return order


@router.post("/merge", response_model=MergeResponse,
             summary="Unir mesas en una sola cuenta (RF-053)")
def merge_orders(
    body: MergeOrdersIn,
    db: Session = Depends(get_db), _: User = Depends(get_current_user),
):
    return tables_advanced.merge_orders(db, body.order_ids)


@router.get("/group/{group_id}/bill", response_model=GroupBillResponse,
            summary="Cuenta consolidada de un grupo de mesas (RF-053)")
def group_bill(
    group_id: UUID,
    db: Session = Depends(get_db), _: User = Depends(get_current_user),
):
    return tables_advanced.group_bill(db, group_id)


@router.get(
    "/tables/{table_id}/qr-token",
    response_model=TableQrTokenResponse,
    summary="Emitir token firmado del QR de la mesa (imprimible)",
)
def issue_table_qr_token(
    table_id: UUID,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_tenant),
    _: User = Depends(require_tenant_admin),
):
    table = get_or_404(db, DiningTable, table_id, "Table not found")
    token = mint_qr_token(tenant.id, table.id)
    return TableQrTokenResponse(
        table_id=table.id,
        number=table.number,
        qr_token=token,
        menu_path=f"/api/v1/menu/qr-token/{token}",
    )


# NOTA: los endpoints legacy de sesión de mesa se eliminaron.
# - `POST /sessions` (qr_token UUID plano + header x-tenant-host) → `POST /cart/sessions`,
#   que autentica con el token de QR firmado y devuelve token de sesión + carrito.
#   Además su manejo de 409 era código muerto: esperaba un IntegrityError que
#   ninguna constraint producía.
# - `POST /sessions/{id}/close` → el cierre ahora es de la sesión de **mesa**
#   completa, en el router de `table_sessions`.


@router.post(
    "/{order_id}/items",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Agregar un ítem directo a un pedido específico (mesero)",
)
def add_order_item(
    order_id: UUID,
    body: OrderItemIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    order = add_item_to_order(db, order_id, body, user)
    return _load_order(db, order.id)


# ============================ Confirmación (staff) ============================
@router.post(
    "/{order_id}/confirm",
    response_model=OrderResponse,
    summary="Confirmar un pedido recibido del QR (descuenta inventario)",
)
def confirm_order(
    order_id: UUID,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    """`recibida` → `abierta`. Es el único punto donde el pedido del comensal
    compromete stock; hasta aquí no había tocado inventario."""
    checkout.confirm_order(db, order_id, user, request_id=current_request_id(request))
    order = _load_order(db, order_id)
    events.order_confirmed(
        tenant.id, order_id=order.id, table_session_id=order.table_session_id
    )
    events.bill_changed(tenant.id, table_session_id=order.table_session_id)
    return order


# ============================ Intentos de pago (cajero, spec 024) ============================
@router.get(
    "/{order_id}/payment-attempts",
    response_model=list[PaymentAttemptResponse],
    summary="Historial de intentos de pago de una orden (cajero/back-office)",
)
def list_payment_attempts(
    order_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user),
):
    return checkout.list_payment_attempts(db, order_id)


@router.post(
    "/payment-attempts/{attempt_id}/approve",
    response_model=PaymentAttemptResponse,
    summary="Aprobar un comprobante de transferencia (cajero)",
)
def approve_payment_attempt(
    attempt_id: UUID, body: PaymentAttemptApproveIn, request: Request = None,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    return checkout.approve_payment_attempt(
        db, attempt_id, body.cash_shift_id, user,
        request_id=current_request_id(request),
    )


@router.post(
    "/payment-attempts/{attempt_id}/reject",
    response_model=PaymentAttemptResponse,
    summary="Rechazar un comprobante de transferencia con motivo (cajero)",
)
def reject_payment_attempt(
    attempt_id: UUID, body: PaymentAttemptRejectIn, request: Request = None,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    return checkout.reject_payment_attempt(
        db, attempt_id, body.reason, user, request_id=current_request_id(request)
    )


@router.post(
    "/payment-attempts/{attempt_id}/confirm-cash",
    response_model=PaymentAttemptResponse,
    summary="Confirmar un pago en efectivo y calcular el cambio (cajero)",
)
def confirm_cash_payment_attempt(
    attempt_id: UUID, body: PaymentAttemptConfirmCashIn, request: Request = None,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    return checkout.confirm_cash_payment_attempt(
        db, attempt_id, body.amount_received, body.cash_shift_id, user,
        request_id=current_request_id(request),
    )


# ============================ Consolidación (mesero) ============================
@router.post(
    "/tables/{table_id}/consolidate",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Consolidar carritos de la mesa en la orden abierta (mesero)",
)
def consolidate(
    table_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    order = consolidate_table(db, table_id, user)
    return _load_order(db, order.id)


# ============================ Preparación ============================
@router.patch(
    "/items/{item_id}/kitchen",
    response_model=OrderItemResponse,
    summary="Avanzar la preparación de un ítem (pendiente→en_preparacion→listo)",
)
def kitchen_transition(
    item_id: UUID, body: KitchenTransitionIn,
    db: Session = Depends(get_db), _: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    """Devuelve `rt_v`: la versión del evento que acaba de emitir esta escritura.

    Sirve para que una pantalla que parchea el ítem en local (escritura
    optimista) pueda descartar un evento en vuelo de otra terminal que llegue
    justo después y revierta visualmente lo que el usuario ya vio.

    `pendiente → listo` es un salto legal: quien toma el pedido es quien lo
    prepara, así que la terminal lo marca de un toque.
    """
    item = kitchen.transition_kitchen(db, item_id, body)
    order = db.get(CustomerOrder, item.order_id)
    entry_id = events.item_kitchen_changed(
        tenant.id,
        order_id=item.order_id,
        table_session_id=order.table_session_id if order else None,
        item_id=item.id,
        estado_cocina=item.estado_cocina,
    )
    out = OrderItemResponse.model_validate(item)
    if entry_id is not None:
        out.rt_v = events.version_of(entry_id)
    return out


@router.post(
    "/{order_id}/ready",
    response_model=OrderResponse,
    summary="Marcar como listos todos los ítems en curso de la orden",
)
def order_ready(
    order_id: UUID,
    db: Session = Depends(get_db), _: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    """Lo que la terminal necesita para cobrar sin ir ítem por ítem.

    Emite un evento por ítem cambiado en lugar de uno agregado: las pantallas ya
    escuchan `order.item_kitchen_changed`, y un tipo nuevo obligaría a tocarlas
    todas para no ganar nada."""
    order, cambiados = kitchen.mark_order_ready(db, order_id)
    for it in cambiados:
        events.item_kitchen_changed(
            tenant.id,
            order_id=order.id,
            table_session_id=order.table_session_id,
            item_id=it.id,
            estado_cocina=it.estado_cocina,
        )
    return _load_order(db, order.id)


@router.post(
    "/items/{item_id}/void",
    response_model=OrderResponse,
    summary="Anular (y opcionalmente reemplazar) un ítem de la orden",
)
def void_item(
    item_id: UUID, body: VoidItemIn,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    order = kitchen.void_item(db, item_id, body, user)
    reemplazo = next((i for i in order.items if i.void_de == item_id), None)
    events.item_voided(
        tenant.id,
        order_id=order.id,
        table_session_id=order.table_session_id,
        item_id=item_id,
        motivo=body.motivo,
        replacement_item_id=reemplazo.id if reemplazo else None,
    )
    events.bill_changed(tenant.id, table_session_id=order.table_session_id)
    return _load_order(db, order.id)


@router.post(
    "/items/{item_id}/additions",
    response_model=OrderResponse,
    summary="Agregar una adición a una línea de pedido ya creada (spec 123)",
)
def add_addition(
    item_id: UUID, body: OptionSelectionIn,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    order = additions.add_addition_to_order_item(db, item_id, body, user, tenant)
    item = next((i for i in order.items if i.id == item_id), None)
    option_name = None
    if item is not None:
        fila = next((o for o in item.options if o.option_id == body.option_id), None)
        option_name = fila.name if fila is not None else None
    events.item_addon_added(
        tenant.id,
        order_id=order.id,
        table_session_id=order.table_session_id,
        item_id=item_id,
        option_name=option_name,
        quantity=body.quantity,
    )
    events.bill_changed(tenant.id, table_session_id=order.table_session_id)
    return _load_order(db, order.id)


@router.patch(
    "/items/{item_id}/additions/{option_id}",
    response_model=OrderResponse,
    summary="Editar la cantidad de una adición que ya está en la línea (spec 123)",
)
def update_addition(
    item_id: UUID, option_id: UUID, body: UpdateAdditionQuantityIn,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    order = additions.update_addition_quantity(db, item_id, option_id, body, user, tenant)
    item = next((i for i in order.items if i.id == item_id), None)
    option_name = None
    new_quantity = body.quantity
    if item is not None:
        fila = next((o for o in item.options if o.option_id == option_id), None)
        if fila is not None:
            option_name = fila.name
            new_quantity = fila.quantity
    # Mismo evento que agregar una adición -- el frontend solo lo usa como señal de
    # recarga (`pos-terminal.store.ts`), no lee el payload.
    events.item_addon_added(
        tenant.id,
        order_id=order.id,
        table_session_id=order.table_session_id,
        item_id=item_id,
        option_name=option_name,
        quantity=new_quantity,
    )
    events.bill_changed(tenant.id, table_session_id=order.table_session_id)
    return _load_order(db, order.id)


@router.delete(
    "/items/{item_id}/additions/{option_id}",
    response_model=OrderResponse,
    summary="Quitar del todo una adición que ya está en la línea (spec 123)",
)
def remove_addition(
    item_id: UUID, option_id: UUID,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    order = additions.remove_addition(db, item_id, option_id, user, tenant)
    # Mismo evento que agregar/editar una adición -- el frontend solo lo usa como señal
    # de recarga (`pos-terminal.store.ts`), no lee el payload.
    events.item_addon_added(
        tenant.id,
        order_id=order.id,
        table_session_id=order.table_session_id,
        item_id=item_id,
        option_name=None,
        quantity=0,
    )
    events.bill_changed(tenant.id, table_session_id=order.table_session_id)
    return _load_order(db, order.id)


# ============================ Cobro / cierre (Fase 7) ============================
@router.get(
    "/tables/{table_id}/bill",
    response_model=BillResponse,
    summary="Cuenta de la mesa (total + split por comensal)",
)
def table_bill(table_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return checkout.compute_bill(db, table_id)


@router.get(
    "/{order_id}/checkout-preview",
    response_model=CheckoutPreviewResponse,
    summary="Desglose autoritativo del cobro de un pedido (spec 073)",
)
def checkout_preview(
    order_id: UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    """spec 073, FR-001/FR-004: `{subtotal, discount, delivery_fee, total,
    promotion_evaluated_at}` calculado por la misma autoridad que emite la
    venta. Solo lectura — no bloquea el pedido ni exige turno de caja. `409`
    si el pedido ya está pagado o cancelado."""
    return checkout.compute_checkout_preview(db, order_id, tenant)


@router.post(
    "/draft-preview",
    response_model=CheckoutPreviewResponse,
    summary="Desglose de un borrador de orden manual sin guardar (spec 073)",
)
def draft_preview(
    body: DraftPreviewIn,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    """spec 073, FR-013: mismo desglose para un borrador que todavía no existe
    como pedido — el frontend manda las mismas líneas que usará para el
    `POST /orders` real. `promotion_evaluated_at` es la hora de la llamada (sin
    congelar). Sin `404`/`409`: no hay ningún recurso persistido."""
    return checkout.compute_draft_preview(db, body, tenant)


@router.post(
    "/{order_id}/block",
    response_model=OrderResponse,
    summary="Bloquear orden para cobro (lock optimista + validación de cocina)",
)
def block_order(
    order_id: UUID, body: BlockIn,
    db: Session = Depends(get_db), _: User = Depends(get_current_user),
):
    order = checkout.block_order(db, order_id, body)
    return _load_order(db, order.id)


@router.post(
    "/{order_id}/complementary-payment",
    response_model=SaleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Cobrar el saldo pendiente de un pedido como pago complementario (spec 123)",
)
def pay_complementary_balance(
    order_id: UUID, body: ComplementaryPaymentIn,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    sale = complementary.settle_pending_balance(db, order_id, body, user, tenant)
    order = db.get(CustomerOrder, order_id)
    events.bill_changed(tenant.id, table_session_id=order.table_session_id if order else None)
    events.payment_completed(
        tenant.id,
        sale_id=sale.id,
        table_session_id=order.table_session_id if order else None,
        total=sale.total,
        customer_name=order.customer_name if order else None,
        billing_mode="counter",
    )
    return sale


@router.post(
    "/{order_id}/pay",
    response_model=SaleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Cobrar orden bloqueada (crea venta, marca pagada)",
)
def pay_order(
    order_id: UUID, body: PayIn,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    sale = checkout.pay_order(db, order_id, body, user, tenant)
    order = db.get(CustomerOrder, order_id)
    events.payment_completed(
        tenant.id,
        sale_id=sale.id,
        table_session_id=order.table_session_id if order else None,
        total=sale.total,
        customer_name=order.customer_name if order else None,
        # Cobro pedido a pedido desde el mostrador, no cierre de sesión de mesa.
        billing_mode="counter",
    )
    notify_payment_completed(
        db, tenant.id,
        sale_id=sale.id,
        table_session_id=order.table_session_id if order else None,
        total=sale.total,
        customer_name=order.customer_name if order else None,
        billing_mode="counter",
        origin_client_id=body.origin_client_id,
    )
    return sale


@router.post(
    "/{order_id}/checkout-and-send",
    response_model=SaleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Cobrar y enviar a cocina en un solo paso (comanda 'hold_for_payment')",
)
def checkout_and_send(
    order_id: UUID, body: CheckoutAndSendIn, request: Request = None,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    """Solo para comandas creadas con `hold_for_payment=True` (status
    'recibida'): cobra la orden y, en la misma transacción, la envía a cocina
    (descuenta inventario) — spec 028, T016."""
    sale = checkout.checkout_and_send(
        db, order_id, body, user, request_id=current_request_id(request), tenant=tenant
    )
    order = db.get(CustomerOrder, order_id)
    events.payment_completed(
        tenant.id,
        sale_id=sale.id,
        table_session_id=order.table_session_id if order else None,
        total=sale.total,
        customer_name=order.customer_name if order else None,
        billing_mode="counter",
    )
    notify_payment_completed(
        db, tenant.id,
        sale_id=sale.id,
        table_session_id=order.table_session_id if order else None,
        total=sale.total,
        customer_name=order.customer_name if order else None,
        billing_mode="counter",
        origin_client_id=body.origin_client_id,
    )
    if order is not None:
        events.order_confirmed(
            tenant.id, order_id=order.id, table_session_id=order.table_session_id
        )
        events.bill_changed(tenant.id, table_session_id=order.table_session_id)
    return sale


@router.post(
    "/{order_id}/cancel",
    response_model=OrderResponse,
    summary="Cancelar pedido (reversa parcial de inventario + auditoría)",
)
def cancel_order(
    order_id: UUID, body: CancelIn, request: Request = None,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    """El staff puede cancelar en cualquier estado no terminal. Solo vuelve al stock
    lo que cocina no llegó a preparar; lo ya consumido se registra como pérdida."""
    order = checkout.cancel_order(
        db, order_id, body, user, tenant_id=tenant.id,
        request_id=current_request_id(request),
    )
    events.order_cancelled(
        tenant.id, order_id=order.id, table_session_id=order.table_session_id,
        motivo=body.motivo,
    )
    events.bill_changed(tenant.id, table_session_id=order.table_session_id)
    return _load_order(db, order.id)


@router.post(
    "/tables/{table_id}/release",
    response_model=TableResponse,
    summary="Liberar mesa (regla dura: cero órdenes no-terminales)",
)
def release_table(
    table_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    closed: list = []
    table = checkout.release_table(db, table_id, closed_by=user, closed_out=closed)
    events.table_status_changed(
        tenant.id, dining_table_id=table.id, table_number=table.number, status=table.status,
    )
    # spec 089 (A-95): "Liberar mesa" cierra la sesión: el comensal recibe su pantalla de gracias.
    # Después del commit (que ya ocurrió dentro de `release_table`), nunca antes.
    notify_sessions_closed(tenant.id, closed, reason="released")
    return table


# ============================ Facturación ============================
# Los endpoints de generación manual se eliminaron: la factura se emite sola al
# cobrar, dentro de la transacción de la venta (`sales/builder.py:build_sale`).
#
# Además partían del **pedido**, que es la unidad equivocada: `Invoice.sale_id` es
# único —una factura por venta— y tras el cobro por sesión un cierre `split` emite
# N ventas, mientras que una venta de mostrador no cuelga de ningún pedido. Por eso
# 12 de 20 ventas reales eran imposibles de facturar por esa vía.
#
# Consulta e impresión: `GET /invoices` y `GET /invoices/{id}`.


# ============================ Comandas ============================
@router.post("", response_model=OrderResponse, status_code=status.HTTP_201_CREATED, summary="Crear comanda (staff)")
def create_order(
    body: OrderCreate,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Alta de comanda por staff (mostrador/mesero). **No es una ruta pública**:
    el pedido anónimo por QR entra por el carrito del comensal, no por aquí.

    Antes era anónima (solo header `x-tenant-host`, falsificable) y además no
    descontaba inventario, así que una comanda creada aquí y cobrada con
    `pay_order` nunca descontaba stock."""
    order = service.create_order(
        db, body, user_id=user.id, user=user, request_id=current_request_id(request)
    )
    return _load_order(db, order.id)


def _decorate_orders(db: Session, orders: list[CustomerOrder]) -> None:
    """Asigna `paid` y `staff_user_name` en bloque sobre `orders` (spec 029 D2,
    spec 076 Historia 4) — una consulta para todo el lote, no una por pedido."""
    paid_ids = service.paid_order_ids(db, [o.id for o in orders])
    staff_ids = [o.user_id for o in orders if o.user_id is not None]
    staff_names = service.staff_user_names(db, staff_ids)
    for o in orders:
        o.paid = o.id in paid_ids
        o.staff_user_name = staff_names.get(o.user_id) if o.user_id is not None else None


@router.get(
    "",
    response_model=list[OrderResponse] | Page[OrderResponse],
    summary="Listar comandas (staff)",
)
def list_orders(
    request: Request,
    status_filter: str | None = Query(None, alias="status"),
    order_type: OrderType | None = Query(None),
    active_sessions_only: bool = Query(False),
    page: int | None = Query(None, ge=1),
    size: int | None = Query(None, ge=1, le=100),
    date_from: date | None = Query(None, description="Fecha inicial (inclusive) sobre created_at."),
    date_to: date | None = Query(None, description="Fecha final (inclusive) sobre created_at."),
    q: str | None = Query(None, description="Búsqueda por nombre de cliente o identificador de la orden."),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_tenant),
    _: User = Depends(get_current_user),
):
    """Dos ramas (spec 079, contracts/orders-list-api.md):

    **Sin `page` ni `size`** — camino de hoy, byte a byte: `service.list_orders`
    con `ORDER BY created_at DESC`, `status` **crudo**, `paid`/`staff_user_name`
    en bloque, respuesta vía `json_or_304` (`ETag` + `304`). La terminal sondea
    esto, así que el `ETag` le ahorra el re-render; `active_sessions_only` (spec
    029) mantiene su semántica.

    **Con `page` y/o `size`** — pantalla "Órdenes": `service.list_orders_query`
    (orden `created_at DESC, id DESC`, `status` como "estado que ve la persona",
    `order_type` opcional, `date_from`/`date_to`/`q` opcionales — spec 120,
    research.md §1-§2) paginado con `paginate` + `clamp_page`; `paid` y
    `staff_user_name` solo sobre los `items` de la página; **sin `ETag`** (la
    pantalla se recarga a mano); `active_sessions_only` se ignora (ningún
    consumidor legítimo lo combina con paginación). `date_from`/`date_to`/`q`
    solo aplican a esta rama — la rama sin paginar no los recibe."""
    if page is None and size is None:
        orders = service.list_orders(db, status_filter, active_sessions_only)
        _decorate_orders(db, orders)
        return json_or_304(request, _ORDERS_ADAPTER, orders)

    # Rama paginada: `status` se valida contra el enum cerrado de 6 valores
    # (5 + ausente) y se interpreta como "estado que ve la persona" (FR-016).
    if status_filter is not None and status_filter not in service.ESTADOS_MOSTRADOS_ORDENES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"status debe ser uno de {', '.join(service.ESTADOS_MOSTRADOS_ORDENES)}.",
        )
    stmt = service.list_orders_query(
        tenant=tenant,
        status_mostrado=status_filter,
        order_type=order_type.value if order_type is not None else None,
        date_from=date_from,
        date_to=date_to,
        q=q,
    )
    effective_size = size or 20
    effective_page = service.clamp_page(db, stmt, page or 1, effective_size)
    result = paginate(db, stmt, effective_page, effective_size)
    _decorate_orders(db, result["items"])
    # spec 121 (FR-001/FR-002): `total` en bloque, solo en esta rama paginada
    # -- la rama sin paginar (arriba, `service.list_orders`) no se toca.
    totales = service.order_totals(db, result["items"])
    for o in result["items"]:
        o.total = totales.get(o.id)
    return result


@router.get("/{order_id}", response_model=OrderResponse, summary="Obtener una comanda")
def get_order(order_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return _load_order(db, order_id)


# `PATCH /{order_id}/status` se eliminó a propósito: asignaba cualquier estado sin
# validar la transición y sin tocar inventario, así que permitía pasar un pedido de
# 'recibida' a 'abierta' esquivando `confirm_order` —el único punto que descuenta
# stock— y dejaba el inventario sobrestimado sin que nadie se enterara.
#
# Cada transición legítima tiene su endpoint, con sus reglas:
#   recibida → abierta    POST /orders/{id}/confirm      (descuenta inventario)
#   abierta  → bloqueada  POST /orders/{id}/block        (valida la preparación)
#   → pagada              POST /table-sessions/{id}/close (cobra y libera la mesa)
#   → cancelada           POST /orders/{id}/cancel       (revierte lo no preparado)
