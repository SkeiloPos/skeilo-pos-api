"""165 follow-up: signed_quantity en inventory_movements

Un movimiento 'adjustment' guarda `quantity` como valor absoluto (CHECK
`quantity > 0`), así que no hay forma de saber si fue un aumento o una
disminución de stock mirando el kardex. Añade `signed_quantity` (positivo =
entrada/aumento, negativo = salida/disminución) y la rellena para 'in'/'out'
(se deriva de `type`, que ya tiene el signo). Para 'adjustment' queda NULL en
filas existentes: no hay forma de reconstruir la dirección histórica a partir
solo de `quantity`.

Revision ID: c3d7e5f9a1b4
Revises: b1c9f4e7a3d2
Create Date: 2026-11-24 00:00:00.000000

"""
from typing import Sequence, Union

from app.scripts.tenant import for_each_tenant_schema
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision: str = 'c3d7e5f9a1b4'
down_revision: Union[str, Sequence[str], None] = 'b1c9f4e7a3d2'
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
    if not _has_table(schema, "inventory_movements"):
        return
    if not _has_column(schema, "inventory_movements", "signed_quantity"):
        op.add_column(
            "inventory_movements",
            sa.Column("signed_quantity", sa.Numeric(18, 6), nullable=True),
            schema=schema,
        )
    op.execute(
        f"UPDATE {schema}.inventory_movements "
        "SET signed_quantity = CASE WHEN type = 'in' THEN quantity "
        "WHEN type = 'out' THEN -quantity ELSE signed_quantity END "
        "WHERE type IN ('in', 'out') AND signed_quantity IS NULL"
    )


@for_each_tenant_schema
def _downgrade_tenant_schema(schema: str) -> None:
    if _has_table(schema, "inventory_movements") and _has_column(
        schema, "inventory_movements", "signed_quantity"
    ):
        op.drop_column("inventory_movements", "signed_quantity", schema=schema)


def upgrade() -> None:
    """Upgrade schema."""
    _upgrade_tenant_schema()


def downgrade() -> None:
    """Downgrade schema."""
    _downgrade_tenant_schema()
