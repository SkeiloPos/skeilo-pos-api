"""123 addition tracking and sale kind

Revision ID: fe71d444fb50
Revises: e0faa21295a3
Create Date: 2026-10-17 12:53:47.354760

spec 123 (adiciones a línea de pedido existente con pago complementario). Por cada
esquema de tenant, dos cambios additivos:

  1. `order_item_options` gana `added_after_sale BOOLEAN NOT NULL DEFAULT false` y
     `complementary_sale_id UUID NULL REFERENCES sales(id)` — distinguen, sin tocar
     ninguna fila histórica, qué adiciones nacieron después de que el pedido ya tenía
     una `Sale` emitida y si ya fueron cubiertas por un pago complementario.
  2. `sales` gana `kind VARCHAR(20) NOT NULL DEFAULT 'original'` + CHECK (`'original'` |
     `'complementario'`) — distingue la venta del cobro normal de la que liquida un
     saldo pendiente. Toda `Sale` existente queda `'original'` por el default: es
     exactamente lo que era (Principio VII).

Ningún paso reescribe, recalcula ni cambia el significado de una fila existente. Ambas
columnas son additivas y reversibles con `DROP COLUMN` sin pérdida de datos de negocio.

Repetible y tolerante a esquemas ya en forma final (`IF NOT EXISTS`/`IF EXISTS`, mismo
patrón que la migración 121).

`downgrade` en orden inverso: quita el CHECK y la columna `kind` de `sales`; quita
`complementary_sale_id` y `added_after_sale` de `order_item_options`.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

from app.scripts.tenant import for_each_tenant_schema


# revision identifiers, used by Alembic.
revision: str = 'fe71d444fb50'
down_revision: Union[str, Sequence[str], None] = 'e0faa21295a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CK_SALE_KIND = "ck__sales__ck_sale_kind"
_FK_COMPLEMENTARY_SALE = "fk__order_item_options__complementary_sale_id__sales"


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    if not _has_table(schema, "sales"):
        return

    quote = op.get_bind().dialect.identifier_preparer.quote
    s = quote(schema)

    # 1. sales.kind
    op.execute(text(
        f"ALTER TABLE {s}.sales ADD COLUMN IF NOT EXISTS kind VARCHAR(20) "
        "NOT NULL DEFAULT 'original'"
    ))
    op.execute(text(f"ALTER TABLE {s}.sales DROP CONSTRAINT IF EXISTS {_CK_SALE_KIND}"))
    op.execute(text(
        f"ALTER TABLE {s}.sales ADD CONSTRAINT {_CK_SALE_KIND} "
        "CHECK (kind IN ('original', 'complementario'))"
    ))

    if not _has_table(schema, "order_item_options"):
        return

    # 2. order_item_options.added_after_sale / complementary_sale_id
    op.execute(text(
        f"ALTER TABLE {s}.order_item_options ADD COLUMN IF NOT EXISTS "
        "added_after_sale BOOLEAN NOT NULL DEFAULT false"
    ))
    op.execute(text(
        f"ALTER TABLE {s}.order_item_options ADD COLUMN IF NOT EXISTS "
        "complementary_sale_id UUID"
    ))
    op.execute(text(f"ALTER TABLE {s}.order_item_options DROP CONSTRAINT IF EXISTS {_FK_COMPLEMENTARY_SALE}"))
    op.execute(text(
        f"ALTER TABLE {s}.order_item_options ADD CONSTRAINT {_FK_COMPLEMENTARY_SALE} "
        f"FOREIGN KEY (complementary_sale_id) REFERENCES {s}.sales (id)"
    ))


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "sales"):
        return

    quote = op.get_bind().dialect.identifier_preparer.quote
    s = quote(schema)

    if _has_table(schema, "order_item_options"):
        op.execute(text(f"ALTER TABLE {s}.order_item_options DROP CONSTRAINT IF EXISTS {_FK_COMPLEMENTARY_SALE}"))
        op.execute(text(f"ALTER TABLE {s}.order_item_options DROP COLUMN IF EXISTS complementary_sale_id"))
        op.execute(text(f"ALTER TABLE {s}.order_item_options DROP COLUMN IF EXISTS added_after_sale"))

    op.execute(text(f"ALTER TABLE {s}.sales DROP CONSTRAINT IF EXISTS {_CK_SALE_KIND}"))
    op.execute(text(f"ALTER TABLE {s}.sales DROP COLUMN IF EXISTS kind"))
