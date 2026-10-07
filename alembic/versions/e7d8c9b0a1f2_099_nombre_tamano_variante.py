"""099 nombre tamano variante

Revision ID: e7d8c9b0a1f2
Revises: 2d50957395dc
Create Date: 2026-10-06 12:00:00.000000

spec 099-nombre-tamano-variante (data-model.md, research.md D1/D6). 100% aditiva, sin
contraparte destructiva.

Agrega `tenant.product_variants.name` (`VARCHAR(60) NOT NULL DEFAULT ''`): un alias
comercial opcional y puramente cosmético que convive con `presentation_id` sin
reemplazarlo como fuente estructural del tamaño (no reabre la columna `name` que la
spec 084/A-79 eliminó como clave estructural -- ver research.md, "Contexto histórico").
El `DEFAULT ''` del propio `ADD COLUMN` rellena cada fila existente sin un `UPDATE`
aparte: ninguna variante, presentación, precio, orden ni pedido existente se modifica
(Escenario 1).

El `downgrade` hace `DROP COLUMN`, sin paso de datos que revertir (mismo criterio que
`a9f7d0310f6b`/spec 084, "variante presentacion"): presentaciones, precios y pedidos
quedan intactos (Escenario 18).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

from app.scripts.tenant import for_each_tenant_schema

# revision identifiers, used by Alembic.
revision: str = 'e7d8c9b0a1f2'
down_revision: Union[str, Sequence[str], None] = '2d50957395dc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


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
    if not _has_table(schema, "product_variants"):
        return
    if _has_column(schema, "product_variants", "name"):
        return

    op.add_column(
        "product_variants",
        sa.Column("name", sa.String(60), nullable=False, server_default=""),
        schema=schema,
    )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "product_variants"):
        return
    if not _has_column(schema, "product_variants", "name"):
        return

    op.drop_column("product_variants", "name", schema=schema)
