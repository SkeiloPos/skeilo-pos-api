"""spec 159: tabla shared.tenant_memberships

Revision ID: 8d70c585a10e
Revises: 80acd65ea8a2
Create Date: 2026-11-16 00:00:02.000000

Membresía multi-negocio por usuario (research.md Decisión 1). `users.tenant_id`
no cambia -- sigue siendo el "negocio principal" de login/invitación. Backfill:
una fila de membresía por cada `User` existente con `tenant_id IS NOT NULL`,
usando su `tenant_id`/`role_id` actuales. Rollback seguro: ninguna otra tabla
depende de esta (data-model.md).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '8d70c585a10e'
down_revision: Union[str, Sequence[str], None] = '80acd65ea8a2'
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
    if not _has_table("shared", "tenant_memberships"):
        op.create_table(
            "tenant_memberships",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", sa.Integer(), nullable=False),
            sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(
                ["user_id"], ["shared.users.id"],
                name="fk__tenant_memberships__user_id__users",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id"], ["shared.tenants.id"],
                name="fk__tenant_memberships__tenant_id__tenants",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["role_id"], ["shared.roles.id"],
                name="fk__tenant_memberships__role_id__roles",
            ),
            sa.PrimaryKeyConstraint("id", name="pk__tenant_memberships"),
            sa.UniqueConstraint(
                "user_id", "tenant_id", name="uq__tenant_memberships__user_id_tenant_id"
            ),
            schema="shared",
        )
        op.create_index(
            "ix__tenant_memberships__user_id", "tenant_memberships", ["user_id"],
            unique=False, schema="shared",
        )
        op.create_index(
            "ix__tenant_memberships__tenant_id", "tenant_memberships", ["tenant_id"],
            unique=False, schema="shared",
        )

        op.execute(
            text(
                "INSERT INTO shared.tenant_memberships (id, user_id, tenant_id, role_id, created_at) "
                "SELECT gen_random_uuid(), id, tenant_id, role_id, created_at "
                "FROM shared.users WHERE tenant_id IS NOT NULL"
            )
        )


def downgrade() -> None:
    if _has_table("shared", "tenant_memberships"):
        op.drop_table("tenant_memberships", schema="shared")
