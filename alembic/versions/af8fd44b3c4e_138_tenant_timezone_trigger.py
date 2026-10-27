"""138_tenant_timezone_trigger

Revision ID: af8fd44b3c4e
Revises: c5815ab62693
Create Date: 2026-10-26 21:59:00.000000

spec 138 (FR-010, data-model.md §1): la zona horaria de un tenant se valida TAMBIÉN en la base de datos,
contra la base IANA del propio motor, con un disparador sobre `shared.tenants.timezone`. Un `CHECK` no sirve:
no admite subconsultas a `pg_timezone_names`, y una función `IMMUTABLE` que consulte el catálogo mentiría sobre
su inmutabilidad.

Regla (la misma que `validate_iana_timezone` en Python): válida si es `UTC` o tiene forma de región
(`Área/Lugar`, contiene `/`) **y** existe en `pg_timezone_names`. Un valor inválido lanza SQLSTATE `22023`
(`invalid_parameter_value`) con el mensaje «Zona horaria inválida: …».

Solo DDL, sobre `shared` (tabla única, NO por esquema de tenant: no va envuelta en `for_each_tenant_schema`).
Nunca corrige datos (Principio VII). **Si algún tenant ya tiene una zona inválida, NO crea el disparador: avisa
y termina SIN error.** Esta migración corre en el arranque del API (`api-entrypoint.sh`): abortar dejaría el
backend en bucle de reinicio y tumbaría también el resto de la spec. Tras corregir la zona con
`python -m app.scripts.set_tenant_timezone` se reaplica (`alembic downgrade -1 && alembic upgrade head` mientras
sea la cabeza; si ya hay migraciones posteriores, se ejecuta a mano el SQL del disparador de abajo).

Idempotente: `CREATE OR REPLACE FUNCTION` y `DROP TRIGGER IF EXISTS` + `CREATE TRIGGER`. `downgrade` quita el
disparador y la función y no toca ninguna fila.
"""
import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = 'af8fd44b3c4e'
down_revision: Union[str, Sequence[str], None] = 'c5815ab62693'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

TRIGGER = "trg_tenants_validate_timezone"
FUNCTION = "shared.tenants_validate_timezone"

_CREATE_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {FUNCTION}() RETURNS trigger AS $$
BEGIN
    IF NOT (
        (NEW.timezone = 'UTC' OR NEW.timezone LIKE '%/%')
        AND EXISTS (SELECT 1 FROM pg_timezone_names WHERE name = NEW.timezone)
    ) THEN
        RAISE EXCEPTION 'Zona horaria inválida: %', NEW.timezone USING ERRCODE = '22023';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def _tenants_con_zona_invalida() -> list[tuple[str, str]]:
    """`(host, zona)` de los tenants cuya zona NO cumpliría la regla del disparador."""
    filas = op.get_bind().execute(
        text(
            "SELECT t.host, t.timezone FROM shared.tenants t "
            "WHERE t.timezone IS NULL OR NOT ((t.timezone = 'UTC' OR t.timezone LIKE '%/%') "
            "AND EXISTS (SELECT 1 FROM pg_timezone_names n WHERE n.name = t.timezone)) "
            "ORDER BY t.id"
        )
    ).all()
    return [(host, zona) for host, zona in filas]


def upgrade() -> None:
    """Crea el disparador de validación, salvo que algún tenant ya tenga una zona inválida."""
    invalidos = _tenants_con_zona_invalida()
    if invalidos:
        detalle = ", ".join(f"{host}={zona!r}" for host, zona in invalidos)
        mensaje = (
            "[138] NO se creó el disparador de validación de zona horaria: hay tenants con una zona "
            f"inválida ({detalle}). El backend arranca igual. Corrígelos con "
            "`python -m app.scripts.set_tenant_timezone --host <host> --timezone <zona>` y reaplica la "
            "migración (`alembic downgrade -1 && alembic upgrade head`)."
        )
        logger.warning(mensaje)
        print(mensaje)
        return

    op.execute(text(_CREATE_FUNCTION))
    op.execute(text(f"DROP TRIGGER IF EXISTS {TRIGGER} ON shared.tenants"))
    op.execute(text(
        f"CREATE TRIGGER {TRIGGER} BEFORE INSERT OR UPDATE OF timezone ON shared.tenants "
        f"FOR EACH ROW EXECUTE FUNCTION {FUNCTION}()"
    ))


def downgrade() -> None:
    """Quita el disparador y la función. No modifica filas."""
    op.execute(text(f"DROP TRIGGER IF EXISTS {TRIGGER} ON shared.tenants"))
    op.execute(text(f"DROP FUNCTION IF EXISTS {FUNCTION}()"))
