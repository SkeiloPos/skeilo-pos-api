"""Rename order status 'recibida' to 'pendiente'; add CustomerOrder.payment_status

Spec 146 (A-150): toda orden nueva, de cualquier canal (QR, caja/mostrador,
domicilio), nace ahora en `status='pendiente'` en vez de `'recibida'`
(`'recibida'` deja de ser un valor válido). Se añade también una columna nueva
`payment_status` (`UNPAID`/`PAID`/`PARTIAL`, default `'UNPAID'`), que no
existía: el pago se infería hasta ahora solo de `status == 'pagada'`.

upgrade(schema), por cada esquema (`tenant_default` + cada tenant de
`shared.tenants`):
  1. drop del CHECK `ck_customer_order_status`.
  2. `UPDATE customer_orders SET status = 'pendiente' WHERE status = 'recibida'`.
  3. recrea el CHECK sin `'recibida'`, con `'pendiente'` en su lugar.
  4. `ADD COLUMN payment_status VARCHAR(10) NOT NULL DEFAULT 'UNPAID'`.
  5. backfill: toda orden ya `'pagada'` → `payment_status = 'PAID'` (no hay
     forma de reconstruir retroactivamente un pago parcial/mixto histórico;
     limitación conocida, no se inventa el dato).
  6. CHECK nuevo `ck_customer_order_payment_status`.

**Limitación de rollback documentada** (data-model.md): el downgrade revierte
*todas* las filas en `'pendiente'` a `'recibida'`, incluidas las creadas
después del upgrade que nunca fueron `'recibida'` originalmente (p. ej.
órdenes de caja/domicilio creadas ya bajo el comportamiento nuevo). No hay
manera de distinguir retroactivamente el origen de una fila en `'pendiente'`.
Aceptable porque un rollback de esta migración solo tiene sentido como
procedimiento de emergencia inmediatamente después del despliegue.

Revision ID: e967f9848848
Revises: 56a2cdbec89c
Create Date: 2026-11-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

from app.scripts.tenant import for_each_tenant_schema

revision: str = 'e967f9848848'
down_revision: Union[str, Sequence[str], None] = '56a2cdbec89c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    if not _has_table(schema, "customer_orders"):
        return

    op.drop_constraint(
        "ck_customer_order_status", "customer_orders", type_="check", schema=schema,
    )
    op.execute(text(
        f'UPDATE "{schema}".customer_orders SET status = \'pendiente\' '
        f"WHERE status = 'recibida'"
    ))
    op.create_check_constraint(
        "ck_customer_order_status", "customer_orders",
        "status IN ('pendiente', 'abierta', 'bloqueada', 'pagada', 'cancelada')",
        schema=schema,
    )

    op.add_column(
        "customer_orders",
        sa.Column("payment_status", sa.String(10), nullable=False, server_default="UNPAID"),
        schema=schema,
    )
    op.execute(text(
        f'UPDATE "{schema}".customer_orders SET payment_status = \'PAID\' '
        f"WHERE status = 'pagada'"
    ))
    op.create_check_constraint(
        "ck_customer_order_payment_status", "customer_orders",
        "payment_status IN ('UNPAID', 'PAID', 'PARTIAL')",
        schema=schema,
    )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "customer_orders"):
        return

    op.drop_constraint(
        "ck_customer_order_payment_status", "customer_orders", type_="check",
        schema=schema,
    )
    op.drop_column("customer_orders", "payment_status", schema=schema)

    op.drop_constraint(
        "ck_customer_order_status", "customer_orders", type_="check", schema=schema,
    )
    op.execute(text(
        f'UPDATE "{schema}".customer_orders SET status = \'recibida\' '
        f"WHERE status = 'pendiente'"
    ))
    op.create_check_constraint(
        "ck_customer_order_status", "customer_orders",
        "status IN ('recibida', 'abierta', 'bloqueada', 'pagada', 'cancelada')",
        schema=schema,
    )
