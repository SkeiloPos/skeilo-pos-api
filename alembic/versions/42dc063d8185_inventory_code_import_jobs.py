"""inventory code import jobs

Revision ID: 42dc063d8185
Revises: d57a9f54de14
Create Date: 2026-10-10 10:47:17.909615

spec 109: agrega `inventory_items.code` (identificador estable para el upsert de la
importación masiva, research.md D3) y la tabla `inventory_import_jobs` (seguimiento del
resultado de un import en segundo plano, research.md D2). Todo insumo existente recibe un
`code` en esta misma migración (backfill secuencial de 4 dígitos ordenado por `name`, A-123) --
no queda ningún insumo sin código después de aplicarla.
"""
from typing import Sequence, Union
from app.scripts.tenant import for_each_tenant_schema
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '42dc063d8185'
down_revision: Union[str, Sequence[str], None] = 'd57a9f54de14'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    if not _has_table(schema, "inventory_items"):
        return

    op.add_column(
        "inventory_items",
        sa.Column("code", sa.String(length=20), nullable=True),
        schema=schema,
    )

    # Backfill determinístico: secuencial de 4 dígitos ordenado por nombre (research.md D3) --
    # mismo orden que ya usa `service.list_all_items_for_export`.
    op.get_bind().execute(text(f"""
        WITH numbered AS (
            SELECT id, ROW_NUMBER() OVER (ORDER BY name) AS rn
            FROM {schema}.inventory_items
        )
        UPDATE {schema}.inventory_items AS it
        SET code = LPAD(numbered.rn::text, 4, '0')
        FROM numbered
        WHERE it.id = numbered.id
    """))

    op.alter_column("inventory_items", "code", nullable=False, schema=schema)
    op.create_index(
        op.f("ux__inventory_items__code"),
        "inventory_items", ["code"], unique=True, schema=schema,
    )

    if _has_table(schema, "inventory_import_jobs"):
        return

    op.create_table(
        "inventory_import_jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("started_by_user_id", sa.UUID(), nullable=False),
        sa.Column("started_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column("created_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.CheckConstraint(
            "status IN ('procesando', 'finalizado', 'interrumpido')",
            name=op.f("ck__inventory_import_jobs__status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk__inventory_import_jobs")),
        schema=schema,
    )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "inventory_items"):
        return

    if _has_table(schema, "inventory_import_jobs"):
        op.drop_table("inventory_import_jobs", schema=schema)

    op.drop_index(op.f("ux__inventory_items__code"), "inventory_items", schema=schema)
    op.drop_column("inventory_items", "code", schema=schema)
