"""spec 159: índices de shared.users para el listado del Super Admin

Revision ID: 8d09a031cba3
Revises: 948742a050c7
Create Date: 2026-11-16 00:00:05.000000

Soporta los filtros/orden combinables de `GET /super-admin/users` (FR-005,
FR-006) dentro del presupuesto de rendimiento SC-010 (listado de miles de
usuarios en <2s).

Nota: `users` no tiene columna `last_login_at` (no existía antes de esta spec
y no está en el alcance aditivo de T003/data-model.md). Se deriva en el
router como `MAX(user_sessions.created_at)` por usuario (research.md
Decisión 5: cada login crea una fila en `user_sessions`), así que el índice
de apoyo para ordenar/filtrar por "último acceso" es
`(user_sessions.user_id, user_sessions.created_at)`, no una columna en
`users`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = '8d09a031cba3'
down_revision: Union[str, Sequence[str], None] = '948742a050c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_index(schema: str, table: str, index: str) -> bool:
    return op.get_bind().execute(
        text(
            "SELECT 1 FROM pg_indexes WHERE schemaname = :s AND tablename = :t AND indexname = :i"
        ),
        {"s": schema, "t": table, "i": index},
    ).scalar() is not None


def upgrade() -> None:
    if not _has_index("shared", "users", "ix__users__status"):
        op.create_index("ix__users__status", "users", ["status"], unique=False, schema="shared")
    if not _has_index("shared", "users", "ix__users__role_id"):
        op.create_index("ix__users__role_id", "users", ["role_id"], unique=False, schema="shared")
    if not _has_index("shared", "users", "ix__users__created_at"):
        op.create_index("ix__users__created_at", "users", ["created_at"], unique=False, schema="shared")
    if not _has_index("shared", "user_sessions", "ix__user_sessions__user_id_created_at"):
        op.create_index(
            "ix__user_sessions__user_id_created_at", "user_sessions",
            ["user_id", "created_at"], unique=False, schema="shared",
        )


def downgrade() -> None:
    if _has_index("shared", "user_sessions", "ix__user_sessions__user_id_created_at"):
        op.drop_index("ix__user_sessions__user_id_created_at", table_name="user_sessions", schema="shared")
    if _has_index("shared", "users", "ix__users__created_at"):
        op.drop_index("ix__users__created_at", table_name="users", schema="shared")
    if _has_index("shared", "users", "ix__users__role_id"):
        op.drop_index("ix__users__role_id", table_name="users", schema="shared")
    if _has_index("shared", "users", "ix__users__status"):
        op.drop_index("ix__users__status", table_name="users", schema="shared")
