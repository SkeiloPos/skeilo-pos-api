"""119 report indexes

Revision ID: cd3e314a79b8
Revises: 3858eae5e8aa
Create Date: 2026-10-15 00:00:00.000000

spec 119 US6 (FR-021, research.md D13/D14): dos índices sobre `cash_shifts` para
las búsquedas "turnos por fecha de apertura" del reporte de período, sin y con
filtro de caja:

  * ix__cash_shifts__opened_at            (opened_at)
  * ix__cash_shifts__register_opened_at   (cash_register_id, opened_at)

Solo estos dos: `sales(cash_shift_id)` y el índice parcial de `customer_orders`
(spec 025) ya bastan -- medido -- y cada índice extra sobre `sales` se paga en el
camino caliente del cobro.

NO transaccional por diseño: `CREATE INDEX CONCURRENTLY` no puede ejecutarse
dentro de una transacción, y es lo que evita bloquear cobros y aperturas de turno
mientras se construye. Cada índice se crea dentro de
`op.get_context().autocommit_block()`, esquema por esquema.

Repetible y reparable: `CREATE INDEX ... IF NOT EXISTS` omite EN SILENCIO un índice
que quedó INVÁLIDO por una construcción `CONCURRENTLY` interrumpida (verificado en
PostgreSQL 16), así que antes de crear se consulta `pg_index.indisvalid` y, si el
índice existe inválido, se elimina con `DROP INDEX CONCURRENTLY IF EXISTS` y se
vuelve a crear. Un tenant creado por `create_all` ya nace con ambos índices
(declarados en el modelo, mismos nombres): `IF NOT EXISTS` los reconoce.

`downgrade`: `DROP INDEX CONCURRENTLY IF EXISTS` de ambos. No toca datos.
"""
from typing import Sequence, Union
from app.scripts.tenant import for_each_tenant_schema
from alembic import op
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = 'cd3e314a79b8'
down_revision: Union[str, Sequence[str], None] = '3858eae5e8aa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (nombre del índice, columnas): idénticos a `CashShift.__table_args__`.
_INDEXES = (
    ("ix__cash_shifts__opened_at", "opened_at"),
    ("ix__cash_shifts__register_opened_at", "cash_register_id, opened_at"),
)


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


def _is_invalid(schema: str, name: str) -> bool:
    """El índice existe pero `indisvalid = false` (construcción interrumpida)."""
    return op.get_bind().execute(
        text(
            "SELECT 1 FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relname = :name AND NOT i.indisvalid"
        ),
        {"schema": schema, "name": name},
    ).first() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    # Los esquemas de scratch (tenant_default) pueden no tener las tablas base.
    if not _has_table(schema, "cash_shifts"):
        return

    quote = op.get_bind().dialect.identifier_preparer.quote
    for name, columns in _INDEXES:
        with op.get_context().autocommit_block():
            if _is_invalid(schema, name):
                op.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {quote(schema)}.{quote(name)}"))
            op.execute(text(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {quote(name)} "
                f"ON {quote(schema)}.cash_shifts ({columns})"
            ))


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "cash_shifts"):
        return

    quote = op.get_bind().dialect.identifier_preparer.quote
    for name, _columns in _INDEXES:
        with op.get_context().autocommit_block():
            op.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {quote(schema)}.{quote(name)}"))
