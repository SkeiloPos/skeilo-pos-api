"""spec 161: notification_events.audience_role e índice de avisos de suscripción únicos

Revision ID: a65d3ff9da49
Revises: e0230fd88635
Create Date: 2026-11-17 00:00:01.000000

Segunda y última revisión de DDL de la spec 161 (la primera, `e0230fd88635`, crea
`shared.subscription_payments`). Aquí se agrega, a la tabla `notification_events` de CADA esquema de tenant
(y a `tenant_default`):

- `audience_role VARCHAR(20) NULL`: rol destinatario de la notificación (FR-031, A-164). `NULL` = todo el
  personal, que es lo que significa en todas las filas existentes, así que NO hay backfill y ningún evento
  anterior cambia de comportamiento (FR-037).
- el índice único parcial `ux__notification_events__subscription_once` sobre `(event_type, related_entity_id)`
  solo para `event_type LIKE 'subscription.%'`: un aviso de suscripción por tipo y ciclo de vencimiento
  (FR-029, SC-008), aunque dos instancias del planificador corran a la vez.

Solo DDL aditivo y reversible (Principio VIII). Idempotente (`IF NOT EXISTS`) y tolerante a un esquema que aún
no tiene la tabla (p. ej. `tenant_default` en una base recién creada).

Reversión: borra primero las filas con audiencia (los avisos de suscripción, que son transitorios y se
regeneran) y luego el índice y la columna. Si se quitara la columna sin borrarlas, esos avisos del ADMIN
pasarían a verse para todo el personal.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

from app.scripts.tenant import for_each_tenant_schema

# revision identifiers, used by Alembic.
revision: str = 'a65d3ff9da49'
down_revision: Union[str, Sequence[str], None] = 'e0230fd88635'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "notification_events"
_INDEX = "ux__notification_events__subscription_once"


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}).scalar() is not None


def upgrade_statements(schema_quoted: str) -> list[str]:
    """SQL del `upgrade` para un esquema (ya citado). Aparte de `upgrade()` para poder revisarlo y validarlo."""
    return [
        f"ALTER TABLE {schema_quoted}.{_TABLE} ADD COLUMN IF NOT EXISTS audience_role VARCHAR(20)",
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX} ON {schema_quoted}.{_TABLE} "
        f"(event_type, related_entity_id) WHERE event_type LIKE 'subscription.%'",
    ]


def downgrade_statements(schema_quoted: str) -> list[str]:
    return [
        # Avisos dirigidos a un rol: se borran antes de quitar la columna (ver el docstring del módulo).
        f"DELETE FROM {schema_quoted}.{_TABLE} WHERE audience_role IS NOT NULL",
        f"DROP INDEX IF EXISTS {schema_quoted}.{_INDEX}",
        f"ALTER TABLE {schema_quoted}.{_TABLE} DROP COLUMN IF EXISTS audience_role",
    ]


def _quoted(schema: str) -> str:
    return sa.sql.compiler.IdentifierPreparer(op.get_bind().dialect).format_schema(schema)


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    if not _has_table(schema, _TABLE):
        return
    for statement in upgrade_statements(_quoted(schema)):
        op.execute(text(statement))


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, _TABLE):
        return
    for statement in downgrade_statements(_quoted(schema)):
        op.execute(text(statement))
