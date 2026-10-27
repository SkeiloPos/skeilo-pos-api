"""Operaciones avanzadas de mesas (RF-051..054): estados de mesa, cambio de
mesa de una orden, y unión de mesas por grupo. La división de cuenta por comensal
(RF-054) ya la expone `compute_bill` (líneas por `session_id`) en el bill de mesa."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.exc import IntegrityError

from app.core.crud import get_or_404
from app.core.models import Tenant
from app.models.dining_table import DiningTable
from app.models.customer_order import CustomerOrder
from app.models.order_item import EN_CURSO
from app.api.v1.orders import checkout


def _has_pending_kitchen_work(order: CustomerOrder) -> bool:
    """¿Le queda a esta orden algún ítem sin terminar de preparar?
    (`EN_CURSO`, `models/order_item.py`)."""
    return any(it.estado_cocina in EN_CURSO for it in order.items)


def _table_occupied_by_order(order: CustomerOrder) -> bool:
    """¿Esta orden hace que su mesa cuente como "con trabajo pendiente" para
    liberarla/reservarla, o como destino/origen ocupado al mover otra orden?
    (spec 035, A-52 de `registro-de-anomalias.md`). `'cancelada'` nunca ocupa
    la mesa. Una orden `'pagada'` deja de ocuparla salvo que le queden ítems
    sin terminar de preparar — antes de esta spec, `'pagada'` dejaba de
    ocupar la mesa de inmediato, sin mirar cocina; ahora que
    `checkout_and_send` puede dejar una orden `'pagada'` con comida todavía
    en preparación (spec 028: se cobra antes de enviar a cocina), esa señal
    ya no basta por sí sola. Cualquier otro estado (`'recibida'`, `'abierta'`,
    `'bloqueada'`) sigue ocupando la mesa sin condición, igual que antes."""
    if order.status == "cancelada":
        return False
    if order.status == "pagada":
        return _has_pending_kitchen_work(order)
    return True


def _order_locked_for_move_or_merge(order: CustomerOrder) -> bool:
    """¿Esta orden específica no se puede mover ni fusionar todavía? (spec
    035, A-52). Distinto de `_table_occupied_by_order`: una orden
    `'cancelada'` siempre lo impide (igual que antes de esta spec) aunque
    nunca ocupe la mesa por sí sola; una orden no-terminal
    (`'recibida'`/`'abierta'`/`'bloqueada'`) nunca lo impide (igual que
    antes). Una orden `'pagada'` lo impide solo mientras le queden ítems sin
    terminar de preparar — antes de esta spec, `'pagada'` siempre lo impedía,
    sin mirar cocina."""
    if order.status == "cancelada":
        return True
    if order.status == "pagada":
        return _has_pending_kitchen_work(order)
    return False


def _active_orders_on_table(db: Session, table_id: UUID) -> list[CustomerOrder]:
    orders = db.execute(
        select(CustomerOrder)
        .options(selectinload(CustomerOrder.items))
        .where(CustomerOrder.dining_table_id == table_id)
    ).scalars().all()
    return [o for o in orders if _table_occupied_by_order(o)]


def set_table_status(db: Session, table_id: UUID, new_status: str) -> DiningTable:
    table = get_or_404(db, DiningTable, table_id, "Table not found")
    # 'libre'/'reservada' sólo si la mesa no tiene órdenes activas.
    if new_status in ("libre", "reservada") and _active_orders_on_table(db, table.id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "La mesa tiene órdenes sin cerrar; no se puede marcar como "
            f"'{new_status}'.",
        )
    table.status = new_status
    db.commit()
    db.refresh(table)
    return table


def move_order(db: Session, order_id: UUID, target_table_id: UUID) -> CustomerOrder:
    order = get_or_404(db, CustomerOrder, order_id, "Order not found")
    if _order_locked_for_move_or_merge(order):
        raise HTTPException(status.HTTP_409_CONFLICT, "La orden ya está cerrada")

    target = get_or_404(db, DiningTable, target_table_id, "Target table not found")
    if target.id == order.dining_table_id:
        return order
    if _active_orders_on_table(db, target.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "La mesa destino ya tiene una orden activa")

    source_id = order.dining_table_id
    order.dining_table_id = target.id
    target.status = "ocupada"
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "La mesa destino ya tiene una orden abierta")

    # Liberar la mesa origen si ya no tiene órdenes activas.
    if source_id and not _active_orders_on_table(db, source_id):
        src = db.get(DiningTable, source_id)
        if src is not None:
            src.status = "libre"
    db.commit()
    db.refresh(order)
    return order


def merge_orders(db: Session, order_ids: list[UUID]) -> dict:
    orders = db.execute(
        select(CustomerOrder).where(CustomerOrder.id.in_(order_ids))
    ).scalars().all()
    if len(orders) != len(set(order_ids)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alguna orden no existe")
    if any(_order_locked_for_move_or_merge(o) for o in orders):
        raise HTTPException(status.HTTP_409_CONFLICT, "No se pueden unir órdenes ya cerradas")

    # Reusar un grupo existente entre las órdenes, o crear uno nuevo.
    group_id = next((o.merged_group_id for o in orders if o.merged_group_id), uuid.uuid4())
    for o in orders:
        o.merged_group_id = group_id
    db.commit()
    return {"merged_group_id": group_id, "order_ids": [o.id for o in orders]}


def group_bill(db: Session, group_id: UUID, *, tenant: Tenant | None = None) -> dict:
    """Cuenta consolidada de un grupo de mesas fusionadas — corrige A-01 camino C
    (`registro-de-anomalias.md`, RN-ORD-64): excluye órdenes `pagada`/`cancelada`
    del cálculo y aplica promociones/combos vigentes, igual que
    `table_sessions.compute_bill` (camino A, referencia). Evalúa descuentos por
    orden, no por comensal — equivalente en total a agrupar por comensal
    (research.md Decisión 1 de specs/019-correccion-cuenta-mesas-fusionadas)."""
    orders = db.execute(
        select(CustomerOrder).where(CustomerOrder.merged_group_id == group_id)
    ).scalars().all()
    if not orders:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Grupo de mesas no encontrado")

    now = datetime.now(timezone.utc)
    per_order = []
    total = Decimal("0")
    for o in orders:
        if o.status in checkout.TERMINAL:
            sub = Decimal("0")
        else:
            lines = checkout.order_sale_lines(db, o.id)
            raw = sum((l.line_total for l in lines), start=Decimal("0"))
            # spec 073 (FR-018a, A-70): cada pedido del grupo evalúa la vigencia
            # contra SU PROPIO instante congelado (o la hora del cobro si es
            # anterior a esta spec), NO un MIN del grupo — esos pedidos se
            # cobran individualmente por `checkout_and_send`/`pay_order`, así
            # que el desglose consolidado debe coincidir con el cobro
            # pedido-por-pedido (research.md D3).
            instant = checkout.promotion_evaluation_instant([o], now=now)
            # spec 138 (FR-006): la vigencia se evalúa en la zona del tenant dueño de las mesas.
            promo_discount, _, _ = checkout.auto_discount(db, lines, instant, tenant)
            sub = raw - promo_discount
            total += sub
        per_order.append({
            "order_id": o.id, "dining_table_id": o.dining_table_id,
            "status": o.status, "subtotal": sub,
        })
    return {"merged_group_id": group_id, "total": total, "orders": per_order}
