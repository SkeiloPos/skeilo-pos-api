"""139_remove_unit_measure_dimension

Revision ID: bc75fbc0b894
Revises: 8f1a2c4d6e9b
Create Date: 2026-10-26 13:04:30.758846

spec 139: elimina `UnitMeasure.dimension`, columna espejo de `UnitMeasureGroup.name`
mantenida como deuda técnica aceptada por la spec 137 (research.md D2) únicamente para no
tocar `app/core/units.py::convert()`. `group_id` -> `unit_measure_groups` pasa a ser la
única fuente de verdad del grupo de una unidad de medida. El downgrade recompone
`dimension` desde `group.name`, replicando el mismo patrón que usa el downgrade de
`8f1a2c4d6e9b_137_unit_measure_groups.py` antes de dropear `group_id`. No hay pérdida de
datos en ningún sentido: `dimension` y `group.name` estaban garantizados sincronizados.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

from app.scripts.tenant import for_each_tenant_schema


# revision identifiers, used by Alembic.
revision: str = 'bc75fbc0b894'
down_revision: Union[str, Sequence[str], None] = '8f1a2c4d6e9b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


def _has_column(schema: str, table: str, column: str) -> bool:
    return op.get_bind().execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name = :t AND column_name = :c"
        ),
        {"s": schema, "t": table, "c": column},
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    """Upgrade schema."""
    if not _has_table(schema, "unit_measures"):
        return

    if _has_column(schema, "unit_measures", "dimension"):
        op.drop_column("unit_measures", "dimension", schema=schema)


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    """Downgrade schema."""
    if not _has_table(schema, "unit_measures"):
        return

    if not _has_column(schema, "unit_measures", "dimension"):
        op.add_column(
            "unit_measures",
            sa.Column("dimension", sa.String(length=100), nullable=True),
            schema=schema,
        )

        # Recomponer `dimension` desde `UnitMeasureGroup.name` (espejo exacto de
        # research.md D2 antes de esta spec); no hay pérdida de datos.
        op.get_bind().execute(
            text(
                f"UPDATE {schema}.unit_measures u "
                f"SET dimension = g.name "
                f"FROM {schema}.unit_measure_groups g "
                "WHERE u.group_id = g.id"
            )
        )

        op.alter_column("unit_measures", "dimension", nullable=False, schema=schema)
