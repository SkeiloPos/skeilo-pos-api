"""Carrito borrador del comensal. Flujo público del QR: sin auth de negocio.

`POST /cart/sessions` autentica por el token de QR firmado; el resto de
operaciones por el token de sesión (`x-session-token`) vía get_session_context.
El tenant y la mesa salen siempre del token firmado, nunca de un id del body.
"""
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import TypeAdapter
from sqlalchemy import select

from app.core import events
from app.core.error_middleware import current_request_id
from app.core.notifications.dispatch import notify_order_created
from app.core.http_cache import json_or_304
from app.core.qr_context import (
    open_qr_context,
    open_session_context,
    get_session_context,
    SessionContext,
)
from app.core.rate_limit import enforce as rate_limit
from app.models.dining_table import DiningTable
from app.api.v1.cart import service
from app.api.v1.orders.schemas import OrderResponse
from app.api.v1.cart.schemas import (
    SessionOpenIn, SessionOpenResponse,
    CartItemIn, CartItemUpdate, CartResponse, MyOrderCancelIn,
    DinerPaymentMethod, PaymentAttemptCreateIn, DinerPaymentAttempt,
    ReceiptPresignIn, ReceiptPresignOut, ReceiptAttachIn,
    SubmitCartIn, PaymentReceiptPresignIn,
)

router = APIRouter(prefix="/cart", tags=["cart"])

# A nivel de módulo: compilar el esquema por request anularía el ahorro.
_ORDERS_ADAPTER = TypeAdapter(list[OrderResponse])


def _table_number(db, dining_table_id) -> int | None:
    """El número de mesa para pintar el aviso del staff sin que tenga que
    resolver el id contra su catálogo."""
    if dining_table_id is None:
        return None
    return db.execute(
        select(DiningTable.number).where(DiningTable.id == dining_table_id)
    ).scalar_one_or_none()


@router.post(
    "/sessions",
    response_model=SessionOpenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Unirse a la mesa por QR (devuelve token de sesión)",
)
async def open_session(body: SessionOpenIn, request: Request):
    # Por IP ANTES de verificar el token: si no, una ráfaga de tokens basura se
    # va en 401 sin pasar nunca por el limitador, que es justo el caso a frenar.
    await rate_limit(request, "cart_session")
    with open_qr_context(body.qr_token) as ctx:
        # Y por mesa después, con el table_id del token ya verificado (nunca del body).
        await rate_limit(request, "cart_session", table_id=ctx.table_id)
        table = ctx.db.execute(
            select(DiningTable).where(
                DiningTable.id == ctx.table_id, DiningTable.active.is_(True)
            )
        ).scalar_one_or_none()
        if table is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Mesa no encontrada o inactiva")
        return service.open_session(ctx.db, ctx.tenant.id, table, body.display_name)


@router.get("", response_model=CartResponse, summary="Ver carrito de la sesión")
def get_cart(ctx: SessionContext = Depends(get_session_context)):
    return service.get_cart(ctx.db, ctx.participant.id, ctx.tenant)


@router.post(
    "/items",
    response_model=CartResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Agregar línea al carrito",
)
async def add_item(
    body: CartItemIn, request: Request,
    ctx: SessionContext = Depends(get_session_context),
):
    await rate_limit(request, "cart_items", table_id=ctx.table_id)
    return service.add_item(ctx.db, ctx.participant.id, body, ctx.tenant)


@router.patch(
    "/items/{item_id}", response_model=CartResponse, summary="Editar línea del carrito"
)
def update_item(
    item_id: UUID, body: CartItemUpdate,
    ctx: SessionContext = Depends(get_session_context),
):
    return service.update_item(ctx.db, ctx.participant.id, item_id, body, ctx.tenant)


@router.delete(
    "/items/{item_id}", response_model=CartResponse, summary="Quitar línea del carrito"
)
def remove_item(item_id: UUID, ctx: SessionContext = Depends(get_session_context)):
    return service.remove_item(ctx.db, ctx.participant.id, item_id, ctx.tenant)


@router.post(
    "/leave",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Salir de la mesa (libera la mesa si era el último y no pidió nada)",
)
def leave(x_session_token: str | None = Header(None, alias="x-session-token")):
    """El comensal se va.

    **Es idempotente y nunca falla con 401.** El comensal ya se está marchando: si
    su token caducó o la mesa se cobró mientras tanto, el resultado deseado —que no
    siga contando como presente— ya se cumplió, y devolverle un error solo le
    impediría cerrar la pantalla.

    Si era el último de la mesa y no dejó ningún pedido que cobrar, la mesa vuelve
    a `libre` en el acto en vez de esperar al barrido.
    """
    if not x_session_token:
        return None
    try:
        with open_session_context(x_session_token) as ctx:
            service.leave_session(ctx.db, ctx.participant, tenant_id=ctx.tenant.id)
    except HTTPException:
        pass  # token inválido/expirado: ya no ocupa la mesa, nada que hacer
    return None


# ---------------------------- Pedidos del comensal ----------------------------

@router.post(
    "/submit",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Enviar el carrito como pedido, con su método de pago (spec 025)",
)
async def submit_cart(
    body: SubmitCartIn, request: Request,
    ctx: SessionContext = Depends(get_session_context),
):
    """El pedido nace junto con su primer intento de pago — no queda pendiente
    de que el staff lo confirme sin pago resuelto; hasta que el staff lo
    confirme no compromete inventario y el comensal puede cancelarlo sin
    coste (spec 025, contracts/submit-cart-with-payment.md)."""
    await rate_limit(request, "cart_submit", table_id=ctx.table_id)
    order = service.submit_cart(
        ctx.db, ctx.participant, body.payment_method_id, body.receipt_file_url,
        tenant_id=ctx.tenant.id, request_id=current_request_id(request),
        tenant_schema=ctx.tenant.schema, tenant=ctx.tenant,
    )
    # Después del COMMIT del servicio, nunca dentro: si la transacción fallara no
    # puede haber salido un evento anunciando un pedido que no existe.
    # spec 089: `line_total` = unit_price × quantity + addons_total (los adicionales una vez).
    _total = sum((i.line_total for i in order.items), start=Decimal(0))
    events.order_created(
        ctx.tenant.id,
        order_id=order.id,
        table_session_id=order.table_session_id,
        dining_table_id=order.dining_table_id,
        table_number=_table_number(ctx.db, order.dining_table_id),
        customer_name=order.customer_name,
        items_count=len(order.items),
        total=_total,
    )
    # Spec 077 (RF-002): además del evento de sincronización de UI de arriba,
    # una notificación persistente y multicanal para el staff.
    notify_order_created(
        ctx.db, ctx.tenant.id,
        order_id=order.id,
        table_session_id=order.table_session_id,
        dining_table_id=order.dining_table_id,
        table_number=_table_number(ctx.db, order.dining_table_id),
        customer_name=order.customer_name,
        items_count=len(order.items),
        total=_total,
    )
    return order


@router.get(
    "/orders",
    response_model=list[OrderResponse],
    summary="Mis pedidos en esta sesión (para restaurar la UI al reingresar)",
)
def my_orders(request: Request, ctx: SessionContext = Depends(get_session_context)):
    """El endpoint más sondeado del sistema, así que responde con `ETag`: el
    comensal recibe un 304 mientras cocina no mueva nada."""
    return json_or_304(
        request, _ORDERS_ADAPTER, service.list_my_orders(ctx.db, ctx.participant.id)
    )


@router.post(
    "/orders/{order_id}/cancel",
    response_model=OrderResponse,
    summary="Cancelar un pedido propio (solo antes de que cocina lo empiece)",
)
def cancel_my_order(
    order_id: UUID,
    body: MyOrderCancelIn,
    request: Request = None,
    ctx: SessionContext = Depends(get_session_context),
):
    order = service.cancel_my_order(
        ctx.db, ctx.participant, order_id, body.motivo, tenant_id=ctx.tenant.id,
        request_id=current_request_id(request),
    )
    events.order_cancelled(
        ctx.tenant.id,
        order_id=order.id,
        table_session_id=order.table_session_id,
        motivo=body.motivo,
    )
    events.bill_changed(ctx.tenant.id, table_session_id=order.table_session_id)
    return order


# ---------------------------- Pagos del comensal (spec 024) ----------------------------

@router.get(
    "/payment-methods",
    response_model=list[DinerPaymentMethod],
    summary="Métodos de pago activos del tenant",
)
def list_payment_methods(ctx: SessionContext = Depends(get_session_context)):
    return service.list_payment_methods(ctx.db)


@router.post(
    "/payment-receipt/presign",
    response_model=ReceiptPresignOut,
    summary="Pedir una URL firmada para subir el comprobante antes de enviar el pedido (spec 025)",
)
def presign_payment_receipt(
    body: PaymentReceiptPresignIn,
    ctx: SessionContext = Depends(get_session_context),
):
    return service.presign_payment_receipt(
        ctx.db, ctx.tenant.schema, ctx.participant.id, body.content_type
    )


@router.post(
    "/orders/{order_id}/payment-attempts",
    response_model=DinerPaymentAttempt,
    status_code=status.HTTP_201_CREATED,
    summary="Iniciar un intento de pago para una orden propia",
)
def create_payment_attempt(
    order_id: UUID, body: PaymentAttemptCreateIn, request: Request = None,
    ctx: SessionContext = Depends(get_session_context),
):
    return service.create_payment_attempt(
        ctx.db, ctx.participant.id, order_id, body.payment_method_id,
        tenant_id=ctx.tenant.id, request_id=current_request_id(request),
    )


@router.post(
    "/payment-attempts/{attempt_id}/receipt/presign",
    response_model=ReceiptPresignOut,
    summary="Pedir una URL firmada para subir el comprobante a R2",
)
def presign_receipt(
    attempt_id: UUID, body: ReceiptPresignIn,
    ctx: SessionContext = Depends(get_session_context),
):
    return service.presign_receipt(
        ctx.db, ctx.tenant.schema, ctx.participant.id, attempt_id, body.content_type
    )


@router.post(
    "/payment-attempts/{attempt_id}/receipt",
    response_model=DinerPaymentAttempt,
    summary="Asociar el comprobante ya subido a R2 con el intento de pago",
)
def attach_receipt(
    attempt_id: UUID, body: ReceiptAttachIn,
    ctx: SessionContext = Depends(get_session_context),
):
    return service.attach_receipt(
        ctx.db, ctx.participant.id, attempt_id, body.file_url, tenant_schema=ctx.tenant.schema
    )
