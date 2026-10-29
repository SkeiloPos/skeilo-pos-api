"""product code import jobs

Revision ID: 93da53d89c1a
Revises: 44e61041e0c7
Create Date: 2026-10-29 09:30:00.000000

spec 143: agrega `products.code` (identificador estable para el upsert del export/import
masivo de la carta, research.md D3) y la tabla `product_import_jobs` (seguimiento del
resultado de un import en segundo plano, research.md D7). Todo producto existente recibe un
`code` en esta misma migración (backfill secuencial de 4 dígitos ordenado por `name`) -- no
queda ningún producto sin código después de aplicarla.
"""
from typing import Sequence, Union
from app.scripts.tenant import for_each_tenant_schema
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '93da53d89c1a'
down_revision: Union[str, Sequence[str], None] = '44e61041e0c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    if not _has_table(schema, "products"):
        return

    op.add_column(
        "products",
        sa.Column("code", sa.String(length=20), nullable=True),
        schema=schema,
    )

    # Backfill determinístico: secuencial de 4 dígitos ordenado por nombre
    # (data-model.md).
    op.get_bind().execute(text(f"""
        WITH numbered AS (
            SELECT id, ROW_NUMBER() OVER (ORDER BY name) AS rn
            FROM {schema}.products
        )
        UPDATE {schema}.products AS p
        SET code = LPAD(numbered.rn::text, 4, '0')
        FROM numbered
        WHERE p.id = numbered.id
    """))

    op.alter_column("products", "code", nullable=False, schema=schema)
    op.create_index(
        op.f("ux__products__code"),
        "products", ["code"], unique=True, schema=schema,
    )

    if _has_table(schema, "product_import_jobs"):
        return

    op.create_table(
        "product_import_jobs",
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
            name=op.f("ck__product_import_jobs__status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk__product_import_jobs")),
        schema=schema,
    )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "products"):
        return

    if _has_table(schema, "product_import_jobs"):
        op.drop_table("product_import_jobs", schema=schema)

    op.drop_index(op.f("ux__products__code"), "products", schema=schema)
    op.drop_column("products", "code", schema=schema)
