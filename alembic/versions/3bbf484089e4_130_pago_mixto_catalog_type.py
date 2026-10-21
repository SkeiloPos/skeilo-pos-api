"""spec 130: agrega 'mixed' al CHECK de type en shared.payment_method_catalog y tenant.payment_methods

data-model.md § PaymentMethodCatalog/PaymentMethod (spec 130, FR-004): el Super Admin puede
crear una fila `type='mixed'` ("Pago mixto") en el catálogo de plataforma igual que cualquier
otra, y el tenant puede activarla (research.md Decisión 5 copia `catalog.type` tal cual a
`PaymentMethod.type`) — ambos CHECK amplían su lista de valores permitidos, sin backfill de
filas existentes (Principio VII).

Nota sobre los nombres de constraint usados en `drop_constraint`: `op.drop_constraint` en este
repo NO reaplica la convención de nombres (`ck__<tabla>__<nombre>`) que sí aplica
`op.create_check_constraint` al crear uno nuevo — hay que pasarle el nombre ya completo tal como
quedó grabado en Postgres (verificado contra una base real, no solo SQLite), o falla con
"constraint ... does not exist".

Revision ID: 3bbf484089e4
Revises: 43e57b9da5d9
Create Date: 2026-10-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

from app.scripts.tenant import for_each_tenant_schema

# revision identifiers, used by Alembic.
revision: str = '3bbf484089e4'
down_revision: Union[str, Sequence[str], None] = '43e57b9da5d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CATALOG_CK_FULL = "ck__payment_method_catalog__ck_payment_method_catalog_type"
_METHOD_CK_FULL = "ck__payment_methods__ck_payment_method_type"


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
def _upgrade_tenant_schema(schema: str) -> None:
    if not _has_table(schema, "payment_methods"):
        return
    if _has_constraint(schema, "payment_methods", _METHOD_CK_FULL):
        op.drop_constraint(_METHOD_CK_FULL, "payment_methods", schema=schema)
    op.create_check_constraint(
        "ck_payment_method_type",
        "payment_methods",
        "type IN ('cash', 'card', 'transfer', 'other', 'mixed')",
        schema=schema,
    )


@for_each_tenant_schema
def _downgrade_tenant_schema(schema: str) -> None:
    if not _has_table(schema, "payment_methods"):
        return
    if _has_constraint(schema, "payment_methods", _METHOD_CK_FULL):
        op.drop_constraint(_METHOD_CK_FULL, "payment_methods", schema=schema)
    op.create_check_constraint(
        "ck_payment_method_type",
        "payment_methods",
        "type IN ('cash', 'card', 'transfer', 'other')",
        schema=schema,
    )


def upgrade() -> None:
    """Upgrade schema."""
    if _has_table("shared", "payment_method_catalog"):
        if _has_constraint("shared", "payment_method_catalog", _CATALOG_CK_FULL):
            op.drop_constraint(_CATALOG_CK_FULL, "payment_method_catalog", schema="shared")
        op.create_check_constraint(
            "ck_payment_method_catalog_type",
            "payment_method_catalog",
            "type IN ('cash', 'card', 'transfer', 'other', 'mixed')",
            schema="shared",
        )
    _upgrade_tenant_schema()


def downgrade() -> None:
    """Downgrade schema."""
    if _has_table("shared", "payment_method_catalog"):
        if _has_constraint("shared", "payment_method_catalog", _CATALOG_CK_FULL):
            op.drop_constraint(_CATALOG_CK_FULL, "payment_method_catalog", schema="shared")
        op.create_check_constraint(
            "ck_payment_method_catalog_type",
            "payment_method_catalog",
            "type IN ('cash', 'card', 'transfer', 'other')",
            schema="shared",
        )
    _downgrade_tenant_schema()
