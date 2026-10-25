"""135 cash_movement payment_method and group_id

Revision ID: c3d9a1f08b42
Revises: a76b5f12a85b
Create Date: 2026-10-25 00:00:00.000000

spec 135 (US2/US3, data-model.md): `tenant.cash_movements` gana dos columnas:

  1. `payment_method_id` (UUID, FK -> `tenant.payment_methods.id`) -- el método de
     esta línea. Se agrega nullable, se rellena para cada fila existente con el
     `PaymentMethod` del mismo tenant cuyo `is_cash=True` (research.md D4: todo
     movimiento histórico era, por la limitación que esta spec resuelve,
     necesariamente efectivo -- Principio VII, el dato se completa, la decisión
     de negocio ya tomada no cambia), y luego se vuelve NOT NULL. Si un tenant no
     tuviera ningún método `is_cash=True`, el `ALTER COLUMN ... SET NOT NULL`
     falla explícitamente en ese tenant (no debería ocurrir -- spec 032 siempre
     siembra uno al aprovisionar).
  2. `group_id` (UUID, nullable, sin backfill) -- enlaza las 1-2 filas de un
     mismo evento de ingreso mixto (FR-011). NULL en toda fila existente, que es
     el valor correcto ("no es parte de un ingreso mixto").

Índice sobre `payment_method_id` (patrón ya usado en `cash_shift_id`), para la
consulta agrupada de `reconcile()`.

`downgrade()` elimina ambas columnas (y el índice) -- ninguna columna
preexistente se modifica ni se elimina, el rollback no pierde información.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

from app.scripts.tenant import for_each_tenant_schema


# revision identifiers, used by Alembic.
revision: str = 'c3d9a1f08b42'
down_revision: Union[str, Sequence[str], None] = 'a76b5f12a85b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FK_PAYMENT_METHOD = "fk__cash_movements__payment_method_id__payment_methods"
_IX_PAYMENT_METHOD = "ix__cash_movements__payment_method_id"


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    if not _has_table(schema, "cash_movements"):
        return

    quote = op.get_bind().dialect.identifier_preparer.quote
    s = quote(schema)

    op.execute(text(
        f"ALTER TABLE {s}.cash_movements ADD COLUMN IF NOT EXISTS payment_method_id UUID"
    ))
    op.execute(text(
        f"UPDATE {s}.cash_movements SET payment_method_id = "
        f"(SELECT id FROM {s}.payment_methods WHERE is_cash = true LIMIT 1) "
        "WHERE payment_method_id IS NULL"
    ))
    op.execute(text(f"ALTER TABLE {s}.cash_movements DROP CONSTRAINT IF EXISTS {_FK_PAYMENT_METHOD}"))
    op.execute(text(
        f"ALTER TABLE {s}.cash_movements ADD CONSTRAINT {_FK_PAYMENT_METHOD} "
        f"FOREIGN KEY (payment_method_id) REFERENCES {s}.payment_methods (id)"
    ))
    op.execute(text(
        f"ALTER TABLE {s}.cash_movements ALTER COLUMN payment_method_id SET NOT NULL"
    ))
    op.execute(text(
        f"ALTER TABLE {s}.cash_movements ADD COLUMN IF NOT EXISTS group_id UUID"
    ))
    op.execute(text(
        f"CREATE INDEX IF NOT EXISTS {_IX_PAYMENT_METHOD} "
        f"ON {s}.cash_movements (payment_method_id)"
    ))


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "cash_movements"):
        return

    quote = op.get_bind().dialect.identifier_preparer.quote
    s = quote(schema)

    op.execute(text(f"DROP INDEX IF EXISTS {s}.{quote(_IX_PAYMENT_METHOD)}"))
    op.execute(text(f"ALTER TABLE {s}.cash_movements DROP COLUMN IF EXISTS group_id"))
    op.execute(text(f"ALTER TABLE {s}.cash_movements DROP CONSTRAINT IF EXISTS {_FK_PAYMENT_METHOD}"))
    op.execute(text(f"ALTER TABLE {s}.cash_movements DROP COLUMN IF EXISTS payment_method_id"))
