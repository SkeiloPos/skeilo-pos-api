from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: Estados del pago (spec 161, data-model.md § 2.1). `voided` es el único terminal; `approved` solo puede pasar a él.
SUBSCRIPTION_PAYMENT_STATUSES = ("pending", "approved", "rejected", "expired", "voided")


class SubscriptionPayment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Un intento de cobro de la mensualidad de un tenant, pagado con un link de Wompi (spec 161, FR-036).

    Vive en `shared` y no en el esquema del tenant porque el webhook de Wompi llega sin tenant (sin
    `x-tenant-host`): debe poder localizar el pago por su `gateway_reference` y, desde él, al tenant.

    Todas las fechas son UTC *naive*, como el resto del esquema compartido (`utc_now_naive`). `period_start`,
    `period_end` y `paid_at` quedan en `NULL` hasta que el pago se aprueba (FR-022).

    No tiene relación con `invoices`: un pago de suscripción no es una factura de venta y nunca la toca
    (FR-040, Principio VII).
    """

    __tablename__ = "subscription_payments"

    # Sin `ON DELETE`: borrar un tenant o un plan con pagos queda restringido (historial financiero).
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("shared.tenants.id"), nullable=False)
    plan_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("shared.plans.id"), nullable=False)

    ciclo_facturacion: Mapped[str] = mapped_column(
        String(10), nullable=False, default="mensual", server_default="mensual"
    )
    # Copia de `plan.precio_mensual` en el momento de pedir el link (COP). No se recalcula después.
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")

    gateway: Mapped[str] = mapped_column(String(20), nullable=False, default="wompi", server_default="wompi")
    # `SKP-<uuid del pago en hex>`; única. Es lo que Wompi devuelve en `transaction.reference`.
    gateway_reference: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Id de la transacción en Wompi; se llena con el evento. Una transacción no puede ligarse a dos pagos.
    gateway_transaction_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    payment_link_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    payment_link_expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # `SET NULL`: la spec 159 permite borrar usuarios y el pago debe sobrevivir.
    requested_by_user_id: Mapped[Optional[UUID]] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("shared.users.id", ondelete="SET NULL"), nullable=True
    )

    period_start: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # Con la convención de `Base.metadata`, `name="status"` queda `ck__subscription_payments__status`.
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired', 'voided')", name="status"
        ),
        CheckConstraint("amount > 0", name="amount_positive"),
        # Historial paginado por tenant (`ORDER BY created_at DESC`: el B-tree también se recorre al revés).
        Index("ix__subscription_payments__tenant_id__created_at", "tenant_id", "created_at"),
        # A lo sumo un pago `pending` por tenant (FR-016): red de seguridad ante dos solicitudes simultáneas.
        Index(
            "ux__subscription_payments__tenant_id__pending",
            "tenant_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        # Una transacción de Wompi no puede aplicarse a dos pagos distintos; los NULL no cuentan.
        Index(
            "ux__subscription_payments__gateway_transaction_id",
            "gateway_transaction_id",
            unique=True,
            postgresql_where=text("gateway_transaction_id IS NOT NULL"),
            sqlite_where=text("gateway_transaction_id IS NOT NULL"),
        ),
        {"schema": "shared"},
    )
