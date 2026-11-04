"""149_plans_add_cocina_access

Revision ID: a6cf1af69346
Revises: 82bab214ee32
Create Date: 2026-11-04 07:42:06.200030

Spec 149 (Decisión 0 acotada a la característica de plan): agrega
`shared.plans.cocina_access` (Boolean NOT NULL DEFAULT false), mismo tipo y
mismo default que `inventario_access`/`promociones_access`. Sin backfill de
datos: el `server_default` resuelve el valor para los planes ya existentes
(research.md Decisión 4).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a6cf1af69346'
down_revision: Union[str, Sequence[str], None] = '82bab214ee32'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "plans",
        sa.Column("cocina_access", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema="shared",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("plans", "cocina_access", schema="shared")
