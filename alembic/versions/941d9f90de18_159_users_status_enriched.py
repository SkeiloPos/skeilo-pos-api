"""spec 159: estado enriquecido de shared.users

Revision ID: 941d9f90de18
Revises: 3b20dc833093
Create Date: 2026-11-16 00:00:00.000000

Agrega `status`/`activated_at`/`suspended_at`/`suspended_reason`/
`security_blocked_at`/`failed_login_attempts`/`anonymized_at` a `shared.users`
(data-model.md). `active: bool` no cambia de tipo ni de significado para
`core/dependencies.py` -- `status` es aditivo, consumido solo por el módulo
Super Admin. Backfill: active=True -> status='active', active=False ->
status='suspended'; activated_at=created_at para todo usuario existente
(research.md Decisión 2).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = '941d9f90de18'
down_revision: Union[str, Sequence[str], None] = '3b20dc833093'
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
    """shared.users no es per-tenant: esta migración no usa for_each_tenant_schema."""
    if not _has_column("shared", "users", "status"):
        op.add_column(
            "users",
            sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
            schema="shared",
        )
    if not _has_column("shared", "users", "activated_at"):
        op.add_column(
            "users", sa.Column("activated_at", sa.DateTime(), nullable=True), schema="shared"
        )
    if not _has_column("shared", "users", "suspended_at"):
        op.add_column(
            "users", sa.Column("suspended_at", sa.DateTime(), nullable=True), schema="shared"
        )
    if not _has_column("shared", "users", "suspended_reason"):
        op.add_column(
            "users", sa.Column("suspended_reason", sa.String(length=500), nullable=True), schema="shared"
        )
    if not _has_column("shared", "users", "security_blocked_at"):
        op.add_column(
            "users", sa.Column("security_blocked_at", sa.DateTime(), nullable=True), schema="shared"
        )
    if not _has_column("shared", "users", "failed_login_attempts"):
        op.add_column(
            "users",
            sa.Column("failed_login_attempts", sa.Integer(), nullable=False, server_default="0"),
            schema="shared",
        )
    if not _has_column("shared", "users", "anonymized_at"):
        op.add_column(
            "users", sa.Column("anonymized_at", sa.DateTime(), nullable=True), schema="shared"
        )

    op.execute(
        text(
            "UPDATE shared.users SET status = CASE WHEN active THEN 'active' ELSE 'suspended' END, "
            "activated_at = COALESCE(activated_at, created_at)"
        )
    )


def downgrade() -> None:
    for column in (
        "anonymized_at",
        "failed_login_attempts",
        "security_blocked_at",
        "suspended_reason",
        "suspended_at",
        "activated_at",
        "status",
    ):
        if _has_column("shared", "users", column):
            op.drop_column("users", column, schema="shared")
