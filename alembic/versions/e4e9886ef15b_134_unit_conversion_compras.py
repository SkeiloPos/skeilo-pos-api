"""134_unit_conversion_compras

Revision ID: e4e9886ef15b
Revises: a76b5f12a85b
Create Date: 2026-10-24 16:24:36.718161

spec 134: activa `app/core/units.py::convert()` (RN-CAT-41, código muerto hoy) añadiendo
`dimension`/`factor_to_base` a `unit_measures`; generaliza la conversión de línea de compra a
cualquier unidad (no solo presentaciones de proveedor, spec 106/107) añadiendo columnas nuevas
a `purchase_items`; ensancha precisión a `Numeric(18,6)` donde la cantidad/costo en unidad base
lo necesita; añade anulación de compras (`purchases.voided_at/voided_by`), historial de cambios
de factor (`unit_measure_factor_changes`) y la bandera de tenant `unit_conversion_enabled`. Ver
data-model.md/research.md de la spec para el detalle de cada decisión.
"""
from typing import Sequence, Union
from app.scripts.tenant import for_each_tenant_schema
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID as PGUUID


# revision identifiers, used by Alembic.
revision: str = 'e4e9886ef15b'
down_revision: Union[str, Sequence[str], None] = 'a76b5f12a85b'
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
            "WHERE table_schema = :s AND table_name = :t AND column_name = :c"
        ),
        {"s": schema, "t": table, "c": column},
    ).scalar() is not None


@for_each_tenant_schema
def _upgrade_tenant_schema(schema: str) -> None:
    # ---------------------------------------------------- unit_measures
    if _has_table(schema, "unit_measures"):
        if not _has_column(schema, "unit_measures", "dimension"):
            op.add_column(
                "unit_measures",
                sa.Column("dimension", sa.String(length=100), nullable=True),
                schema=schema,
            )
            op.execute(f"UPDATE {schema}.unit_measures SET dimension = 'general' WHERE dimension IS NULL")
            op.alter_column("unit_measures", "dimension", nullable=False, schema=schema)

        if not _has_column(schema, "unit_measures", "factor_to_base"):
            op.add_column(
                "unit_measures",
                sa.Column(
                    "factor_to_base", sa.Numeric(18, 6), nullable=False, server_default="1",
                ),
                schema=schema,
            )

    # ---------------------------------------------- unit_measure_factor_changes
    if _has_table(schema, "unit_measures") and not _has_table(schema, "unit_measure_factor_changes"):
        op.create_table(
            "unit_measure_factor_changes",
            sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
            sa.Column("unit_measure_id", PGUUID(as_uuid=True), nullable=False),
            sa.Column("old_factor", sa.Numeric(18, 6), nullable=False),
            sa.Column("new_factor", sa.Numeric(18, 6), nullable=False),
            sa.Column("changed_by", PGUUID(as_uuid=True), nullable=False),
            sa.Column("changed_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(
                ["unit_measure_id"], [f"{schema}.unit_measures.id"],
                name="fk__unit_measure_factor_changes__unit_measure_id__unit_measures",
            ),
            sa.PrimaryKeyConstraint("id", name="pk__unit_measure_factor_changes"),
            schema=schema,
        )
        op.create_index(
            "ix__unit_measure_factor_changes__unit_measure_id",
            "unit_measure_factor_changes",
            ["unit_measure_id"],
            schema=schema,
        )

    # ---------------------------------------------------- inventory_items
    if _has_table(schema, "inventory_items"):
        op.alter_column(
            "inventory_items", "current_stock",
            type_=sa.Numeric(18, 6), existing_nullable=False, schema=schema,
        )
        op.alter_column(
            "inventory_items", "unit_cost",
            type_=sa.Numeric(18, 6), existing_nullable=False, schema=schema,
        )

    # ---------------------------------------------------- inventory_movements
    if _has_table(schema, "inventory_movements"):
        op.alter_column(
            "inventory_movements", "quantity",
            type_=sa.Numeric(18, 6), existing_nullable=False, schema=schema,
        )

    # ---------------------------------------------------- purchases
    if _has_table(schema, "purchases"):
        if not _has_column(schema, "purchases", "voided_at"):
            op.add_column(
                "purchases", sa.Column("voided_at", sa.DateTime(), nullable=True), schema=schema,
            )
        if not _has_column(schema, "purchases", "voided_by"):
            op.add_column(
                "purchases",
                sa.Column("voided_by", PGUUID(as_uuid=True), nullable=True),
                schema=schema,
            )

    # ---------------------------------------------------- purchase_items
    if _has_table(schema, "purchase_items"):
        if not _has_column(schema, "purchase_items", "unit_measure_id"):
            op.add_column(
                "purchase_items",
                sa.Column("unit_measure_id", PGUUID(as_uuid=True), nullable=True),
                schema=schema,
            )
        if not _has_column(schema, "purchase_items", "conversion_factor_applied"):
            op.add_column(
                "purchase_items",
                sa.Column(
                    "conversion_factor_applied", sa.Numeric(18, 6), nullable=False, server_default="1",
                ),
                schema=schema,
            )
        if not _has_column(schema, "purchase_items", "quantity_base"):
            op.add_column(
                "purchase_items",
                sa.Column("quantity_base", sa.Numeric(18, 6), nullable=True),
                schema=schema,
            )
        if not _has_column(schema, "purchase_items", "cost_base"):
            op.add_column(
                "purchase_items",
                sa.Column("cost_base", sa.Numeric(18, 6), nullable=True),
                schema=schema,
            )

        # Backfill (FR-021, data-model.md): toda fila anterior a esta spec toma la unidad
        # base del insumo que ya tenía, factor 1, y quantity_base/cost_base iguales a lo ya
        # capturado -- ningún stock existente se recalcula ni se toca.
        op.execute(f"""
            UPDATE {schema}.purchase_items pi
            SET unit_measure_id = ii.unit_measure_id,
                quantity_base = pi.quantity,
                cost_base = pi.unit_cost
            FROM {schema}.inventory_items ii
            WHERE pi.inventory_item_id = ii.id
              AND pi.quantity_base IS NULL
        """)

        op.alter_column("purchase_items", "unit_measure_id", nullable=False, schema=schema)
        op.alter_column("purchase_items", "quantity_base", nullable=False, schema=schema)
        op.alter_column("purchase_items", "cost_base", nullable=False, schema=schema)

        op.create_foreign_key(
            "fk__purchase_items__unit_measure_id__unit_measures",
            "purchase_items", "unit_measures",
            ["unit_measure_id"], ["id"],
            source_schema=schema, referent_schema=schema,
        )


@for_each_tenant_schema
def _downgrade_tenant_schema(schema: str) -> None:
    if _has_table(schema, "purchase_items"):
        if _has_column(schema, "purchase_items", "unit_measure_id"):
            op.drop_constraint(
                "fk__purchase_items__unit_measure_id__unit_measures",
                "purchase_items", schema=schema, type_="foreignkey",
            )
            op.drop_column("purchase_items", "unit_measure_id", schema=schema)
        if _has_column(schema, "purchase_items", "conversion_factor_applied"):
            op.drop_column("purchase_items", "conversion_factor_applied", schema=schema)
        if _has_column(schema, "purchase_items", "quantity_base"):
            op.drop_column("purchase_items", "quantity_base", schema=schema)
        if _has_column(schema, "purchase_items", "cost_base"):
            op.drop_column("purchase_items", "cost_base", schema=schema)

    if _has_table(schema, "purchases"):
        if _has_column(schema, "purchases", "voided_at"):
            op.drop_column("purchases", "voided_at", schema=schema)
        if _has_column(schema, "purchases", "voided_by"):
            op.drop_column("purchases", "voided_by", schema=schema)

    if _has_table(schema, "inventory_movements"):
        op.alter_column(
            "inventory_movements", "quantity",
            type_=sa.Numeric(12, 3), existing_nullable=False, schema=schema,
        )

    if _has_table(schema, "inventory_items"):
        op.alter_column(
            "inventory_items", "current_stock",
            type_=sa.Numeric(12, 3), existing_nullable=False, schema=schema,
        )
        op.alter_column(
            "inventory_items", "unit_cost",
            type_=sa.Numeric(12, 2), existing_nullable=False, schema=schema,
        )

    if _has_table(schema, "unit_measure_factor_changes"):
        op.drop_index("ix__unit_measure_factor_changes__unit_measure_id", table_name="unit_measure_factor_changes", schema=schema)
        op.drop_table("unit_measure_factor_changes", schema=schema)

    if _has_table(schema, "unit_measures"):
        if _has_column(schema, "unit_measures", "factor_to_base"):
            op.drop_column("unit_measures", "factor_to_base", schema=schema)
        if _has_column(schema, "unit_measures", "dimension"):
            op.drop_column("unit_measures", "dimension", schema=schema)


def _has_shared_column(table: str, column: str) -> bool:
    return _has_column("shared", table, column)


def upgrade() -> None:
    """Upgrade schema."""
    if _has_table("shared", "tenants") and not _has_shared_column("tenants", "unit_conversion_enabled"):
        op.add_column(
            "tenants",
            sa.Column(
                "unit_conversion_enabled", sa.Boolean(), nullable=False, server_default="true",
            ),
            schema="shared",
        )
    _upgrade_tenant_schema()


def downgrade() -> None:
    """Downgrade schema."""
    if _has_table("shared", "tenants") and _has_shared_column("tenants", "unit_conversion_enabled"):
        op.drop_column("tenants", "unit_conversion_enabled", schema="shared")
    _downgrade_tenant_schema()
