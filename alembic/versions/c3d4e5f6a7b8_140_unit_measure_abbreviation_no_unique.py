"""unit_measures.abbreviation: sin restricción de unicidad (spec 140)

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-10-27 00:00:00.000000

Tercer ajuste del mismo campo dentro de la misma spec, por feedback directo del usuario:
la abreviatura puede repetirse libremente -- ni siquiera dentro del mismo grupo. Solo el
NOMBRE sigue siendo único en todo el tenant (sin cambios, `ux_unit_measures_name_ci`).

Elimina `ux_unit_measures_group_abbreviation_ci` (el índice único por grupo agregado en
`b2c3d4e5f6a7`) y cualquier otro índice único remanente sobre `abbreviation` que pudiera
quedar de versiones anteriores de esta misma spec. No crea ningún índice nuevo.
"""
from typing import Sequence, Union
from app.scripts.tenant import for_each_tenant_schema
from alembic import op
from sqlalchemy import text

revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


def _unique_abbreviation_indexes(schema: str) -> list[str]:
    """Cualquier índice único cuya definición mencione `abbreviation` (sola o junto a
    `group_id`), sin importar el nombre -- cubre tanto `ux_unit_measures_abbreviation_ci`
    (global, e5f6a7b8c9d0) como `ux_unit_measures_group_abbreviation_ci` (por grupo,
    b2c3d4e5f6a7) y cualquier residuo del provisioning original de cada tenant."""
    rows = op.get_bind().execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = :schema AND tablename = 'unit_measures' "
            "AND indexdef ILIKE '%UNIQUE%' AND indexdef ILIKE '%abbreviation%'"
        ),
        {"schema": schema},
    ).scalars().all()
    return list(rows)


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    if not _has_table(schema, "unit_measures"):
        return
    for index_name in _unique_abbreviation_indexes(schema):
        op.execute(f'DROP INDEX IF EXISTS "{schema}"."{index_name}"')


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "unit_measures"):
        return
    op.execute(
        f'CREATE UNIQUE INDEX IF NOT EXISTS ux_unit_measures_group_abbreviation_ci '
        f'ON "{schema}".unit_measures (group_id, upper(abbreviation))'
    )
