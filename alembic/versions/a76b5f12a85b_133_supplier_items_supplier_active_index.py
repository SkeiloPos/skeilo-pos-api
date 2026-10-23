"""133 supplier_items supplier_id active index

Revision ID: a76b5f12a85b
Revises: a7f3c9d1b246
Create Date: 2026-10-23 00:00:00.000000

spec 133: índice compuesto nuevo sobre `supplier_items(supplier_id, active)` para que el
filtro por proveedor del listado de insumos (`GET /inventory/items?supplier_id=...`) no
dependa de un filtro en memoria sobre `active` tras usar el índice de una sola columna ya
existente (`ix__supplier_items__supplier_id`). Ver data-model.md de la spec para el detalle.
"""
from typing import Sequence, Union
from app.scripts.tenant import for_each_tenant_schema
from alembic import op
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = 'a76b5f12a85b'
down_revision: Union[str, Sequence[str], None] = 'a7f3c9d1b246'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    if not _has_table(schema, "supplier_items"):
        return

    op.create_index(
        "ix__supplier_items__supplier_id_active",
        "supplier_items",
        ["supplier_id", "active"],
        schema=schema,
    )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "supplier_items"):
        return

    op.drop_index(
        "ix__supplier_items__supplier_id_active",
        table_name="supplier_items",
        schema=schema,
    )
