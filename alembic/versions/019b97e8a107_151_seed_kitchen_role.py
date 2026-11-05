"""siembra el rol KITCHEN en shared.roles (spec 151)

Revision ID: 019b97e8a107
Revises: c6d7e8f9a0b1
Create Date: 2026-11-04 00:00:00.000000

"""
import uuid
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = '019b97e8a107'
down_revision: Union[str, Sequence[str], None] = 'c6d7e8f9a0b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ROLE_NAME = "KITCHEN"


def upgrade() -> None:
    """Agrega el rol KITCHEN al catálogo `shared.roles` (spec 151, data-model.md
    §Estrategia de migración). `_seed_shared_data()` (app/core/db.py) solo
    siembra roles en la inicialización de una base de datos nueva — en
    cualquier ambiente que ya pasó por esa inicialización, agregar el valor
    solo a `ROLE_NAMES` no tiene ningún efecto, así que hace falta esta
    migración de datos. Idempotente por `name`, mismo molde que
    `8421e9a2b45a_seed_mesero_role.py`."""
    bind = op.get_bind()
    exists = bind.execute(
        text("SELECT 1 FROM shared.roles WHERE name = :name"),
        {"name": _ROLE_NAME},
    ).scalar()
    if exists:
        return
    bind.execute(
        text(
            "INSERT INTO shared.roles (id, name, active, created_at) "
            "VALUES (:id, :name, true, now())"
        ),
        {"id": uuid.uuid4(), "name": _ROLE_NAME},
    )


def downgrade() -> None:
    """Elimina la fila KITCHEN únicamente si ningún usuario o invitación
    pendiente la referencia todavía (data-model.md §Estrategia de migración),
    mismo molde que `8421e9a2b45a_seed_mesero_role.py`."""
    bind = op.get_bind()
    in_use = bind.execute(
        text(
            "SELECT 1 FROM shared.roles r "
            "WHERE r.name = :name AND ("
            "  EXISTS (SELECT 1 FROM shared.users u WHERE u.role_id = r.id)"
            "  OR EXISTS (SELECT 1 FROM shared.user_invitations i WHERE i.role_id = r.id)"
            ")"
        ),
        {"name": _ROLE_NAME},
    ).scalar()
    if in_use:
        return
    bind.execute(
        text("DELETE FROM shared.roles WHERE name = :name"),
        {"name": _ROLE_NAME},
    )
