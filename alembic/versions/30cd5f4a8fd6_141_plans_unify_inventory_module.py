"""141_plans_unify_inventory_module: «Compras» deja de ser un módulo de plan

Revision ID: 30cd5f4a8fd6
Revises: 56a2cdbec89c
Create Date: 2026-10-31 16:09:38.516174

Spec 141 (A-150, A-152). «Inventario» pasa a cubrir Inventario, Proveedores y
Compras de forma indivisible, así que `shared.plans.compras_access` se absorbe en
`inventario_access` y se elimina:

    inventario_después = inventario_antes OR compras_antes

Ningún plan pierde acceso (FR-020). Solo toca el esquema `shared`: **no** recorre
los esquemas de los tenants, por lo que los insumos, proveedores, compras e
importaciones de cada negocio quedan intactos (FR-018, Principio VII).

Idempotente (FR-021): si `compras_access` ya no existe, `upgrade()` no hace nada.

Reversión (Principio VIII) — `downgrade()` recrea `compras_access` como
`boolean NOT NULL DEFAULT false` y la deja igual a `inventario_access`.
**Lo que no se recupera**: el dato «este plan tenía Inventario sin Compras» ni
«este plan tenía Compras sin Inventario»; tras `upgrade` + `downgrade` todo plan con
Inventario queda también con Compras. Es un enriquecimiento, nunca una pérdida.
El API ejecuta `alembic upgrade head` al arrancar, así que para volver a una imagen
anterior a la spec 141 hay que correr `alembic downgrade -1` **antes**.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '30cd5f4a8fd6'
down_revision: Union[str, Sequence[str], None] = '56a2cdbec89c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SHARED_SCHEMA = "shared"


def _has_compras_access(connection, schema: str | None) -> bool:
    columns = sa.inspect(connection).get_columns("plans", schema=schema)
    return "compras_access" in {c["name"] for c in columns}


def merge_compras_into_inventario(connection, schema: str | None = SHARED_SCHEMA) -> None:
    """Paso de datos puro: `inventario_access := inventario_access OR compras_access`.

    Recibe la conexión y el esquema para poder probarse sobre SQLite (`schema=None`)
    y sobre PostgreSQL real (`schema="shared"`). No elimina la columna. Idempotente:
    sin `compras_access` no hace nada.
    """
    if not _has_compras_access(connection, schema):
        return
    plans = sa.table(
        "plans",
        sa.column("inventario_access", sa.Boolean),
        sa.column("compras_access", sa.Boolean),
        schema=schema,
    )
    connection.execute(
        plans.update()
        .where(plans.c.compras_access.is_(True), plans.c.inventario_access.is_(False))
        .values(inventario_access=True)
    )


def upgrade() -> None:
    """Absorbe `compras_access` en `inventario_access` y elimina la columna."""
    bind = op.get_bind()
    if not _has_compras_access(bind, SHARED_SCHEMA):
        return
    merge_compras_into_inventario(bind, SHARED_SCHEMA)
    op.drop_column("plans", "compras_access", schema=SHARED_SCHEMA)


def downgrade() -> None:
    """Recrea `compras_access` igual a `inventario_access`.

    No se recupera la separación comercial previa entre Inventario y Compras (ver el
    docstring del módulo): es un enriquecimiento, nunca una pérdida de acceso.
    """
    bind = op.get_bind()
    if not _has_compras_access(bind, SHARED_SCHEMA):
        op.add_column(
            "plans",
            sa.Column("compras_access", sa.Boolean(), nullable=False, server_default=sa.false()),
            schema=SHARED_SCHEMA,
        )
    plans = sa.table(
        "plans",
        sa.column("inventario_access", sa.Boolean),
        sa.column("compras_access", sa.Boolean),
        schema=SHARED_SCHEMA,
    )
    bind.execute(plans.update().values(compras_access=plans.c.inventario_access))
