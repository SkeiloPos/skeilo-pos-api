"""spec 159: estado mínimo de shared.tenants

Revision ID: 80acd65ea8a2
Revises: 941d9f90de18
Create Date: 2026-11-16 00:00:01.000000

Agrega `status` (active|suspended) a `shared.tenants`, server_default
'active' -- ningún tenant existente cambia de comportamiento (research.md
Decisión 9). Campo de solo lectura para esta spec, consumido únicamente por
la pestaña "Negocios asociados" de la ficha de usuario.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = '80acd65ea8a2'
down_revision: Union[str, Sequence[str], None] = '941d9f90de18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(schema: str, table: str, column: str) -> bool:
    return op.get_bind().execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name = :t AND column_name = :c"
        ),
        {"s": schema, "t": table, "c": column},
    ).scalar() is not None


def upgrade() -> None:
    if not _has_column("shared", "tenants", "status"):
        op.add_column(
            "tenants",
            sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
            schema="shared",
        )


def downgrade() -> None:
    if _has_column("shared", "tenants", "status"):
        op.drop_column("tenants", "status", schema="shared")
