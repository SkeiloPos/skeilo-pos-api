"""Collapse order/item status model: 5->3 order status, 3->2 payment status, 4->3 item status

Spec 147 (A-151): colapsa el modelo de 5 estados de pedido
(`pendiente, abierta, bloqueada, pagada, cancelada`) a 3
(`pendiente, entregada, cancelada`); el de pago de 3 (`UNPAID, PAID, PARTIAL`)
a 2 (`UNPAID, PAID`); y el de ítem de 4 (`pendiente, en_preparacion, listo,
anulado`) a 3 (`pendiente, entregado, cancelado`).

upgrade(schema), por cada esquema (`tenant_default` + cada tenant de
`shared.tenants`), en este orden exacto (research.md R2):
  1. drop de los tres CHECK afectados (`ck_customer_order_status`,
     `ck_customer_order_payment_status`, `ck_order_item_estado_cocina`).
  2. `UPDATE customer_orders.status`: `'abierta'->'pendiente'`,
     `'bloqueada'->'entregada'`, `'pagada'->'entregada'`.
  3. `UPDATE customer_orders.payment_status`: `'PARTIAL'->'UNPAID'` (toda fila
     que venía de `'pagada'` ya quedó `payment_status='PAID'` por la migración
     `e967f9848848` precedente, eso no se re-deriva aquí).
  4. `UPDATE order_items.estado_cocina`: `'en_preparacion'->'pendiente'`,
     `'listo'->'entregado'`, `'anulado'->'cancelado'`.
  5. Forzado final (FR-023 in fine): para toda orden cuyo `status` quedó
     `'entregada'`, todo ítem suyo cuyo `estado_cocina` no sea `'cancelado'`
     se fuerza a `'entregado'`; para toda orden cuyo `status` quedó
     `'cancelada'`, todo ítem suyo se fuerza a `'cancelado'`. Debe ocurrir
     DESPUÉS de los pasos 2 y 4 porque compara contra el estado final ya
     migrado del pedido (research.md R2).
  6. Recrear los tres CHECK con los conjuntos finales de valores.
  7. Recrear `idx_active_order_per_participant` con el criterio de "orden abierta" de
     FR-016 (`status = 'pendiente' OR (status = 'entregada' AND payment_status =
     'UNPAID')`) en vez del `status NOT IN ('pagada', 'cancelada')` original — debe
     dropearse ANTES de los `UPDATE` de `status` (paso 2) porque, con el predicado viejo
     todavía vigente, un pedido recién migrado a `'entregada'` seguiría contando como
     "activo" bajo esa regla antigua y podría chocar con otro pedido activo preexistente
     del mismo `participant_id` (verificado contra datos reales durante
     `/speckit-implement`: dos pedidos históricos ya `'pagada'`+`'PAID'` del mismo
     participante). Se recrea al final, ya con los datos en su estado final, con el
     criterio corregido (data-model.md, "Corrección post-implementación").

**Limitación de rollback documentada** (data-model.md): el downgrade revierte
agregadamente (`'entregada'->'bloqueada'`, etc.) sin poder distinguir
retroactivamente qué fila vino de dónde, y el forzado del paso 5 no es
reversible de forma exacta (se perdió la distinción entre un ítem que ya
estaba `entregado`/`cancelado` y uno que se forzó). Aceptable solo como
procedimiento de emergencia inmediatamente posterior al despliegue, mismo
criterio que la migración `e967f9848848` precedente.

Revision ID: 6bf8898f6e63
Revises: e967f9848848
Create Date: 2026-11-01 15:32:04.827564

"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

from app.scripts.tenant import for_each_tenant_schema

revision: str = '6bf8898f6e63'
down_revision: Union[str, Sequence[str], None] = 'e967f9848848'
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
    op.drop_constraint(
        "ck_customer_order_payment_status", "customer_orders", type_="check", schema=schema,
    )
    op.drop_constraint(
        "ck_order_item_estado_cocina", "order_items", type_="check", schema=schema,
    )
    op.drop_index(
        "idx_active_order_per_participant", table_name="customer_orders", schema=schema,
    )

    op.execute(text(
        f'UPDATE "{schema}".customer_orders SET status = \'pendiente\' '
        f"WHERE status = 'abierta'"
    ))
    op.execute(text(
        f'UPDATE "{schema}".customer_orders SET status = \'entregada\' '
        f"WHERE status IN ('bloqueada', 'pagada')"
    ))
    op.execute(text(
        f'UPDATE "{schema}".customer_orders SET payment_status = \'UNPAID\' '
        f"WHERE payment_status = 'PARTIAL'"
    ))
    op.execute(text(
        f'UPDATE "{schema}".order_items SET estado_cocina = \'pendiente\' '
        f"WHERE estado_cocina = 'en_preparacion'"
    ))
    op.execute(text(
        f'UPDATE "{schema}".order_items SET estado_cocina = \'entregado\' '
        f"WHERE estado_cocina = 'listo'"
    ))
    op.execute(text(
        f'UPDATE "{schema}".order_items SET estado_cocina = \'cancelado\' '
        f"WHERE estado_cocina = 'anulado'"
    ))

    # Forzado FR-023 in fine: el estado final del pedido prevalece sobre el
    # mapeo individual del ítem (research.md R2) — debe ir después de los
    # UPDATE anteriores, que ya dejaron a `customer_orders.status` en su
    # valor final.
    op.execute(text(
        f'UPDATE "{schema}".order_items oi SET estado_cocina = \'entregado\' '
        f'FROM "{schema}".customer_orders co '
        f"WHERE oi.order_id = co.id AND co.status = 'entregada' "
        f"AND oi.estado_cocina != 'cancelado'"
    ))
    op.execute(text(
        f'UPDATE "{schema}".order_items oi SET estado_cocina = \'cancelado\' '
        f'FROM "{schema}".customer_orders co '
        f"WHERE oi.order_id = co.id AND co.status = 'cancelada'"
    ))

    op.create_check_constraint(
        "ck_customer_order_status", "customer_orders",
        "status IN ('pendiente', 'entregada', 'cancelada')",
        schema=schema,
    )
    op.create_check_constraint(
        "ck_customer_order_payment_status", "customer_orders",
        "payment_status IN ('UNPAID', 'PAID')",
        schema=schema,
    )
    op.create_check_constraint(
        "ck_order_item_estado_cocina", "order_items",
        "estado_cocina IN ('pendiente', 'entregado', 'cancelado')",
        schema=schema,
    )
    op.create_index(
        "idx_active_order_per_participant", "customer_orders", ["participant_id"],
        unique=True,
        postgresql_where=text(
            "status = 'pendiente' OR (status = 'entregada' AND payment_status = 'UNPAID')"
        ),
        schema=schema,
    )
    # El default de columna seguía en 'abierta' (del modelo previo a esta
    # spec); con el CHECK nuevo, cualquier INSERT que confiara en el default
    # violaría la restricción en silencio hasta fallar con un IntegrityError.
    op.alter_column(
        "customer_orders", "status", server_default="pendiente", schema=schema,
    )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "customer_orders"):
        return

    op.drop_constraint(
        "ck_order_item_estado_cocina", "order_items", type_="check", schema=schema,
    )
    op.drop_constraint(
        "ck_customer_order_payment_status", "customer_orders", type_="check", schema=schema,
    )
    op.drop_constraint(
        "ck_customer_order_status", "customer_orders", type_="check", schema=schema,
    )
    op.drop_index(
        "idx_active_order_per_participant", table_name="customer_orders", schema=schema,
    )

    op.execute(text(
        f'UPDATE "{schema}".order_items SET estado_cocina = \'listo\' '
        f"WHERE estado_cocina = 'entregado'"
    ))
    op.execute(text(
        f'UPDATE "{schema}".order_items SET estado_cocina = \'anulado\' '
        f"WHERE estado_cocina = 'cancelado'"
    ))
    op.execute(text(
        f'UPDATE "{schema}".customer_orders SET status = \'bloqueada\' '
        f"WHERE status = 'entregada'"
    ))

    op.create_check_constraint(
        "ck_order_item_estado_cocina", "order_items",
        "estado_cocina IN ('pendiente', 'en_preparacion', 'listo', 'anulado')",
        schema=schema,
    )
    op.create_check_constraint(
        "ck_customer_order_payment_status", "customer_orders",
        "payment_status IN ('UNPAID', 'PAID', 'PARTIAL')",
        schema=schema,
    )
    op.create_check_constraint(
        "ck_customer_order_status", "customer_orders",
        "status IN ('pendiente', 'abierta', 'bloqueada', 'pagada', 'cancelada')",
        schema=schema,
    )
    op.create_index(
        "idx_active_order_per_participant", "customer_orders", ["participant_id"],
        unique=True,
        postgresql_where=text("status NOT IN ('pagada', 'cancelada')"),
        schema=schema,
    )
    op.alter_column(
        "customer_orders", "status", server_default="abierta", schema=schema,
    )
