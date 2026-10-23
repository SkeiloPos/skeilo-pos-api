from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import (
    String, Integer, Numeric, ForeignKey, DateTime, func, CheckConstraint, Index, text,
)
from sqlalchemy.orm import mapped_column, Mapped, relationship
from typing import Optional, TYPE_CHECKING
from decimal import Decimal
from datetime import datetime, timezone
from enum import Enum

if TYPE_CHECKING:
    from .customer_order import CustomerOrder
    from .payment import PaymentMethod


class RejectionReason(str, Enum):
    """Conjunto cerrado de motivos de rechazo de un pago (spec 132, FR-015).
    Reutilizado tanto por este modelo (vía el `CheckConstraint` de la
    migración `_132_rejection_reason_enum`) como por
    `app/api/v1/orders/schemas.py` (`PaymentAttemptRejectIn.reason`)."""
    CLIENTE_NO_MUESTRA_APP = "cliente_no_muestra_app"
    MONTO_NO_COINCIDE = "monto_no_coincide"
    TRANSFERENCIA_NO_COMPLETADA = "transferencia_no_completada"
    CLIENTE_SE_NIEGA_A_MOSTRAR = "cliente_se_niega_a_mostrar"
    COMPROBANTE_SOSPECHOSO = "comprobante_sospechoso"


class OrderPaymentAttempt(UUIDPrimaryKeyMixin, Base):
    """Intento de pago de una orden de mesa (spec 024). Una orden puede tener
    varios a lo largo del tiempo (p.ej. tras un rechazo, FR-015/FR-016), pero
    a lo sumo uno `pendiente` a la vez (índice único parcial abajo, FR-015a).

    `confirm_order` (`app/api/v1/orders/checkout.py`) exige que exista uno
    `confirmado` antes de dejar avanzar la orden a comanda (FR-017) — es la
    única puerta de entrada a comanda, no se agrega ningún endpoint de
    transición de estado genérico (research.md spec 024, Decisión 5)."""

    __tablename__ = "order_payment_attempts"

    order_id: Mapped[UUID] = mapped_column(
        ForeignKey("customer_orders.id"), nullable=False, index=True
    )
    order: Mapped["CustomerOrder"] = relationship(back_populates="payment_attempts")

    payment_method_id: Mapped[UUID] = mapped_column(
        ForeignKey("payment_methods.id"), nullable=False, index=True
    )
    # `foreign_keys=` explícito: desde que existe `transfer_method_id` (spec 130) hay dos FK
    # de esta tabla a `payment_methods`, y SQLAlchemy ya no puede inferir sola cuál usa cada
    # relación.
    payment_method: Mapped["PaymentMethod"] = relationship(foreign_keys=[payment_method_id])

    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default="pendiente")

    # Solo efectivo (FR-009/FR-010). `None` en intentos de transferencia.
    amount_received: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    change_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)

    # Solo transferencia: un único archivo por intento (research.md Decisión 3).
    receipt_file_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Pago mixto (spec 130, FR-005 a FR-008). `cash_amount IS NOT NULL` es el
    # discriminador de "este intento es mixto" en todo el backend (gate de
    # `_confirm_order_impl`, `confirm_cash_payment_attempt`, `approve_payment_attempt`,
    # bloqueo de salida A-139). `None` en todos los intentos no mixtos existentes y
    # nuevos — sin backfill (Principio VII).
    cash_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    transfer_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    transfer_method_id: Mapped[Optional[UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_methods.id"), nullable=True
    )
    # `foreign_keys=` explícito: distingue esta relación de la ya existente
    # `payment_method` (también FK a `payment_methods`) — sin esto SQLAlchemy no
    # puede decidir sola cuál columna usar para cada relación.
    transfer_method: Mapped[Optional["PaymentMethod"]] = relationship(
        foreign_keys=[transfer_method_id]
    )
    # Confirmación de la porción en efectivo de un pago mixto (research.md Decisión 4).
    # `status == 'confirmado'` sigue siendo la confirmación de la porción por
    # transferencia (comprobante verificado); un intento mixto solo avanza a cocina
    # cuando AMBAS tienen valor.
    cash_confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Visible solo para cajero/back-office (Clarification 3, FR-014) — el
    # router del comensal nunca serializa esta columna.
    rejection_reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Referencia blanda a shared.users.id, mismo patrón que CustomerOrder.user_id.
    resolved_by_user_id: Mapped[Optional[UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Lock optimista (mismo patrón que CustomerOrder.version); el bloqueo real
    # de FR-018/SC-007 lo da el WITH FOR UPDATE sobre status='pendiente' en
    # checkout.py, no esta columna (research.md Decisión 9).
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # `default=` (no solo `server_default=`) a propósito: `current_payment_attempt`
    # (CustomerOrder) desempata "el más reciente" comparando este valor en
    # Python, y `CURRENT_TIMESTAMP` de SQLite solo resuelve al segundo — dos
    # intentos creados en el mismo segundo (reintento rápido tras rechazo,
    # spec 024 US5) empatarían sin este default de mayor resolución.
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        server_default=func.now(),
        nullable=False,
    )

    @property
    def payment_method_name(self) -> str:
        """Atajo de lectura para serializar sin duplicar el nombre en esta
        fila (`PaymentAttemptResponse`/`CurrentPaymentAttemptSummary`,
        `app/api/v1/orders/schemas.py`). Editar el método de pago no cambia
        este valor retroactivamente: siempre lee el nombre *actual* del
        `PaymentMethod` referenciado, no una copia congelada."""
        return self.payment_method.name

    @property
    def is_cash(self) -> bool:
        """Atajo de lectura, mismo motivo que `payment_method_name` — le
        permite al cliente (staff UI) distinguir efectivo de transferencia
        sin una segunda llamada a `payment_methods`."""
        return self.payment_method.is_cash

    __table_args__ = (
        CheckConstraint(
            "status IN ('pendiente', 'confirmado', 'rechazado')",
            name="ck_order_payment_attempt_status",
        ),
        CheckConstraint(
            "status != 'rechazado' OR rejection_reason IS NOT NULL",
            name="ck_order_payment_attempt_rejection_reason",
        ),
        # Spec 132 (FR-015, migración a7f3c9d1b246): conjunto cerrado de
        # motivos de rechazo — coexiste con el CheckConstraint anterior, que
        # solo exige que no sea NULL.
        CheckConstraint(
            "status != 'rechazado' OR rejection_reason IN ("
            "'cliente_no_muestra_app', 'monto_no_coincide', "
            "'transferencia_no_completada', 'cliente_se_niega_a_mostrar', "
            "'comprobante_sospechoso')",
            name="ck_order_payment_attempt_rejection_reason_enum",
        ),
        # A lo sumo un intento 'pendiente' por orden a la vez (FR-015a).
        Index(
            "idx_pending_payment_attempt_per_order",
            "order_id",
            unique=True,
            postgresql_where=text("status = 'pendiente'"),
        ),
        {"schema": "tenant"},
    )
