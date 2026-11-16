"""spec 159: tabla shared.admin_audit_logs

Revision ID: 948742a050c7
Revises: 3945b2887ed8
Create Date: 2026-11-16 00:00:04.000000

Bitácora de auditoría administrativa de alcance plataforma (research.md
Decisión 6). Referencias a users/tenants son BLANDAS (sin FK) a propósito --
debe sobrevivir a la eliminación permanente de un usuario (FR-038). Índices
en (created_at), (affected_user_id), (actor_user_id), (tenant_id) soportan
los filtros combinables de auditoría (FR-036) dentro del presupuesto de
SC-010.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = '948742a050c7'
down_revision: Union[str, Sequence[str], None] = '3945b2887ed8'
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
    if not _has_table("shared", "admin_audit_logs"):
        op.create_table(
            "admin_audit_logs",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("actor_type", sa.String(length=20), nullable=False),
            sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("actor_name_snapshot", sa.String(length=255), nullable=True),
            sa.Column("affected_user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("affected_user_email_snapshot", sa.String(length=255), nullable=True),
            sa.Column("tenant_id", sa.Integer(), nullable=True),
            sa.Column("action", sa.String(length=50), nullable=False),
            sa.Column("detail", JSONB(), nullable=True),
            sa.Column("ip_address", sa.String(length=45), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id", name="pk__admin_audit_logs"),
            schema="shared",
        )
        op.create_index(
            "ix__admin_audit_logs__created_at", "admin_audit_logs", ["created_at"],
            unique=False, schema="shared",
        )
        op.create_index(
            "ix__admin_audit_logs__affected_user_id", "admin_audit_logs", ["affected_user_id"],
            unique=False, schema="shared",
        )
        op.create_index(
            "ix__admin_audit_logs__actor_user_id", "admin_audit_logs", ["actor_user_id"],
            unique=False, schema="shared",
        )
        op.create_index(
            "ix__admin_audit_logs__tenant_id", "admin_audit_logs", ["tenant_id"],
            unique=False, schema="shared",
        )


def downgrade() -> None:
    if _has_table("shared", "admin_audit_logs"):
        op.drop_table("admin_audit_logs", schema="shared")
