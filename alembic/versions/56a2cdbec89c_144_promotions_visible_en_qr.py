"""visibilidad de promociones en el menú QR: promotions.visible_en_qr

Revision ID: 56a2cdbec89c
Revises: 93da53d89c1a
Create Date: 2026-10-29 00:00:00.000000

"""
from typing import Sequence, Union
from app.scripts.tenant import for_each_tenant_schema
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = '56a2cdbec89c'
down_revision: Union[str, Sequence[str], None] = '93da53d89c1a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    """spec 144 (FR-001/FR-010): toda promoción existente queda visible en QR al
    migrar -- sin backfill de datos, `server_default` ya resuelve el valor
    correcto para las filas existentes."""
    if not _has_table(schema, "promotions"):
        return
    op.add_column(
        "promotions",
        sa.Column("visible_en_qr", sa.Boolean(), nullable=False, server_default=sa.true()),
        schema=schema,
    )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "promotions"):
        return
    op.drop_column("promotions", "visible_en_qr", schema=schema)
