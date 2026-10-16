"""119 report breakdown rows

Revision ID: 3858eae5e8aa
Revises: 2009eabd2e82
Create Date: 2026-10-15 00:00:00.000000

spec 119 US6 (FR-018/FR-019, research.md D9/D14): el desglose congelado de
cada turno cerrado pasa de dos columnas JSONB de `shift_sales_reports` a FILAS
de dos tablas hijas (`shift_sales_report_products` / `_categories`), para que
la recombinación del reporte de período se resuelva en SQL estándar (también
ejercitable con la suite, que corre sobre SQLite). La cabecera conserva solo
los totales.

Transaccional (una revisión de forma del dato; los índices van aparte en la
Rev. C, que no puede serlo). Cada paso es idempotente frente a un tenant que
ya nació en la forma final por `create_all` (research.md D14 §2):
  1. crea cada tabla hija solo si no existe;
  2. rellena las filas desde el JSONB solo si esas columnas aún existen;
  3. elimina las columnas JSONB solo si aún existen.

`downgrade` NO pierde datos: recrea las columnas JSONB, las reconstruye desde
las filas con el mismo formato que escribía la Rev. A y solo entonces borra
las tablas hijas.
"""
from typing import Sequence, Union
from app.scripts.tenant import for_each_tenant_schema
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '3858eae5e8aa'
down_revision: Union[str, Sequence[str], None] = '2009eabd2e82'
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
    ).first() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    # Los esquemas de scratch (tenant_default) pueden no tener las tablas base.
    if not _has_table(schema, "cash_shifts"):
        return

    if not _has_table(schema, "shift_sales_report_products"):
        op.create_table(
            "shift_sales_report_products",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("cash_shift_id", sa.UUID(), nullable=False),
            sa.Column("product_variant_id", sa.UUID(), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("units", sa.Integer(), nullable=False),
            sa.Column("revenue", sa.Numeric(14, 2), nullable=False),
            sa.CheckConstraint("units >= 0", name="units_nonneg"),
            sa.CheckConstraint("revenue >= 0", name="revenue_nonneg"),
            sa.ForeignKeyConstraint(
                ["cash_shift_id"], [f"{schema}.cash_shifts.id"],
                name=op.f("fk__shift_sales_report_products__cash_shift_id__cash_shifts"),
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk__shift_sales_report_products")),
            sa.UniqueConstraint(
                "cash_shift_id", "product_variant_id",
                name="ux__shift_sales_report_products__shift_variant",
            ),
            schema=schema,
        )

    if not _has_table(schema, "shift_sales_report_categories"):
        op.create_table(
            "shift_sales_report_categories",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("cash_shift_id", sa.UUID(), nullable=False),
            sa.Column("category_id", sa.UUID(), nullable=True),
            sa.Column("category_name", sa.String(255), nullable=False),
            sa.Column("units", sa.Integer(), nullable=False),
            sa.Column("revenue", sa.Numeric(14, 2), nullable=False),
            sa.CheckConstraint("units >= 0", name="units_nonneg"),
            sa.CheckConstraint("revenue >= 0", name="revenue_nonneg"),
            sa.ForeignKeyConstraint(
                ["cash_shift_id"], [f"{schema}.cash_shifts.id"],
                name=op.f("fk__shift_sales_report_categories__cash_shift_id__cash_shifts"),
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk__shift_sales_report_categories")),
            schema=schema,
        )
        op.create_index(
            op.f("ix__shift_sales_report_categories__cash_shift_id"),
            "shift_sales_report_categories", ["cash_shift_id"], schema=schema,
        )

    # Tenant ya en forma final: no hay columnas JSONB que rellenar ni borrar.
    if _has_column(schema, "shift_sales_reports", "products_breakdown"):
        op.execute(text(f"""
            INSERT INTO "{schema}".shift_sales_report_products
                (id, cash_shift_id, product_variant_id, name, units, revenue)
            SELECT gen_random_uuid(), r.cash_shift_id,
                   (e->>'product_variant_id')::uuid, e->>'name',
                   (e->>'units')::integer, (e->>'revenue')::numeric
            FROM "{schema}".shift_sales_reports r
            CROSS JOIN LATERAL jsonb_array_elements(r.products_breakdown) AS e
            ON CONFLICT ON CONSTRAINT ux__shift_sales_report_products__shift_variant DO NOTHING
        """))
        op.drop_column("shift_sales_reports", "products_breakdown", schema=schema)

    if _has_column(schema, "shift_sales_reports", "categories_breakdown"):
        op.execute(text(f"""
            INSERT INTO "{schema}".shift_sales_report_categories
                (id, cash_shift_id, category_id, category_name, units, revenue)
            SELECT gen_random_uuid(), r.cash_shift_id,
                   (e->>'category_id')::uuid, e->>'category_name',
                   (e->>'units')::integer, (e->>'revenue')::numeric
            FROM "{schema}".shift_sales_reports r
            CROSS JOIN LATERAL jsonb_array_elements(r.categories_breakdown) AS e
        """))
        op.drop_column("shift_sales_reports", "categories_breakdown", schema=schema)


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "cash_shifts"):
        return

    # 1) Recrear las columnas JSONB (con un valor temporal para poder marcarlas NOT NULL).
    for column in ("products_breakdown", "categories_breakdown"):
        if not _has_column(schema, "shift_sales_reports", column):
            op.add_column(
                "shift_sales_reports",
                sa.Column(
                    column, postgresql.JSONB(), nullable=False,
                    server_default=sa.text("'[]'::jsonb"),
                ),
                schema=schema,
            )

    # 2) Reconstruirlas desde las filas, en el mismo formato que escribía la
    #    Rev. A (units como número, revenue como texto, category_id nulo como null).
    if _has_table(schema, "shift_sales_report_products"):
        op.execute(text(f"""
            UPDATE "{schema}".shift_sales_reports r
            SET products_breakdown = COALESCE((
                SELECT jsonb_agg(jsonb_build_object(
                    'product_variant_id', p.product_variant_id::text,
                    'name', p.name, 'units', p.units, 'revenue', p.revenue::text
                ) ORDER BY p.revenue DESC)
                FROM "{schema}".shift_sales_report_products p
                WHERE p.cash_shift_id = r.cash_shift_id
            ), '[]'::jsonb)
        """))
    if _has_table(schema, "shift_sales_report_categories"):
        op.execute(text(f"""
            UPDATE "{schema}".shift_sales_reports r
            SET categories_breakdown = COALESCE((
                SELECT jsonb_agg(jsonb_build_object(
                    'category_id', c.category_id::text,
                    'category_name', c.category_name, 'units', c.units,
                    'revenue', c.revenue::text
                ) ORDER BY c.revenue DESC)
                FROM "{schema}".shift_sales_report_categories c
                WHERE c.cash_shift_id = r.cash_shift_id
            ), '[]'::jsonb)
        """))

    # 3) Solo ahora que el JSONB quedó completo se descarta el valor temporal
    #    y se borran las tablas hijas.
    for column in ("products_breakdown", "categories_breakdown"):
        op.alter_column("shift_sales_reports", column, server_default=None, schema=schema)

    op.drop_table("shift_sales_report_categories", schema=schema)
    op.drop_table("shift_sales_report_products", schema=schema)
