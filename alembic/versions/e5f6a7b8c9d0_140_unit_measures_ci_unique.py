"""unit_measures: índice único insensible a mayúsculas en name/abbreviation (spec 140)

Revision ID: e5f6a7b8c9d0
Revises: d1e2f3a4b5c6
Create Date: 2026-10-27 00:00:00.000000

El modelo ya tiene `name`/`abbreviation` como `unique=True` (comparación exacta); ese
índice se deja intacto. Esto agrega, de forma aditiva, dos índices únicos de expresión
sobre `upper(name)`/`upper(abbreviation)` para que "BLS" y "bls" cuenten como la misma
abreviatura a nivel de base de datos -- la garantía real contra duplicados insensibles a
mayúsculas, más allá del chequeo de aplicación (`ensure_unique_ci`, que solo evita el 409
crudo del `IntegrityError`, no sustituye al índice).
"""
from typing import Sequence, Union
from app.scripts.tenant import for_each_tenant_schema
from alembic import op
from sqlalchemy import text

revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


def _has_ci_duplicates(schema: str, column: str) -> bool:
    row = op.get_bind().execute(
        text(
            f'SELECT 1 FROM "{schema}".unit_measures GROUP BY upper({column}) '
            f'HAVING count(*) > 1 LIMIT 1'
        )
    ).first()
    return row is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    if not _has_table(schema, "unit_measures"):
        return
    # Si un tenant ya tiene duplicados insensibles a mayúsculas (dato preexistente), se
    # omite su índice en vez de romper la migración -- ese tenant necesita limpiar esos
    # datos a mano antes de que el índice pueda crearse; el resto de tenants no se bloquea.
    if not _has_ci_duplicates(schema, "name"):
        op.execute(
            f'CREATE UNIQUE INDEX IF NOT EXISTS ux_unit_measures_name_ci '
            f'ON "{schema}".unit_measures (upper(name))'
        )
    if not _has_ci_duplicates(schema, "abbreviation"):
        op.execute(
            f'CREATE UNIQUE INDEX IF NOT EXISTS ux_unit_measures_abbreviation_ci '
            f'ON "{schema}".unit_measures (upper(abbreviation))'
        )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "unit_measures"):
        return
    op.execute(f'DROP INDEX IF EXISTS "{schema}".ux_unit_measures_name_ci')
    op.execute(f'DROP INDEX IF EXISTS "{schema}".ux_unit_measures_abbreviation_ci')
