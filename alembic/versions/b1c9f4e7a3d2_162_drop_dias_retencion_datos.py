"""spec 162: elimina tenant.tenant_legal_profiles.dias_retencion_datos —
retirada de la lista cerrada de variables de la plantilla, el negocio pidió
no recolectar este dato del tenant

Revision ID: b1c9f4e7a3d2
Revises: e9ab58cbea91
Create Date: 2026-11-18 00:00:02.000000

"""
from typing import Sequence, Union

from app.scripts.tenant import for_each_tenant_schema
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision: str = 'b1c9f4e7a3d2'
down_revision: Union[str, Sequence[str], None] = 'e9ab58cbea91'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(schema: str, table: str, column: str) -> bool:
    return op.get_bind().execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :table AND column_name = :column"
        ),
        {"schema": schema, "table": table, "column": column},
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    if _has_column(schema, "tenant_legal_profiles", "dias_retencion_datos"):
        op.drop_column("tenant_legal_profiles", "dias_retencion_datos", schema=schema)


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_column(schema, "tenant_legal_profiles", "dias_retencion_datos"):
        op.add_column(
            "tenant_legal_profiles",
            sa.Column("dias_retencion_datos", sa.Integer(), nullable=True),
            schema=schema,
        )
