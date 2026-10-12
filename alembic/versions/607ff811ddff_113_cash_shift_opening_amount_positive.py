"""113 cash shift opening amount positive

Revision ID: 607ff811ddff
Revises: 94d1a25bcc10
Create Date: 2026-10-12 11:57:25.798183

spec 113-validacion-saldo-inicial-caja (FR-001, FR-007, Principio VII/VIII).
Endurece el `CheckConstraint` de `cash_shifts.opening_amount` de `>= 0` a `> 0`.
Se agrega con `NOT VALID` para no escanear ni rechazar ningún turno histórico
ya existente (incluidos los que tengan `opening_amount = 0`) — solo rige para
los `INSERT`/`UPDATE` nuevos a partir de este despliegue.

Nombre real del constraint en la base de datos: `ck__cash_shifts__ck_cash_shift_opening_positive`
(convención `ck__%(table_name)s__%(constraint_name)s` de `app/core/models.py`, no el nombre corto
`ck_cash_shift_opening_positive` pasado a `CheckConstraint(name=...)` — verificado contra los
esquemas reales `heladeria`/`skeilo`/`tenantlocal`, data-model.md/research.md D2 asumían el nombre
corto).

`downgrade`: revierte exactamente al estado anterior (`>= 0`, sin `NOT VALID`,
porque no hay fila que pudiera violar ese chequeo más laxo).
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

from app.scripts.tenant import for_each_tenant_schema

# revision identifiers, used by Alembic.
revision: str = '607ff811ddff'
down_revision: Union[str, Sequence[str], None] = '94d1a25bcc10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    # Los schemas de scratch (tenant_default) pueden no tener las tablas base todavía.
    if not _has_table(schema, "cash_shifts"):
        return

    op.execute(text(
        f'ALTER TABLE "{schema}".cash_shifts '
        f'DROP CONSTRAINT ck__cash_shifts__ck_cash_shift_opening_positive'
    ))
    op.execute(text(
        f'ALTER TABLE "{schema}".cash_shifts '
        f'ADD CONSTRAINT ck__cash_shifts__ck_cash_shift_opening_positive '
        f'CHECK (opening_amount > 0) NOT VALID'
    ))


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "cash_shifts"):
        return

    op.execute(text(
        f'ALTER TABLE "{schema}".cash_shifts '
        f'DROP CONSTRAINT ck__cash_shifts__ck_cash_shift_opening_positive'
    ))
    op.execute(text(
        f'ALTER TABLE "{schema}".cash_shifts '
        f'ADD CONSTRAINT ck__cash_shifts__ck_cash_shift_opening_positive '
        f'CHECK (opening_amount >= 0)'
    ))
