"""spec 111: idempotencia en la creación de pedidos (client_request_id)

Revision ID: 30f5e7b0ef5f
Revises: d57a9f54de14
Create Date: 2026-10-11 00:00:00.000000

FR-011/RN4: un reintento de red sobre la misma solicitud de creación de pedido (el cliente no
recibió la respuesta y reintenta) no debe generar un segundo pedido. Agrega `client_request_id`
(nullable, sin backfill -- Principio VII: todo pedido histórico queda en NULL) a
`customer_orders`, con un índice único parcial que permite múltiples filas en NULL sin violar la
unicidad de los valores reales. Aditiva y retrocompatible: sin este campo, `POST /orders` sigue
creando un pedido nuevo por cada solicitud, igual que hoy.
"""
from typing import Sequence, Union
from app.scripts.tenant import for_each_tenant_schema
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = '30f5e7b0ef5f'
down_revision: Union[str, Sequence[str], None] = 'd57a9f54de14'
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
        sa.Column("client_request_id", sa.String(length=64), nullable=True),
        schema=schema,
    )
    op.create_index(
        "uq_customer_orders_client_request_id",
        "customer_orders", ["client_request_id"],
        unique=True, schema=schema,
        postgresql_where=text("client_request_id IS NOT NULL"),
    )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "customer_orders"):
        return
    op.drop_index("uq_customer_orders_client_request_id", "customer_orders", schema=schema)
    op.drop_column("customer_orders", "client_request_id", schema=schema)
