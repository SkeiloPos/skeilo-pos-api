"""spec 158: tabla de idempotencia para la generación de compra sugerida

Revision ID: 3b20dc833093
Revises: cf37cdd30f3b
Create Date: 2026-11-15 00:00:00.000000

Tabla nueva `purchase_generation_requests` (esquema `tenant`): registra cada
`idempotency_key` ya procesada por `POST /inventory/purchases/suggested/generate`
junto con las `Purchase.id` creadas, para que un reintento con la misma clave
devuelva el mismo resultado sin duplicar órdenes (FR-010/SC-005). Sin backfill:
una tabla vacía al desplegar es el estado correcto (data-model.md de spec 158).
No tiene FK entrante ni saliente -- purgarla nunca invalida una `Purchase`.
"""
from typing import Sequence, Union

from app.scripts.tenant import for_each_tenant_schema
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = '3b20dc833093'
down_revision: Union[str, Sequence[str], None] = 'cf37cdd30f3b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    if _has_table(schema, "purchase_generation_requests"):
        return

    op.create_table(
        "purchase_generation_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("purchase_ids", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk__purchase_generation_requests")),
        schema=schema,
    )
    op.create_index(
        "ux__purchase_generation_requests__idempotency_key",
        "purchase_generation_requests",
        ["idempotency_key"],
        unique=True,
        schema=schema,
    )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "purchase_generation_requests"):
        return

    op.drop_index(
        "ux__purchase_generation_requests__idempotency_key",
        table_name="purchase_generation_requests",
        schema=schema,
    )
    op.drop_table("purchase_generation_requests", schema=schema)
