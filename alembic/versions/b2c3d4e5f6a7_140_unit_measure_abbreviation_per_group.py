"""unit_measures.abbreviation: única por grupo, no en todo el negocio (spec 140)

Revision ID: b2c3d4e5f6a7
Revises: e5f6a7b8c9d0
Create Date: 2026-10-27 00:00:00.000000

Corrige la migración anterior (`e5f6a7b8c9d0`): la abreviatura NO debe ser única en todo
el tenant -- dos grupos sin relación entre sí pueden reutilizar la misma abreviatura (ej.
"BOL" como base de un grupo y también dentro de otro). Solo el nombre sigue siendo único
globalmente (sin cambios aquí).

- Elimina cualquier índice único existente sobre la columna `abbreviation` sola (case-exacto,
  el de la migración baseline / el que haya quedado del provisioning de cada tenant) y el
  índice de expresión `ux_unit_measures_abbreviation_ci` (case-insensitive global, agregado
  por `e5f6a7b8c9d0`, demasiado amplio).
- Crea `ux_unit_measures_group_abbreviation_ci`, único sobre `(group_id, upper(abbreviation))`
  -- insensible a mayúsculas, con el alcance correcto (por grupo).
"""
from typing import Sequence, Union
from app.scripts.tenant import for_each_tenant_schema
from alembic import op
from sqlalchemy import text

revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


def _unique_abbreviation_only_indexes(schema: str) -> list[str]:
    """Nombres de todo índice único cuya definición sea exactamente la columna
    `abbreviation` sola (sin `upper(...)`, sin otras columnas) -- regardless de cómo se
    haya llamado en cada tenant (el provisioning no siguió siempre la convención `op.f()`
    de esta migración)."""
    rows = op.get_bind().execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = :schema AND tablename = 'unit_measures' "
            "AND indexdef ILIKE '%UNIQUE%' AND indexdef ILIKE '%(abbreviation)%' "
            "AND indexdef NOT ILIKE '%upper%'"
        ),
        {"schema": schema},
    ).scalars().all()
    return list(rows)


def _has_group_abbreviation_ci_duplicates(schema: str) -> bool:
    row = op.get_bind().execute(
        text(
            f'SELECT 1 FROM "{schema}".unit_measures '
            f'GROUP BY group_id, upper(abbreviation) HAVING count(*) > 1 LIMIT 1'
        )
    ).first()
    return row is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    if not _has_table(schema, "unit_measures"):
        return

    for index_name in _unique_abbreviation_only_indexes(schema):
        op.execute(f'DROP INDEX IF EXISTS "{schema}"."{index_name}"')
    op.execute(f'DROP INDEX IF EXISTS "{schema}".ux_unit_measures_abbreviation_ci')

    # Mismo criterio defensivo que e5f6a7b8c9d0: si algún tenant ya tiene datos que
    # violarían el nuevo índice (dos abreviaturas iguales en el mismo grupo), se omite su
    # índice en vez de romper la migración -- ese tenant necesita limpiar esos datos a mano.
    if not _has_group_abbreviation_ci_duplicates(schema):
        op.execute(
            f'CREATE UNIQUE INDEX IF NOT EXISTS ux_unit_measures_group_abbreviation_ci '
            f'ON "{schema}".unit_measures (group_id, upper(abbreviation))'
        )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "unit_measures"):
        return
    op.execute(f'DROP INDEX IF EXISTS "{schema}".ux_unit_measures_group_abbreviation_ci')
    op.execute(
        f'CREATE UNIQUE INDEX IF NOT EXISTS ux_unit_measures_abbreviation_ci '
        f'ON "{schema}".unit_measures (upper(abbreviation))'
    )
