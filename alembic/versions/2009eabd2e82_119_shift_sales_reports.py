"""119 shift sales reports

Revision ID: 2009eabd2e82
Revises: 607ff811ddff
Create Date: 2026-10-15 00:00:00.000000

spec 119: tabla nueva `shift_sales_reports`, relación 1:1 con `cash_shifts`
(FR-013/research.md D2) — snapshot inmutable del reporte operativo de un
turno (total de ventas, órdenes, ticket promedio, desglose COMPLETO de
productos/categorías). Migración puramente aditiva: ninguna columna
existente de `cash_shifts`/`sales`/`sale_items`/`customer_orders` cambia.
Sin backfill (data-model.md §4, research.md D8): ningún turno ya cerrado
antes de este despliegue recibe una fila retroactiva — su reporte cae a
cálculo en vivo, no a un snapshot, hasta su próximo cierre (que en este caso
nunca vuelve a ocurrir porque ya está cerrado; queda así de forma permanente,
documentado como ventana de transición, no como error).
"""
from typing import Sequence, Union
from app.scripts.tenant import for_each_tenant_schema
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '2009eabd2e82'
down_revision: Union[str, Sequence[str], None] = '607ff811ddff'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    # Los esquemas de scratch (tenant_default) pueden no tener las tablas base.
    if not _has_table(schema, "cash_shifts"):
        return

    op.create_table(
        "shift_sales_reports",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("cash_shift_id", sa.UUID(), nullable=False),
        sa.Column("total_sales", sa.Numeric(12, 2), nullable=False),
        sa.Column("order_count", sa.Integer(), nullable=False),
        sa.Column("avg_ticket", sa.Numeric(12, 2), nullable=False),
        sa.Column("products_breakdown", postgresql.JSONB(), nullable=False),
        sa.Column("categories_breakdown", postgresql.JSONB(), nullable=False),
        sa.Column("computed_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "total_sales >= 0", name="ck_shift_sales_report_total_sales",
        ),
        sa.CheckConstraint(
            "order_count >= 0", name="ck_shift_sales_report_order_count",
        ),
        sa.CheckConstraint(
            "avg_ticket >= 0", name="ck_shift_sales_report_avg_ticket",
        ),
        sa.ForeignKeyConstraint(
            ["cash_shift_id"], [f"{schema}.cash_shifts.id"],
            name=op.f("fk__shift_sales_reports__cash_shift_id__cash_shifts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk__shift_sales_reports")),
        sa.UniqueConstraint(
            "cash_shift_id", name="ux__shift_sales_reports__cash_shift_id",
        ),
        schema=schema,
    )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "cash_shifts"):
        return

    op.drop_table("shift_sales_reports", schema=schema)
