"""spec 132: conjunto cerrado de motivos de rechazo en tenant.order_payment_attempts

data-model.md § OrderPaymentAttempt (spec 132, FR-015, research.md Decisión 2): agrega un
`CheckConstraint` que restringe `rejection_reason` a los 5 valores del conjunto cerrado
(`RejectionReason`, `app/models/order_payment_attempt.py`) cuando `status = 'rechazado'` —
coexiste con el `CheckConstraint` ya existente (`ck_order_payment_attempt_rejection_reason`)
que solo exige que no sea `NULL` en ese caso.

Auditoría previa (T002, verificada contra la base real `pos_db`): el único tenant con filas
(`heladeria`) tenía dos valores de texto libre fuera del conjunto cerrado —
`'Cancelado por el comensal'` y `'el monto transferido es insuficiente'` — de rechazos
anteriores a este spec. Antes de aplicar el `CheckConstraint`, esta migración los normaliza a
`comprobante_sospechoso` (motivo catch-all más conservador) en la misma transacción; no se
reinterpreta retroactivamente la decisión de negocio ya tomada en su momento (Principio VII),
solo se corrige el dato para que pueda convivir con el nuevo constraint.

Rollback: `downgrade()` elimina únicamente el `CheckConstraint` agregado; no hay pérdida de
datos porque no se eliminan columnas ni se revierte la normalización de valores (idempotente de
re-ejecutar: una fila ya normalizada a `comprobante_sospechoso` sigue coincidiendo con el nuevo
`UPDATE ... WHERE rejection_reason NOT IN (...)` si se corriera otra vez).

Revision ID: a7f3c9d1b246
Revises: 1500f17ff5e6
Create Date: 2026-10-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

from app.scripts.tenant import for_each_tenant_schema

# revision identifiers, used by Alembic.
revision: str = 'a7f3c9d1b246'
down_revision: Union[str, Sequence[str], None] = '1500f17ff5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CLOSED_SET = (
    'cliente_no_muestra_app',
    'monto_no_coincide',
    'transferencia_no_completada',
    'cliente_se_niega_a_mostrar',
    'comprobante_sospechoso',
)

# Nombre real tras la convención de nombres (`app/core/models.py::convention`) Y el truncado a
# 63 bytes + hash que aplica Postgres/SQLAlchemy porque el nombre completo lo excede —
# verificado contra una base real (mismo cuidado que `3bbf484089e4_130_pago_mixto_catalog_type`,
# que documenta el primer caso de este mismo gotcha, sin el truncado adicional por longitud).
_CK_ENUM_FULL = "ck__order_payment_attempts__ck_order_payment_attempt_re_1973"


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


def _has_constraint(schema: str, table: str, constraint: str) -> bool:
    return op.get_bind().execute(
        text(
            "SELECT 1 FROM pg_constraint c "
            "JOIN pg_namespace n ON n.oid = c.connamespace "
            "WHERE n.nspname = :s AND c.conrelid = CAST(:t AS regclass) AND c.conname = :c"
        ),
        {"s": schema, "t": f"{schema}.{table}", "c": constraint},
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    """Upgrade schema."""
    if not _has_table(schema, "order_payment_attempts"):
        return

    # T002: normaliza valores fuera del conjunto cerrado al motivo catch-all
    # más conservador, antes de que el CheckConstraint pueda rechazarlos.
    # `_CLOSED_SET` es una tupla fija interna (no input de usuario): se arma
    # el `IN (...)` en texto porque `text()` no soporta expandir una tupla
    # Python en un `IN` sin un `bindparam(expanding=True)` adicional.
    closed_set_sql = ", ".join(f"'{value}'" for value in _CLOSED_SET)
    op.get_bind().execute(
        text(
            f'UPDATE "{schema}".order_payment_attempts '
            "SET rejection_reason = 'comprobante_sospechoso' "
            "WHERE status = 'rechazado' AND rejection_reason IS NOT NULL "
            f"AND rejection_reason NOT IN ({closed_set_sql})"
        )
    )

    if not _has_constraint(schema, "order_payment_attempts", _CK_ENUM_FULL):
        op.create_check_constraint(
            "ck_order_payment_attempt_rejection_reason_enum",
            "order_payment_attempts",
            "status != 'rechazado' OR rejection_reason IN "
            "('cliente_no_muestra_app', 'monto_no_coincide', 'transferencia_no_completada', "
            "'cliente_se_niega_a_mostrar', 'comprobante_sospechoso')",
            schema=schema,
        )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    """Downgrade schema."""
    if not _has_table(schema, "order_payment_attempts"):
        return
    if _has_constraint(schema, "order_payment_attempts", _CK_ENUM_FULL):
        op.drop_constraint(_CK_ENUM_FULL, "order_payment_attempts", schema=schema)
