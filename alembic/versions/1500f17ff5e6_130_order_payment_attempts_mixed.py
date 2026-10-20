"""spec 130: columnas de pago mixto en tenant.order_payment_attempts

data-model.md § OrderPaymentAttempt (spec 130, FR-005 a FR-008): agrega cuatro columnas
nullable para el desglose del pago mixto (efectivo + transferencia) — `cash_amount`,
`transfer_amount`, `transfer_method_id` (FK a `payment_methods`), `cash_confirmed_at`. Todas
`NULL` en cualquier intento no mixto, existente o nuevo — sin backfill (Principio VII).
`cash_amount IS NOT NULL` es el discriminador "este intento es mixto" en el resto del backend.

Rollback: DROP de las cuatro columnas (y su FK), por cada schema de tenant. Sin otra tabla que
dependa de ellas.

Revision ID: 1500f17ff5e6
Revises: 3bbf484089e4
Create Date: 2026-10-20 00:00:00.000001

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy import text

from app.scripts.tenant import for_each_tenant_schema

# revision identifiers, used by Alembic.
revision: str = '1500f17ff5e6'
down_revision: Union[str, Sequence[str], None] = '3bbf484089e4'
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
def upgrade(schema: str) -> None:
    """Upgrade schema."""
    if not _has_table(schema, "order_payment_attempts"):
        return
    if not _has_column(schema, "order_payment_attempts", "cash_amount"):
        op.add_column(
            "order_payment_attempts",
            sa.Column("cash_amount", sa.Numeric(precision=12, scale=2), nullable=True),
            schema=schema,
        )
    if not _has_column(schema, "order_payment_attempts", "transfer_amount"):
        op.add_column(
            "order_payment_attempts",
            sa.Column("transfer_amount", sa.Numeric(precision=12, scale=2), nullable=True),
            schema=schema,
        )
    if not _has_column(schema, "order_payment_attempts", "transfer_method_id"):
        op.add_column(
            "order_payment_attempts",
            sa.Column("transfer_method_id", postgresql.UUID(as_uuid=True), nullable=True),
            schema=schema,
        )
        op.create_foreign_key(
            "fk_order_payment_attempts_transfer_method_id_payment_methods",
            "order_payment_attempts",
            "payment_methods",
            ["transfer_method_id"],
            ["id"],
            source_schema=schema,
            referent_schema=schema,
        )
    if not _has_column(schema, "order_payment_attempts", "cash_confirmed_at"):
        op.add_column(
            "order_payment_attempts",
            sa.Column("cash_confirmed_at", sa.DateTime(), nullable=True),
            schema=schema,
        )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    """Downgrade schema."""
    if not _has_table(schema, "order_payment_attempts"):
        return
    if _has_column(schema, "order_payment_attempts", "cash_confirmed_at"):
        op.drop_column("order_payment_attempts", "cash_confirmed_at", schema=schema)
    if _has_column(schema, "order_payment_attempts", "transfer_method_id"):
        op.drop_constraint(
            "fk_order_payment_attempts_transfer_method_id_payment_methods",
            "order_payment_attempts",
            schema=schema,
            type_="foreignkey",
        )
        op.drop_column("order_payment_attempts", "transfer_method_id", schema=schema)
    if _has_column(schema, "order_payment_attempts", "transfer_amount"):
        op.drop_column("order_payment_attempts", "transfer_amount", schema=schema)
    if _has_column(schema, "order_payment_attempts", "cash_amount"):
        op.drop_column("order_payment_attempts", "cash_amount", schema=schema)
