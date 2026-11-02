"""Add 'confirmado' order status (a pedido del usuario)

Reintroduce un paso explícito de recepción entre `pendiente` y `entregada`:
el cajero "recibe" el pedido QR apenas llega (`POST /orders/{id}/receive`,
idempotente, sin tocar cocina ni pago) antes de "Entregar". No cambia ningún
dato existente -- toda fila sigue en el valor que ya tenía (`'pendiente'`
sigue siendo válido, nada migra a `'confirmado'` retroactivamente).

upgrade(schema), por cada esquema (`tenant_default` + cada tenant de
`shared.tenants`):
  1. drop + recreate `ck_customer_order_status` agregando `'confirmado'`.
  2. drop + recreate `idx_active_order_per_participant`: `'confirmado'`
     cuenta como activo igual que `'pendiente'` (ninguno es pago ni entrega
     todavía).

Revision ID: c23ca53d6387
Revises: 6bf8898f6e63
Create Date: 2026-11-02 18:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

from app.scripts.tenant import for_each_tenant_schema

revision: str = 'c23ca53d6387'
down_revision: Union[str, Sequence[str], None] = '6bf8898f6e63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    if not _has_table(schema, "customer_orders"):
        return

    op.drop_constraint(
        "ck_customer_order_status", "customer_orders", type_="check", schema=schema,
    )
    op.drop_index(
        "idx_active_order_per_participant", table_name="customer_orders", schema=schema,
    )

    op.create_check_constraint(
        "ck_customer_order_status", "customer_orders",
        "status IN ('pendiente', 'confirmado', 'entregada', 'cancelada')",
        schema=schema,
    )
    op.create_index(
        "idx_active_order_per_participant", "customer_orders", ["participant_id"],
        unique=True,
        postgresql_where=text(
            "status IN ('pendiente', 'confirmado') "
            "OR (status = 'entregada' AND payment_status = 'UNPAID')"
        ),
        schema=schema,
    )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "customer_orders"):
        return

    op.drop_index(
        "idx_active_order_per_participant", table_name="customer_orders", schema=schema,
    )
    op.drop_constraint(
        "ck_customer_order_status", "customer_orders", type_="check", schema=schema,
    )

    # Downgrade de emergencia: cualquier fila que haya quedado en
    # 'confirmado' se repliega a 'pendiente' (el estado anterior más cercano
    # en el ciclo) para no violar el CHECK que se recrea después.
    op.execute(text(
        f'UPDATE "{schema}".customer_orders SET status = \'pendiente\' '
        f"WHERE status = 'confirmado'"
    ))

    op.create_check_constraint(
        "ck_customer_order_status", "customer_orders",
        "status IN ('pendiente', 'entregada', 'cancelada')",
        schema=schema,
    )
    op.create_index(
        "idx_active_order_per_participant", "customer_orders", ["participant_id"],
        unique=True,
        postgresql_where=text(
            "status = 'pendiente' OR (status = 'entregada' AND payment_status = 'UNPAID')"
        ),
        schema=schema,
    )
