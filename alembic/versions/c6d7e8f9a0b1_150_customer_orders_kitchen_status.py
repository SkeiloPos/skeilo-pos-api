"""150_customer_orders_kitchen_status

Spec 150 (data-model.md): agrega `tenant.customer_orders.kitchen_status`
(String, nullable, sin `CheckConstraint` a propósito -- clarificación 4 de
spec.md) y `sent_to_kitchen_at` (DateTime, nullable), más el índice parcial
`idx_customer_orders_kitchen_queue` para la cola de preparación calculada bajo
demanda. Sin backfill: todo pedido existente nace con ambas columnas en NULL
(Principio II/VII) -- ningún pedido histórico aparece "ya en cocina" sin haber
ejecutado la acción nueva.

Revision ID: c6d7e8f9a0b1
Revises: a6cf1af69346
Create Date: 2026-11-04 09:00:00.000000

"""
from typing import Sequence, Union

from app.scripts.tenant import for_each_tenant_schema
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision: str = 'c6d7e8f9a0b1'
down_revision: Union[str, Sequence[str], None] = 'a6cf1af69346'
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
    op.add_column(
        "customer_orders",
        sa.Column("kitchen_status", sa.String(), nullable=True),
        schema=schema,
    )
    op.add_column(
        "customer_orders",
        sa.Column("sent_to_kitchen_at", sa.DateTime(), nullable=True),
        schema=schema,
    )
    op.create_index(
        "idx_customer_orders_kitchen_queue",
        "customer_orders", ["sent_to_kitchen_at"],
        schema=schema,
        postgresql_where=text("kitchen_status IS NOT NULL"),
    )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "customer_orders"):
        return
    op.drop_index("idx_customer_orders_kitchen_queue", "customer_orders", schema=schema)
    op.drop_column("customer_orders", "sent_to_kitchen_at", schema=schema)
    op.drop_column("customer_orders", "kitchen_status", schema=schema)
