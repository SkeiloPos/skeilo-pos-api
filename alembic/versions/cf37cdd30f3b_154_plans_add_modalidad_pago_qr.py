"""agrega shared.plans.modalidad_pago_qr (spec 154)

Revision ID: cf37cdd30f3b
Revises: 019b97e8a107
Create Date: 2026-11-06 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'cf37cdd30f3b'
down_revision: Union[str, Sequence[str], None] = '019b97e8a107'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Agrega la columna + CheckConstraint que determinan la instrucción de
    pago del menú QR por plan (spec 154, data-model.md). `server_default`
    reproduce el comportamiento actual para todo plan ya existente — sin
    backfill (research.md Decisión 8)."""
    op.add_column(
        "plans",
        sa.Column(
            "modalidad_pago_qr",
            sa.String(30),
            nullable=False,
            server_default="PAY_AFTER_RECEIVING",
        ),
        schema="shared",
    )
    op.create_check_constraint(
        "ck_plans_modalidad_pago_qr",
        "plans",
        "modalidad_pago_qr IN ('PAY_AFTER_RECEIVING', 'PAY_BEFORE_RECEIVING')",
        schema="shared",
    )


def downgrade() -> None:
    """Revierte en el orden inverso: el constraint depende de la columna
    (memory del proyecto sobre el gotcha de nombrado de constraints)."""
    op.drop_constraint(
        "ck_plans_modalidad_pago_qr", "plans", schema="shared", type_="check"
    )
    op.drop_column("plans", "modalidad_pago_qr", schema="shared")
