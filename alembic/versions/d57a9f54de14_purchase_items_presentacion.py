"""purchase items presentacion

Revision ID: d57a9f54de14
Revises: 99d57acd0216
Create Date: 2026-10-09 15:35:56.995512

spec 107: conecta `supplier_items.conversion_factor` (spec 106) a las compras. Agrega, a cada
línea de compra, la cantidad de presentaciones y una copia del texto de la presentación en el
momento de comprar (snapshot, no un join en vivo con `supplier_items.presentation` -- esa
presentación se puede editar después, spec 106, y una compra ya registrada no puede cambiar de
aspecto retroactivamente, Principio VII). `purchase_items.supplier_item_id` ya existe desde la
migración de spec 106 (99d57acd0216); esta migración no la toca, solo agrega las dos columnas
que faltaban para poblarla con sentido.
"""
from typing import Sequence, Union
from app.scripts.tenant import for_each_tenant_schema
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = 'd57a9f54de14'
down_revision: Union[str, Sequence[str], None] = '99d57acd0216'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    if not _has_table(schema, "purchase_items"):
        return
    op.add_column(
        "purchase_items",
        sa.Column("presentation_quantity", sa.Numeric(12, 3), nullable=True),
        schema=schema,
    )
    op.add_column(
        "purchase_items",
        sa.Column("presentation_label", sa.String(length=100), nullable=True),
        schema=schema,
    )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "purchase_items"):
        return
    op.drop_column("purchase_items", "presentation_label", schema=schema)
    op.drop_column("purchase_items", "presentation_quantity", schema=schema)
