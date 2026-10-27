"""supplier_items.unit_measure_id (presentación de proveedor ligada a UnitMeasure, spec 140)

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-10-27 00:00:00.000000

Columna aditiva, nullable: la relación proveedor-insumo puede ahora vincularse a una
`UnitMeasure` del mismo grupo que la unidad base del insumo (el admin la elige en vez de
escribir una presentación y un factor a mano); el factor efectivo pasa a ser
`unit_measures.factor_to_base` en vivo (`SupplierItem.effective_conversion_factor`).
`presentation`/`conversion_factor` se conservan intactos para las relaciones ya creadas
(modo legado) -- ninguna fila existente cambia de significado (default NULL).
"""
from typing import Sequence, Union
from app.scripts.tenant import for_each_tenant_schema
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
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
    ).first() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    if not _has_table(schema, "supplier_items"):
        return
    if _has_column(schema, "supplier_items", "unit_measure_id"):
        return
    op.add_column(
        "supplier_items",
        sa.Column("unit_measure_id", sa.UUID(), nullable=True),
        schema=schema,
    )
    op.create_index(
        op.f("ix__supplier_items__unit_measure_id"), "supplier_items", ["unit_measure_id"],
        schema=schema,
    )
    op.create_foreign_key(
        op.f("fk__supplier_items__unit_measure_id__unit_measures"),
        "supplier_items", "unit_measures", ["unit_measure_id"], ["id"],
        source_schema=schema, referent_schema=schema,
    )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "supplier_items"):
        return
    if not _has_column(schema, "supplier_items", "unit_measure_id"):
        return
    op.drop_constraint(
        op.f("fk__supplier_items__unit_measure_id__unit_measures"),
        "supplier_items", schema=schema, type_="foreignkey",
    )
    op.drop_index(op.f("ix__supplier_items__unit_measure_id"), table_name="supplier_items", schema=schema)
    op.drop_column("supplier_items", "unit_measure_id", schema=schema)
