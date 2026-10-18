"""visibilidad de categorías en el menú QR: categories.visible_en_qr

Revision ID: cfa621f5b865
Revises: fe71d444fb50
Create Date: 2026-10-18 09:06:50.800507

"""
from typing import Sequence, Union
from app.scripts.tenant import for_each_tenant_schema
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = 'cfa621f5b865'
down_revision: Union[str, Sequence[str], None] = 'fe71d444fb50'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    """spec 128 (FR-001): toda categoría existente queda visible en QR al
    migrar -- sin backfill de datos (a diferencia de display_order,
    94144eaa60b5), `server_default` ya resuelve el valor correcto para las
    filas existentes."""
    if not _has_table(schema, "categories"):
        return
    op.add_column(
        "categories",
        sa.Column("visible_en_qr", sa.Boolean(), nullable=False, server_default=sa.true()),
        schema=schema,
    )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "categories"):
        return
    op.drop_column("categories", "visible_en_qr", schema=schema)
