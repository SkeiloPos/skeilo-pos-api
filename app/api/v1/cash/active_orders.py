"""Pedidos activos del tenant para el bloqueo de cierre de turno (spec 119,
FR-007/FR-008/FR-009, research.md D1).

Alcance **tenant-wide**, no filtrado por `cash_register_id` ni por
`CustomerOrder.cash_shift_id`: el modelo de datos actual no liga un pedido
`TAKEAWAY`/`DELIVERY` a ninguna caja (ese campo solo existe para numeración
de `DINE_IN`, spec 087), así que cualquier pedido activo de cualquier caja
bloquea el cierre de cualquier turno — mismo criterio que ya usa FR-012 para
la agregación de "turno actual" sin filtro de caja.
"""
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.customer_order import CustomerOrder
from app.models.dining_table import DiningTable

# Misma tupla que `cart/service.py:82` y `orders/service.py:69` — repetida
# aquí por el mismo motivo que ya documentan esos dos módulos (evitar un
# import cruzado entre paquetes de API hermanos).
_NON_TERMINAL_ORDER_STATUSES = ("recibida", "abierta", "bloqueada")

# Un único predicado de "activo" para el conteo y para la lista (US6, FR-023):
# así no pueden divergir.
_IS_ACTIVE = CustomerOrder.status.in_(_NON_TERMINAL_ORDER_STATUSES)


@dataclass
class ActiveOrderRow:
    id: UUID
    order_type: str | None
    status: str
    dining_table_id: UUID | None
    table_number: int | None
    customer_name: str | None
    created_at: datetime


def list_active_orders(db: Session) -> list[ActiveOrderRow]:
    """Pedidos `recibida`/`abierta`/`bloqueada` de todo el tenant, más
    antiguo primero — el filtro de "activo" no distingue canal
    (`DINE_IN`/`TAKEAWAY`/`DELIVERY`/`NULL`, data-model.md §1.6)."""
    rows = db.execute(
        select(
            CustomerOrder.id,
            CustomerOrder.order_type,
            CustomerOrder.status,
            CustomerOrder.dining_table_id,
            DiningTable.number,
            CustomerOrder.customer_name,
            CustomerOrder.created_at,
        )
        .select_from(CustomerOrder)
        .outerjoin(DiningTable, DiningTable.id == CustomerOrder.dining_table_id)
        .where(_IS_ACTIVE)
        .order_by(CustomerOrder.created_at.asc())
    ).all()
    return [
        ActiveOrderRow(
            id=id_, order_type=order_type, status=status,
            dining_table_id=dining_table_id, table_number=table_number,
            customer_name=customer_name, created_at=created_at,
        )
        for id_, order_type, status, dining_table_id, table_number, customer_name, created_at in rows
    ]


def count_active_orders(db: Session) -> int:
    """Cuántos pedidos activos hay en todo el tenant, con un `COUNT` en la base
    de datos (US6, FR-023, research.md D12) en vez de traer todas las filas para
    llamar `len()`. Mismo predicado que `list_active_orders`."""
    return db.execute(select(func.count()).select_from(CustomerOrder).where(_IS_ACTIVE)).scalar_one()
