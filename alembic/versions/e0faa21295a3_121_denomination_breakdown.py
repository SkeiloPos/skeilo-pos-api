"""121 denomination breakdown

Revision ID: e0faa21295a3
Revises: cd3e314a79b8
Create Date: 2026-10-15 00:00:00.000000

spec 121 (desglose por denominación en el arqueo de cierre; A-130 a A-134). Por cada
esquema de tenant, en una sola transacción (todo es DDL transaccional de PostgreSQL y
no hay índices nuevos sobre tablas grandes):

  1. Crea `denomination_catalog` (catálogo de denominaciones del tenant, R1) y la siembra
     con los 11 valores COP por defecto (FR-009). La lista va escrita como literal AQUÍ:
     una migración no debe importar código de aplicación que luego cambie.
  2. COMPUERTA FR-028: si alguna línea de `cash_count_denominations` tiene un valor que no
     está en el catálogo, ABORTA la migración nombrando esquema y valores. Nada se
     descarta en silencio.
  3. `cash_count_denominations.kind` (D-4): columna nula -> se rellena desde el catálogo
     por valor -> `NOT NULL` + `CHECK (kind IN ('billete','moneda'))`.
  4. Descarta las líneas con cantidad 0 (D-3, ANTES del nuevo CHECK) y reemplaza
     `CHECK (quantity >= 0)` por `CHECK (quantity > 0)` conservando el nombre.
  5. `cash_shifts.closed_by_user_id` / `closed_by_user_name` (D-5, FR-029), nulos: no se
     infieren del usuario de apertura en los turnos históricos.

Ningún paso lee ni escribe `counted_amount`, `close_note`, la diferencia ni el esperado de
ningún turno (SC-004, Principio VII). Las líneas con cantidad > 0 conservan valor y
cantidad; solo ganan `kind`.

Repetible y tolerante a esquemas ya en forma final: un tenant nacido de `create_all`
después de esta spec ya tiene tabla, columnas y restricciones con ESTOS MISMOS nombres
(los de la convención de `app/core/models.py`), así que `IF NOT EXISTS` / `IF EXISTS` los
reconocen. Los pasos de datos son funciones de módulo que reciben `Connection` y esquema
(SQL portable) para poder probarse sobre SQLite.

`downgrade` en orden inverso: quita `closed_by_*`, restaura `CHECK (quantity >= 0)`, quita
`kind` y su CHECK y borra `denomination_catalog`. No pierde valor ni cantidad de ninguna
línea conservada; sí pierde `kind`, `closed_by_*` y -- solo restaurables desde el respaldo
`pg_dump --table` previo (quickstart, paso 1) -- las filas con cantidad 0 descartadas.
"""
import logging
import uuid
from decimal import Decimal
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

from app.scripts.tenant import for_each_tenant_schema


# revision identifiers, used by Alembic.
revision: str = 'e0faa21295a3'
down_revision: Union[str, Sequence[str], None] = 'cd3e314a79b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

# Catálogo COP por defecto (FR-009): idéntico a `DEFAULT_COP_DENOMINATIONS` de
# `app/models/denomination_catalog.py`, pero escrito aquí a propósito.
_CATALOG = (
    (Decimal("100000"), "billete"),
    (Decimal("50000"), "billete"),
    (Decimal("20000"), "billete"),
    (Decimal("10000"), "billete"),
    (Decimal("5000"), "billete"),
    (Decimal("2000"), "billete"),
    (Decimal("1000"), "moneda"),
    (Decimal("500"), "moneda"),
    (Decimal("200"), "moneda"),
    (Decimal("100"), "moneda"),
    (Decimal("50"), "moneda"),
)

# Nombres IDÉNTICOS a los que produce la convención de `app/core/models.py` para
# los modelos (un tenant nuevo nace de `create_all`; uno migrado, de aquí).
_CK_QUANTITY = "ck__cash_count_denominations__ck_cash_count_quantity_positive"
_CK_KIND = "ck__cash_count_denominations__ck_cash_count_kind"


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


def _qualified(conn, schema, table: str) -> str:
    """`"esquema".tabla`; sin esquema (pruebas sobre SQLite) solo la tabla."""
    if schema is None:
        return table
    return f"{conn.dialect.identifier_preparer.quote(schema)}.{table}"


# ---------------------------------------------------------------- pasos de datos
def _seed_catalog(conn, schema) -> None:
    """Paso 1: siembra los 11 valores que falten (idempotente)."""
    tabla = sa.table(
        "denomination_catalog",
        sa.column("id", sa.Uuid()),
        sa.column("denomination", sa.Numeric(12, 2)),
        sa.column("kind", sa.String()),
        schema=schema,
    )
    for valor, tipo in _CATALOG:
        existe = conn.execute(
            sa.select(sa.literal(1)).select_from(tabla).where(tabla.c.denomination == valor)
        ).first()
        if existe is None:
            conn.execute(sa.insert(tabla).values(id=uuid.uuid4(), denomination=valor, kind=tipo))


def _find_out_of_catalog(conn, schema) -> list[Decimal]:
    """Paso 2: valores de línea de desglose que no están en el catálogo (FR-028)."""
    lineas = _qualified(conn, schema, "cash_count_denominations")
    catalogo = _qualified(conn, schema, "denomination_catalog")
    filas = conn.execute(text(
        f"SELECT DISTINCT c.denomination FROM {lineas} c "
        f"WHERE c.denomination NOT IN (SELECT denomination FROM {catalogo}) "
        f"ORDER BY c.denomination DESC"
    )).fetchall()
    return [Decimal(str(f[0])) for f in filas]


def _gate_message(schema, valores) -> str:
    lista = ", ".join(f"${int(v):,}".replace(",", ".") for v in valores)
    return (
        f"Migración 121 abortada (FR-028): el esquema '{schema}' tiene líneas de "
        f"desglose con denominaciones fuera del catálogo: {lista}. Resuélvalo con el "
        "negocio antes de migrar; la migración no descarta nada en silencio."
    )


def _gate(conn, schema) -> None:
    fuera = _find_out_of_catalog(conn, schema)
    if fuera:
        raise RuntimeError(_gate_message(schema, fuera))


def _backfill_kind(conn, schema) -> int:
    """Paso 3: rellena `kind` por valor desde el catálogo, solo donde es NULL."""
    lineas = _qualified(conn, schema, "cash_count_denominations")
    catalogo = _qualified(conn, schema, "denomination_catalog")
    res = conn.execute(text(
        f"UPDATE {lineas} SET kind = ("
        f"SELECT c.kind FROM {catalogo} c WHERE c.denomination = {lineas}.denomination"
        f") WHERE kind IS NULL"
    ))
    return res.rowcount


def _discard_zero_quantity(conn, schema) -> int:
    """Paso 4 (D-3): descarta las líneas con cantidad 0. Va ANTES del nuevo CHECK."""
    lineas = _qualified(conn, schema, "cash_count_denominations")
    res = conn.execute(text(f"DELETE FROM {lineas} WHERE quantity = 0"))
    descartadas = res.rowcount
    if descartadas:
        logger.info("121: %s líneas con cantidad 0 descartadas en %s", descartadas, schema)
    return descartadas


# ---------------------------------------------------------------------- upgrade
@for_each_tenant_schema
def upgrade(schema: str) -> None:
    # Los esquemas de scratch (tenant_default) pueden no tener las tablas base.
    if not _has_table(schema, "cash_shifts"):
        return

    conn = op.get_bind()
    quote = conn.dialect.identifier_preparer.quote
    s = quote(schema)

    # 1. Catálogo + siembra.
    op.execute(text(
        f"CREATE TABLE IF NOT EXISTS {s}.denomination_catalog ("
        "id UUID NOT NULL, "
        "denomination NUMERIC(12, 2) NOT NULL, "
        "kind VARCHAR(10) NOT NULL, "
        "CONSTRAINT pk__denomination_catalog PRIMARY KEY (id), "
        "CONSTRAINT uq__denomination_catalog__denomination UNIQUE (denomination), "
        "CONSTRAINT ck__denomination_catalog__ck_denomination_value_positive "
        "CHECK (denomination > 0), "
        "CONSTRAINT ck__denomination_catalog__ck_denomination_kind "
        "CHECK (kind IN ('billete','moneda')))"
    ))
    _seed_catalog(conn, schema)

    if _has_table(schema, "cash_count_denominations"):
        # 2. Compuerta FR-028: aborta sin tocar nada si hay valores fuera del catálogo.
        _gate(conn, schema)

        # 3. kind: nulo -> rellenar -> NOT NULL + CHECK.
        op.execute(text(f"ALTER TABLE {s}.cash_count_denominations ADD COLUMN IF NOT EXISTS kind VARCHAR(10)"))
        _backfill_kind(conn, schema)
        op.execute(text(f"ALTER TABLE {s}.cash_count_denominations ALTER COLUMN kind SET NOT NULL"))
        op.execute(text(f"ALTER TABLE {s}.cash_count_denominations DROP CONSTRAINT IF EXISTS {_CK_KIND}"))
        op.execute(text(
            f"ALTER TABLE {s}.cash_count_denominations ADD CONSTRAINT {_CK_KIND} "
            "CHECK (kind IN ('billete','moneda'))"
        ))

        # 4. Sin líneas en cero: el DELETE va antes del nuevo CHECK.
        _discard_zero_quantity(conn, schema)
        op.execute(text(f"ALTER TABLE {s}.cash_count_denominations DROP CONSTRAINT IF EXISTS {_CK_QUANTITY}"))
        op.execute(text(
            f"ALTER TABLE {s}.cash_count_denominations ADD CONSTRAINT {_CK_QUANTITY} "
            "CHECK (quantity > 0)"
        ))

    # 5. Quién cerró el turno.
    op.execute(text(f"ALTER TABLE {s}.cash_shifts ADD COLUMN IF NOT EXISTS closed_by_user_id UUID"))
    op.execute(text(f"ALTER TABLE {s}.cash_shifts ADD COLUMN IF NOT EXISTS closed_by_user_name VARCHAR(255)"))


# -------------------------------------------------------------------- downgrade
@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "cash_shifts"):
        return

    quote = op.get_bind().dialect.identifier_preparer.quote
    s = quote(schema)

    op.execute(text(f"ALTER TABLE {s}.cash_shifts DROP COLUMN IF EXISTS closed_by_user_name"))
    op.execute(text(f"ALTER TABLE {s}.cash_shifts DROP COLUMN IF EXISTS closed_by_user_id"))

    if _has_table(schema, "cash_count_denominations"):
        # Vuelve a `quantity >= 0` con el nombre original.
        op.execute(text(f"ALTER TABLE {s}.cash_count_denominations DROP CONSTRAINT IF EXISTS {_CK_QUANTITY}"))
        op.execute(text(
            f"ALTER TABLE {s}.cash_count_denominations ADD CONSTRAINT {_CK_QUANTITY} "
            "CHECK (quantity >= 0)"
        ))
        op.execute(text(f"ALTER TABLE {s}.cash_count_denominations DROP CONSTRAINT IF EXISTS {_CK_KIND}"))
        op.execute(text(f"ALTER TABLE {s}.cash_count_denominations DROP COLUMN IF EXISTS kind"))

    op.execute(text(f"DROP TABLE IF EXISTS {s}.denomination_catalog"))
