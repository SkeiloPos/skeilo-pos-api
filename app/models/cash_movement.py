from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import String, Numeric, ForeignKey, DateTime, func, CheckConstraint
from sqlalchemy.orm import mapped_column, Mapped, relationship
from typing import Optional, TYPE_CHECKING
from decimal import Decimal
from datetime import datetime

if TYPE_CHECKING:
    from .payment import PaymentMethod


class CashMovement(UUIDPrimaryKeyMixin, Base):
    """Movimiento manual de efectivo del turno, distinto a una venta:
    ingreso (aporte al cajón), egreso (gasto operativo) o retiro (salida a
    banco/caja fuerte). Las ventas NO se guardan aquí: se derivan de Payment."""

    __tablename__ = "cash_movements"

    cash_shift_id: Mapped[UUID] = mapped_column(
        ForeignKey("cash_shifts.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # ingreso (+), egreso (−), retiro (−) sobre el efectivo esperado.
    kind: Mapped[str] = mapped_column(String(20), nullable=False)

    # Categoría del movimiento (p. ej. "Compra de hielo", "Consignación").
    category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # spec 135: método de esta línea -- egreso/retiro siempre el método
    # `is_cash=True` del tenant (forzado por el endpoint, FR-012); ingreso
    # cualquier método activo (FR-004). `NOT NULL` tras backfill (migración),
    # nullable aquí solo porque SQLAlchemy no distingue "nullable en Python"
    # de "nullable en la base de datos" -- la columna real sí es NOT NULL.
    payment_method_id: Mapped[UUID] = mapped_column(
        ForeignKey("payment_methods.id"), nullable=False, index=True
    )
    payment_method: Mapped["PaymentMethod"] = relationship()

    @property
    def payment_method_name(self) -> str:
        """Nombre legible del método de esta línea (dashboard/reporte de
        caja): `CashMovementResponse` lo expone vía `from_attributes`."""
        return self.payment_method.name

    # spec 135 (FR-011): enlaza las 1-2 filas de un mismo evento de ingreso
    # mixto (US3). `NULL` para egreso/retiro y para un ingreso de 1 sola línea.
    group_id: Mapped[Optional[UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Referencia blanda a shared.users.id (sin FK cross-schema) + snapshot.
    user_id: Mapped[Optional[UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    user_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('ingreso', 'egreso', 'retiro')", name="ck_cash_movement_kind"
        ),
        CheckConstraint("amount > 0", name="ck_cash_movement_amount_positive"),
        {"schema": "tenant"},
    )
