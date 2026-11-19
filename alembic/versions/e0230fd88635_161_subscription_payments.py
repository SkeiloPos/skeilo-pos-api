"""spec 161: tabla shared.subscription_payments

Revision ID: e0230fd88635
Revises: b1c9f4e7a3d2
Create Date: 2026-11-17 00:00:00.000000

Pagos de la mensualidad de un tenant con link de Wompi (spec 161, data-model.md § 2). Tabla
**nueva** del esquema `shared`: no modifica ni reinterpreta ningún dato existente y su reversión
consiste en eliminarla (Principio VIII). Sin backfill: vacía al desplegar es el estado correcto.

Vive en `shared` porque el webhook de Wompi llega sin tenant y debe localizar el pago por su
`gateway_reference`. Las claves foráneas a `tenants`/`plans` no llevan `ON DELETE` (borrar un
tenant o un plan con pagos queda restringido: es historial financiero); la del usuario que pidió
el link es `SET NULL` porque la spec 159 permite borrar usuarios y el pago debe sobrevivir.

Dos índices únicos PARCIALES: un solo pago `pending` por tenant (FR-016) y una transacción de
Wompi no puede ligarse a dos pagos (research.md Decisión 3).

Alcance de esta revisión: solo la tabla compartida. La columna `notification_events.audience_role`
(esquema de cada tenant, Historia 5) irá en una revisión posterior.

El padre es `b1c9f4e7a3d2`, la última migración de la spec 162 (documentos legales), que cuelga de
`8d09a031cba3`: así la cadena queda lineal (`8d09… → spec 162 → esta`) y hay una sola cabeza. Los dos archivos de
fusión que había sin versionar en el árbol de trabajo (`f2d755c229e8…`, `b0ba035937ba…`) no se usan como padre a
propósito: quien clone el repositorio no los tiene, y eran fusiones sin ninguna instrucción.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e0230fd88635'
down_revision: Union[str, Sequence[str], None] = 'b1c9f4e7a3d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    # En modo offline (`alembic upgrade a:b --sql`) no hay conexión que consultar: se genera el DDL completo,
    # para que quien despliegue pueda revisarlo antes de aplicarlo.
    if context.is_offline_mode():
        return False
    return op.get_bind().execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = :s AND table_name = :t"
        ),
        {"s": schema, "t": table},
    ).scalar() is not None


def upgrade() -> None:
    if _has_table("shared", "subscription_payments"):
        return

    op.create_table(
        "subscription_payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ciclo_facturacion", sa.String(length=10), server_default="mensual", nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("gateway", sa.String(length=20), server_default="wompi", nullable=False),
        sa.Column("gateway_reference", sa.String(length=64), nullable=False),
        sa.Column("gateway_transaction_id", sa.String(length=64), nullable=True),
        sa.Column("payment_link_url", sa.String(length=2048), nullable=False),
        sa.Column("payment_link_expires_at", sa.DateTime(), nullable=False),
        sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("period_start", sa.DateTime(), nullable=True),
        sa.Column("period_end", sa.DateTime(), nullable=True),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk__subscription_payments")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["shared.tenants.id"],
            name=op.f("fk__subscription_payments__tenant_id__tenants"),
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["shared.plans.id"],
            name=op.f("fk__subscription_payments__plan_id__plans"),
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["shared.users.id"],
            name=op.f("fk__subscription_payments__requested_by_user_id__users"),
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("gateway_reference", name=op.f("uq__subscription_payments__gateway_reference")),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired', 'voided')",
            name=op.f("ck__subscription_payments__status"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck__subscription_payments__amount_positive")),
        schema="shared",
    )
    # Historial paginado por tenant (`ORDER BY created_at DESC`: el B-tree también se recorre al revés).
    op.create_index(
        "ix__subscription_payments__tenant_id__created_at",
        "subscription_payments",
        ["tenant_id", "created_at"],
        unique=False,
        schema="shared",
    )
    # A lo sumo un pago `pending` por tenant (FR-016).
    op.create_index(
        "ux__subscription_payments__tenant_id__pending",
        "subscription_payments",
        ["tenant_id"],
        unique=True,
        schema="shared",
        postgresql_where=sa.text("status = 'pending'"),
    )
    # Una transacción de Wompi no puede aplicarse a dos pagos; los NULL no cuentan.
    op.create_index(
        "ux__subscription_payments__gateway_transaction_id",
        "subscription_payments",
        ["gateway_transaction_id"],
        unique=True,
        schema="shared",
        postgresql_where=sa.text("gateway_transaction_id IS NOT NULL"),
    )


def downgrade() -> None:
    # Los índices se eliminan con la tabla. Exportar `subscription_payments` ANTES de revertir si ya
    # tiene filas (quickstart.md § 9): son el historial de cobros.
    # En offline no se puede consultar: se asume que la tabla existe, para que el SQL de reversión sea completo.
    if context.is_offline_mode() or _has_table("shared", "subscription_payments"):
        op.drop_table("subscription_payments", schema="shared")
