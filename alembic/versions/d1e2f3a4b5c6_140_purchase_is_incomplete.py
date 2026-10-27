"""purchases.is_incomplete (borrador incompleto, spec 140)

Revision ID: d1e2f3a4b5c6
Revises: c5815ab62693
Create Date: 2026-10-26 00:00:00.000000

"""
from typing import Sequence, Union
from app.scripts.tenant import for_each_tenant_schema
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, Sequence[str], None] = 'c5815ab62693'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    if not _has_table(schema, "purchases"):
        return
    op.add_column("purchases", sa.Column("is_incomplete", sa.Boolean(), nullable=False,
                                          server_default="false"), schema=schema)


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "purchases"):
        return
    op.drop_column("purchases", "is_incomplete", schema=schema)
