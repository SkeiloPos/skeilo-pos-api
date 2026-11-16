"""spec 159: tabla shared.user_sessions

Revision ID: 3945b2887ed8
Revises: 8d70c585a10e
Create Date: 2026-11-16 00:00:03.000000

Sesiones activas con dispositivo/navegador/IP (research.md Decisión 5). Tabla
nueva, sin backfill posible -- cualquier token emitido antes de esta
migración sin claim `sid` se sigue tratando como válido mientras no cruce
`tokens_valid_after` (compatibilidad retroactiva en `core/dependencies.py`,
fuera de esta migración).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '3945b2887ed8'
down_revision: Union[str, Sequence[str], None] = '8d70c585a10e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = :s AND table_name = :t"
        ),
        {"s": schema, "t": table},
    ).scalar() is not None


def upgrade() -> None:
    if not _has_table("shared", "user_sessions"):
        op.create_table(
            "user_sessions",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("device_label", sa.String(length=255), nullable=True),
            sa.Column("browser", sa.String(length=100), nullable=True),
            sa.Column("ip_address", sa.String(length=45), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ["user_id"], ["shared.users.id"],
                name="fk__user_sessions__user_id__users",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name="pk__user_sessions"),
            schema="shared",
        )
        op.create_index(
            "ix__user_sessions__user_id", "user_sessions", ["user_id"],
            unique=False, schema="shared",
        )


def downgrade() -> None:
    if _has_table("shared", "user_sessions"):
        op.drop_table("user_sessions", schema="shared")
