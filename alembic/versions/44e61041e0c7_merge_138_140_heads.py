"""merge heads: 138 tenant timezone trigger + 140 supplier item unit measure

Revision ID: 44e61041e0c7
Revises: af8fd44b3c4e, d4e5f6a7b8c9
Create Date: 2026-10-27 00:00:00.000000

PR #138 y #140 ramificaron ambas desde c5815ab62693, dejando dos heads
en alembic (`af8fd44b3c4e (138_tenant_timezone_trigger)` y
`d4e5f6a7b8c9 (140_supplier_item_unit_measure)`). Esta migración de
merge no hace nada en la base de datos: solo unifica el historial en
un único head para que `alembic upgrade head` vuelva a funcionar en
local y producción.
"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = '44e61041e0c7'
down_revision: Union[str, Sequence[str], None] = ('af8fd44b3c4e', 'd4e5f6a7b8c9')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
