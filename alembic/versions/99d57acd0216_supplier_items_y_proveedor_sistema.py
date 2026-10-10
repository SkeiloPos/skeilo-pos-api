"""supplier_items y proveedor sistema

Revision ID: 99d57acd0216
Revises: e7d8c9b0a1f2
Create Date: 2026-10-09 11:27:33.309365

spec 106: relación N:M proveedor-insumo (`supplier_items`), con presentación y factor de
conversión propios (D2 del enunciado), más el atributo `suppliers.is_system` y el proveedor
semilla "Compra ocasional" (RN-17, FR-019) -- prerrequisito directo para que FR-012/FR-013
tengan algo que excluir. También agrega `purchase_items.supplier_item_id` (NULL, sin poblar
por ningún flujo de esta spec) para que el bloqueo de factor usado-en-compra (FR-014) tenga
algo que inspeccionar; HU-05 es quien la puebla al implementar el upsert automático (RN-15).

Unicidad por (supplier_id, inventory_item_id, lower(trim(presentation))) -- insensible a
mayúsculas y espacios (FR-003) -- y a lo sumo una relación preferida activa por insumo, vía un
índice parcial NO DEFERRABLE (los índices de Postgres no lo admiten); el swap de preferido
depende de bloquear primero la fila del insumo (SELECT ... FOR UPDATE, ver
app/inventory_engine/stock.py::lock_items) antes de desmarcar y marcar, en ese orden
(research.md D2/D4 de la spec).
"""
from typing import Sequence, Union
from app.scripts.tenant import for_each_tenant_schema
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = '99d57acd0216'
down_revision: Union[str, Sequence[str], None] = 'e7d8c9b0a1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    if not _has_table(schema, "suppliers"):
        return

    op.add_column(
        "suppliers",
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        schema=schema,
    )

    # Seed idempotente del proveedor de sistema (FR-019). Reejecutable: no inserta un
    # segundo "Compra ocasional" si el schema ya tiene uno marcado is_system.
    op.get_bind().execute(text(f"""
        INSERT INTO {schema}.suppliers (id, name, is_system, active, created_at)
        SELECT gen_random_uuid(), 'Compra ocasional', true, true, now()
        WHERE NOT EXISTS (
            SELECT 1 FROM {schema}.suppliers WHERE is_system
        )
    """))

    op.create_table(
        "supplier_items",
        sa.Column("supplier_id", sa.UUID(), nullable=False),
        sa.Column("inventory_item_id", sa.UUID(), nullable=False),
        sa.Column("presentation", sa.String(length=100), nullable=False),
        sa.Column("conversion_factor", sa.Numeric(12, 3), nullable=False),
        sa.Column("last_cost", sa.Numeric(12, 2), nullable=True),
        sa.Column("last_cost_at", sa.DateTime(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("preferred", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "conversion_factor > 0", name=op.f("ck__supplier_items__conversion_factor"),
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"], [f"{schema}.suppliers.id"],
            name=op.f("fk__supplier_items__supplier_id__suppliers"),
        ),
        sa.ForeignKeyConstraint(
            ["inventory_item_id"], [f"{schema}.inventory_items.id"],
            name=op.f("fk__supplier_items__inventory_item_id__inventory_items"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk__supplier_items")),
        schema=schema,
    )
    op.create_index(
        op.f("ix__supplier_items__supplier_id"),
        "supplier_items", ["supplier_id"], schema=schema,
    )
    op.create_index(
        op.f("ix__supplier_items__inventory_item_id"),
        "supplier_items", ["inventory_item_id"], schema=schema,
    )
    # Unicidad por (proveedor, insumo, presentación) insensible a mayúsculas/espacios
    # (FR-003) -- índice por expresión, no una UniqueConstraint sobre el texto crudo.
    op.create_index(
        "ux__supplier_items__combo",
        "supplier_items",
        ["supplier_id", "inventory_item_id", text("lower(trim(presentation))")],
        unique=True, schema=schema,
    )
    # A lo sumo una relación preferida activa por insumo (RN-16/FR-008/FR-009). NO
    # DEFERRABLE -- los índices de Postgres no lo admiten.
    op.create_index(
        "ux__supplier_items__preferred",
        "supplier_items", ["inventory_item_id"],
        unique=True, schema=schema,
        postgresql_where=text("preferred AND active"),
    )

    op.add_column(
        "purchase_items",
        sa.Column("supplier_item_id", sa.UUID(), nullable=True),
        schema=schema,
    )
    op.create_foreign_key(
        op.f("fk__purchase_items__supplier_item_id__supplier_items"),
        "purchase_items", "supplier_items",
        ["supplier_item_id"], ["id"],
        source_schema=schema, referent_schema=schema,
    )
    op.create_index(
        op.f("ix__purchase_items__supplier_item_id"),
        "purchase_items", ["supplier_item_id"], schema=schema,
    )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "suppliers"):
        return

    op.drop_index(
        op.f("ix__purchase_items__supplier_item_id"), "purchase_items", schema=schema,
    )
    op.drop_constraint(
        op.f("fk__purchase_items__supplier_item_id__supplier_items"),
        "purchase_items", schema=schema, type_="foreignkey",
    )
    op.drop_column("purchase_items", "supplier_item_id", schema=schema)

    op.drop_index("ux__supplier_items__preferred", "supplier_items", schema=schema)
    op.drop_index("ux__supplier_items__combo", "supplier_items", schema=schema)
    op.drop_index(
        op.f("ix__supplier_items__inventory_item_id"), "supplier_items", schema=schema,
    )
    op.drop_index(
        op.f("ix__supplier_items__supplier_id"), "supplier_items", schema=schema,
    )
    op.drop_table("supplier_items", schema=schema)

    op.get_bind().execute(text(f"DELETE FROM {schema}.suppliers WHERE is_system"))
    op.drop_column("suppliers", "is_system", schema=schema)
