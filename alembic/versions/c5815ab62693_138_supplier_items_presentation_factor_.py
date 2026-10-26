"""138_supplier_items_presentation_factor_nullable

Revision ID: c5815ab62693
Revises: bc75fbc0b894
Create Date: 2026-10-26 15:06:54.859408

spec 138 (ajuste pedido por el usuario): al importar un insumo por Excel con un
`Proveedor preferido` que no existe todavía, el sistema ahora crea tanto el `Supplier` como la
relación `SupplierItem` preferida -- pero la plantilla de inventario ya no trae `Presentación`
ni `Factor de conversión` (FR-001/FR-002), así que esa relación se crea sin esos dos datos.
Se vuelven nullable para permitirlo; el administrador los completa después desde el módulo de
proveedores (quedan fuera de alcance de esta spec, research.md/spec.md lo documentan
explícitamente como estado intermedio a refinar en una spec futura).

`ck__supplier_items__conversion_factor` (`conversion_factor > 0`) no se toca: un `NULL` ya
satisface un CHECK en Postgres sin necesidad de ajustarlo.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

from app.scripts.tenant import for_each_tenant_schema


# revision identifiers, used by Alembic.
revision: str = 'c5815ab62693'
down_revision: Union[str, Sequence[str], None] = 'bc75fbc0b894'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    """Upgrade schema."""
    if not _has_table(schema, "supplier_items"):
        return

    op.alter_column(
        "supplier_items", "presentation",
        existing_type=sa.String(length=100), nullable=True, schema=schema,
    )
    op.alter_column(
        "supplier_items", "conversion_factor",
        existing_type=sa.Numeric(12, 3), nullable=True, schema=schema,
    )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    """Downgrade schema."""
    if not _has_table(schema, "supplier_items"):
        return

    # Relaciones creadas por import sin presentación/factor (estado intermedio aceptado por
    # esta spec) no tienen un valor razonable que inventar -- se eliminan antes de restaurar
    # NOT NULL, igual que un downgrade de spec 137 recompone datos derivables; aquí no son
    # derivables, así que se opta por no dejar basura con NOT NULL violado.
    op.get_bind().execute(
        text(
            f"DELETE FROM {schema}.supplier_items "
            "WHERE presentation IS NULL OR conversion_factor IS NULL"
        )
    )
    op.alter_column(
        "supplier_items", "conversion_factor",
        existing_type=sa.Numeric(12, 3), nullable=False, schema=schema,
    )
    op.alter_column(
        "supplier_items", "presentation",
        existing_type=sa.String(length=100), nullable=False, schema=schema,
    )
