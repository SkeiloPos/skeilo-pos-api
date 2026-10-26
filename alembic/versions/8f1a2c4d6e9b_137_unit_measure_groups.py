"""137_unit_measure_groups

Revision ID: 8f1a2c4d6e9b
Revises: c3d9a1f08b42, e4e9886ef15b
Create Date: 2026-10-26 10:00:00.000000

spec 137: formaliza `UnitMeasure.dimension` (texto libre, spec 134) como una entidad real
`UnitMeasureGroup` con una unidad base explícita (`UnitMeasure.is_base`). Backfill: un grupo
por `dimension` distinto; base = unidad con `factor_to_base = 1` más antigua
(`created_at` mínimo) si hay exactamente una, o la más antigua de todas si hay cero o más
de una con `factor_to_base = 1` (Edge Case del spec). `dimension` se conserva como espejo
sincronizado con `UnitMeasureGroup.name` (research.md D2) -- ningún `factor_to_base`,
`active`, nombre o abreviatura existente se altera (FR-012). Ver data-model.md/research.md
D4 para el detalle de cada decisión. Esta revisión también fusiona los dos heads previos
(`c3d9a1f08b42`/`e4e9886ef15b`).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from app.scripts.tenant import for_each_tenant_schema


# revision identifiers, used by Alembic.
revision: str = '8f1a2c4d6e9b'
down_revision: Union[str, Sequence[str], None] = ('c3d9a1f08b42', 'e4e9886ef15b')
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


def _has_index(schema: str, index_name: str) -> bool:
    return op.get_bind().execute(
        text(
            "SELECT 1 FROM pg_indexes WHERE schemaname = :s AND indexname = :i"
        ),
        {"s": schema, "i": index_name},
    ).scalar() is not None


@for_each_tenant_schema
def _upgrade_tenant_schema(schema: str) -> None:
    if not _has_table(schema, "unit_measures"):
        return

    bind = op.get_bind()

    if not _has_table(schema, "unit_measure_groups"):
        op.create_table(
            "unit_measure_groups",
            sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id", name="pk__unit_measure_groups"),
            sa.UniqueConstraint("name", name="uq__unit_measure_groups__name"),
            schema=schema,
        )

    if not _has_column(schema, "unit_measures", "group_id"):
        op.add_column(
            "unit_measures",
            sa.Column("group_id", PGUUID(as_uuid=True), nullable=True),
            schema=schema,
        )
    if not _has_column(schema, "unit_measures", "is_base"):
        op.add_column(
            "unit_measures",
            sa.Column("is_base", sa.Boolean(), nullable=False, server_default="false"),
            schema=schema,
        )

    # Backfill: un UnitMeasureGroup por cada `dimension` distinta sin grupo todavía.
    dimensions = bind.execute(
        text(f"SELECT DISTINCT dimension FROM {schema}.unit_measures WHERE group_id IS NULL")
    ).scalars().all()
    for dimension in dimensions:
        existing_group_id = bind.execute(
            text(f"SELECT id FROM {schema}.unit_measure_groups WHERE name = :name"),
            {"name": dimension},
        ).scalar()
        if existing_group_id is None:
            existing_group_id = bind.execute(
                text(
                    f"INSERT INTO {schema}.unit_measure_groups (id, name) "
                    "VALUES (gen_random_uuid(), :name) RETURNING id"
                ),
                {"name": dimension},
            ).scalar()
        bind.execute(
            text(
                f"UPDATE {schema}.unit_measures SET group_id = :group_id "
                "WHERE dimension = :dimension AND group_id IS NULL"
            ),
            {"group_id": existing_group_id, "dimension": dimension},
        )

    # Determinar la base por grupo (Edge Case: desempate por created_at más antigua).
    group_ids = bind.execute(
        text(f"SELECT id FROM {schema}.unit_measure_groups")
    ).scalars().all()
    for group_id in group_ids:
        already_based = bind.execute(
            text(
                f"SELECT 1 FROM {schema}.unit_measures "
                "WHERE group_id = :gid AND is_base = true"
            ),
            {"gid": group_id},
        ).scalar()
        if already_based:
            continue

        base_id = bind.execute(
            text(
                f"SELECT id FROM {schema}.unit_measures "
                "WHERE group_id = :gid AND factor_to_base = 1 "
                "ORDER BY created_at ASC LIMIT 1"
            ),
            {"gid": group_id},
        ).scalar()
        if base_id is None:
            base_id = bind.execute(
                text(
                    f"SELECT id FROM {schema}.unit_measures "
                    "WHERE group_id = :gid ORDER BY created_at ASC LIMIT 1"
                ),
                {"gid": group_id},
            ).scalar()
        if base_id is not None:
            bind.execute(
                text(f"UPDATE {schema}.unit_measures SET is_base = true WHERE id = :id"),
                {"id": base_id},
            )

    op.alter_column("unit_measures", "group_id", nullable=False, schema=schema)

    op.create_foreign_key(
        "fk__unit_measures__group_id__unit_measure_groups",
        "unit_measures", "unit_measure_groups",
        ["group_id"], ["id"],
        source_schema=schema, referent_schema=schema,
    )

    index_name = "uq__unit_measures__group_id__is_base_true"
    if not _has_index(schema, index_name):
        op.create_index(
            index_name, "unit_measures", ["group_id"],
            unique=True, schema=schema,
            postgresql_where=text("is_base = true"),
        )


@for_each_tenant_schema
def _downgrade_tenant_schema(schema: str) -> None:
    if not _has_table(schema, "unit_measures"):
        return

    index_name = "uq__unit_measures__group_id__is_base_true"
    if _has_index(schema, index_name):
        op.drop_index(index_name, table_name="unit_measures", schema=schema)

    if _has_column(schema, "unit_measures", "group_id"):
        # Recomponer `dimension` desde `UnitMeasureGroup.name` por trazabilidad (ya
        # deberían coincidir por el espejo de research.md D2); no hay pérdida de datos
        # porque `dimension`/`factor_to_base` nunca se sobrescribieron en el upgrade.
        op.get_bind().execute(
            text(
                f"UPDATE {schema}.unit_measures u "
                f"SET dimension = g.name "
                f"FROM {schema}.unit_measure_groups g "
                "WHERE u.group_id = g.id"
            )
        )
        op.drop_constraint(
            "fk__unit_measures__group_id__unit_measure_groups",
            "unit_measures", schema=schema, type_="foreignkey",
        )
        op.drop_column("unit_measures", "group_id", schema=schema)

    if _has_column(schema, "unit_measures", "is_base"):
        op.drop_column("unit_measures", "is_base", schema=schema)

    if _has_table(schema, "unit_measure_groups"):
        op.drop_table("unit_measure_groups", schema=schema)


def upgrade() -> None:
    """Upgrade schema."""
    _upgrade_tenant_schema()


def downgrade() -> None:
    """Downgrade schema."""
    _downgrade_tenant_schema()
