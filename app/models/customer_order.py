from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy import (
    String, Integer, Boolean, Numeric, ForeignKey, DateTime, func, CheckConstraint, Index, text,
)
from sqlalchemy.orm import mapped_column, Mapped, relationship
from typing import Optional, List
from decimal import Decimal
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .order_item import OrderItem
    from .order_cancel_log import OrderCancelLog
    from .order_payment_attempt import OrderPaymentAttempt


class CustomerOrder(UUIDPrimaryKeyMixin, Base):
    """Pedido. Varios por mesa y por comensal a lo largo de una `table_session`.

    `status` es el ciclo del pedido, **no** el de cocina (ese vive por ítem, en
    `order_items.estado_cocina`):

        recibida → abierta → bloqueada → pagada
                 ↘  cancelada (terminal, desde cualquier estado no terminal)

    - `recibida`: el comensal la envió desde el QR pero **aún no descuenta stock**;
    - `abierta`: confirmada por staff — aquí y solo aquí se descontó el inventario;
    - `bloqueada`: congelada para cobro (lock optimista por `version`);
    - `pagada` / `cancelada`: terminales.

    `user_id` es null cuando el pedido lo envió el cliente por QR."""

    __tablename__ = "customer_orders"

    # Sesión de mesa a la que pertenece el pedido (null en mostrador).
    table_session_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("table_sessions.id"), nullable=True, index=True
    )

    # Comensal que lo envió (null si lo creó el staff).
    participant_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("session_participants.id"), nullable=True, index=True
    )

    dining_table_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("dining_tables.id"), nullable=True
    )

    customer_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Canal de origen estandarizado (spec 055): POS/QR_MENU/WHATSAPP/API.
    channel: Mapped[str] = mapped_column(String(10), nullable=False, server_default="POS")

    # Cómo se atiende el pedido (spec 055): DINE_IN/TAKEAWAY/DELIVERY. Nulable:
    # los pedidos de antes de esta mejora sin mesa quedan sin clasificar
    # (data-model.md, backfill) — todo pedido nuevo lo trae siempre asignado.
    order_type: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)

    # Técnico, no expuesto en ningún schema de API (research.md D2): distingue
    # las órdenes que abre/reusa `orders.consolidation.get_or_create_open_order`
    # (mesero) de las que arma `orders.service.create_order` (staff, POS) —
    # antes de esta spec esa distinción vivía en el propio `channel`
    # ('waiter' vs 'counter'); fusionarlos en un único valor `POS` de canal
    # exige preservarla aquí para no reabrir por accidente una comanda ya
    # cobrada (`checkout_and_send` deja las órdenes pagadas en `'abierta'`).
    is_consolidation_order: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    # Spec 056 — solo diligenciados cuando order_type == 'DELIVERY'. Sin default
    # de ningún tipo (ni de columna ni de aplicación): un pedido a domicilio
    # incompleto no es un pedido válido, no un pedido con "$0"/"" implícito
    # (spec.md FR-006, Edge Cases).
    delivery_address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    delivery_phone: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    delivery_fee: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)

    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default="abierta")

    # spec 063 (FR-021, A-64): hoy `CustomerOrder` no tiene ningún campo de
    # descuento. Se fija en el cobro (`pay_order`, `_close_unified`, `_close_split`)
    # con el mismo agregado que la `Sale`. Nace `0` para todo pedido existente.
    discount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default="0"
    )
    # spec 063 (FR-021, A-64): mismo contenido que `sales.applied_promotions`,
    # fijado en el cobro. Nace `'[]'`.
    applied_promotions: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # spec 073 (FR-008, A-70): instante contra el que se evalua la vigencia
    # TEMPORAL (fechas, dia, franja) de las promociones de este pedido. Se fija
    # una sola vez, al crear el pedido (`orders/service.py::create_order` y
    # `cart/service.py`), y nunca se actualiza -- ni al agregar/anular items,
    # ni al mover de mesa. Nullable sin default: las filas anteriores a esta
    # spec quedan en NULL, sin backfill -- el helper `promotion_evaluation_instant`
    # trata NULL como "usa la hora del cobro" (FR-012), comportamiento identico
    # al actual para todo lo historico.
    #
    # `DateTime(timezone=True)` -- aware UTC, a diferencia de `created_at` (naive):
    # el valor se pasa a `auto_discount` -> `local_now()` (`promotions/service.py`),
    # que interpreta un naive como hora LOCAL del tenant; un UTC naive desplazaria
    # la evaluacion de la franja por el offset (-5 en Colombia). `created_at` no
    # sufre esto porque nunca se pasa a `local_now()` (research.md D1, data-model.md).
    promotion_evaluated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Lock optimista para la transición abierta→bloqueada del cobro (Fase 7).
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # Referencia blanda a shared.users.id (null si el cliente pidió por QR).
    user_id: Mapped[Optional[UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Unión de mesas (RF-053): órdenes con el mismo grupo se cobran juntas.
    merged_group_id: Mapped[Optional[UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )

    notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # spec 087 (FR-006, A-86): numeración estable de pedidos de mesa,
    # asignada una sola vez al crear el pedido (`orders/service.py::create_order`
    # y `cart/service.py::submit_cart`) vía `cash/service.py::resolve_dine_in_table_order`.
    # Ambas NULL para TAKEAWAY/DELIVERY y para todo pedido creado antes de esta
    # migración -- nunca se recalculan ni se desplazan después.
    cash_shift_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("cash_shifts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    table_order_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # spec 111 (FR-011, RN4): generado por el cliente una sola vez por intento de "Crear
    # pedido"; reenviado sin cambios si ese mismo intento se reintenta por una falla de red.
    # Nullable y sin backfill (Principio VII): todo pedido ya existente queda en NULL.
    client_request_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    items: Mapped[List["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )

    cancel_logs: Mapped[List["OrderCancelLog"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )

    # Historial completo de intentos de pago (spec 024, FR-016) — nunca se
    # borran, solo lectura desde aquí.
    payment_attempts: Mapped[List["OrderPaymentAttempt"]] = relationship(
        back_populates="order"
    )

    @property
    def current_payment_attempt(self) -> Optional["OrderPaymentAttempt"]:
        """El intento de pago más reciente (o `None` si no hay ninguno) —
        base de `OrderResponse.current_payment_attempt` (spec 024). No es lo
        mismo que "el confirmado": mientras no exista uno con
        `status == 'confirmado'`, la orden sigue pendiente de pago para el
        comensal, sin importar cuántos intentos rechazados haya antes."""
        if not self.payment_attempts:
            return None
        return max(self.payment_attempts, key=lambda a: a.created_at)

    __table_args__ = (
        CheckConstraint(
            "channel IN ('POS', 'QR_MENU', 'WHATSAPP', 'API')", name="ck_customer_order_channel"
        ),
        CheckConstraint(
            "order_type IS NULL OR order_type IN ('DINE_IN', 'TAKEAWAY', 'DELIVERY')",
            name="ck_customer_order_order_type",
        ),
        CheckConstraint(
            "status IN ('recibida', 'abierta', 'bloqueada', 'pagada', 'cancelada')",
            name="ck_customer_order_status",
        ),
        CheckConstraint(
            "delivery_fee IS NULL OR delivery_fee >= 0",
            name="ck_customer_order_delivery_fee_non_negative",
        ),
        # spec 063 (FR-021, `063a`).
        CheckConstraint("discount >= 0", name="ck_customer_order_discount_non_negative"),
        Index("idx_customer_orders_channel", "channel"),
        Index("idx_customer_orders_order_type", "order_type"),
        # Ya NO hay índice único de "una orden abierta por mesa": la mesa puede
        # tener varios pedidos simultáneos (uno por comensal, o varias rondas del
        # mismo). La agrupación para cobrar la da `table_session_id`.
        #
        # A lo sumo una orden activa por comensal (spec 025, FR-013) — mismo
        # predicado que `_NON_TERMINAL_ORDER_STATUSES`
        # (`app/api/v1/cart/service.py`). Postgres no considera dos NULL
        # iguales: las órdenes de mostrador/mesero (participant_id NULL) no
        # se ven afectadas.
        Index(
            "idx_active_order_per_participant",
            "participant_id",
            unique=True,
            postgresql_where=text("status NOT IN ('pagada', 'cancelada')"),
        ),
        # spec 087 (FR-006): unicidad del número de pedido dentro del turno de
        # caja, solo para las filas que sí lo tienen (DINE_IN).
        Index(
            "uq_customer_orders_shift_table_order_number",
            "cash_shift_id",
            "table_order_number",
            unique=True,
            postgresql_where=text("table_order_number IS NOT NULL"),
        ),
        # spec 111 (FR-011): único parcial -- permite múltiples filas en NULL (todo pedido
        # histórico, y cualquier cliente que aún no lo envíe) sin violar la unicidad de los
        # valores reales.
        Index(
            "uq_customer_orders_client_request_id",
            "client_request_id",
            unique=True,
            postgresql_where=text("client_request_id IS NOT NULL"),
        ),
        {"schema": "tenant"},
    )
