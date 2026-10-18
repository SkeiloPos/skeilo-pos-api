from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy import (
    String, Integer, Numeric, ForeignKey, DateTime, func, CheckConstraint, text,
)
from sqlalchemy.orm import mapped_column, Mapped, relationship
from typing import Optional, List, TYPE_CHECKING
from decimal import Decimal
from datetime import datetime

if TYPE_CHECKING:
    from .payment import Payment
    from .invoice import Invoice
    from .dining_table import DiningTable


class Sale(UUIDPrimaryKeyMixin, Base):
    """Venta emitida. Ligada al turno de caja para la conciliación. `user_id`/
    `user_name` = cajero (referencia blanda a shared.users + snapshot)."""

    __tablename__ = "sales"

    # Comensal cobrado. Poblado en el cierre con billing_mode='split' (una venta
    # por comensal); null en 'unified' y en mostrador.
    participant_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("session_participants.id"), nullable=True
    )

    # Sesión de mesa cobrada (cualquiera de los dos billing_mode).
    table_session_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("table_sessions.id"), nullable=True, index=True
    )

    dining_table_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("dining_tables.id"), nullable=True
    )

    # Orden de mesa que originó esta venta (cobro de mesa, Fase 7). Null en
    # ventas de mostrador directas. Gancho para la facturación por order (Fase 8).
    customer_order_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("customer_orders.id"), nullable=True, index=True
    )

    cash_shift_id: Mapped[UUID] = mapped_column(
        ForeignKey("cash_shifts.id"), nullable=False, index=True
    )

    # Cajero: referencia blanda a shared.users.id (sin FK cross-schema) + snapshot.
    user_id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    customer_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, server_default="0")
    discount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, server_default="0")
    tax: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, server_default="0")
    tip: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, server_default="0")
    # Spec 056 — valor del domicilio de la CustomerOrder asociada, copiado aquí
    # al facturar (incluido en `total`, ver sales/builder.py). Null/0 en toda
    # venta que no venga de una orden DELIVERY.
    delivery_fee: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, server_default="0")

    # Promoción aplicada automáticamente (RF-012); su descuento está incluido en `discount`.
    # `SET NULL`, igual que los `combo_id` de cart/order/sale_items: sin él,
    # borrar una promoción ya vendida lanzaba IntegrityError -> 500.
    #
    # spec 063 (FR-021, A-64): `promotion_id` SE CONSERVA y se sigue poblando
    # cuando **una sola** promoción explica todo el descuento; con dos o más
    # queda `NULL` como hoy, pero `applied_promotions` (abajo) lo cubre — cierra A-29.
    promotion_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("promotions.id", ondelete="SET NULL"), nullable=True
    )

    # spec 063 (FR-021, A-64): snapshot inmutable al emitir —
    # `[{"promotion_id", "name", "amount"}]`, una entrada por promoción que
    # descontó alguna línea de este cobro, con su monto AGREGADO (no por línea).
    # Mismo patrón que `sale_items.options`. Nace `'[]'` para todo lo existente
    # (no retroactivo).
    applied_promotions: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # spec 073 (FR-011a, A-70): el instante que EFECTIVAMENTE se uso para evaluar
    # la vigencia temporal de las promociones de esta venta -- la salida de
    # `promotion_evaluation_instant(...)` al facturar (research.md D3/D7). No es
    # necesariamente igual al `promotion_evaluated_at` del pedido: si la cuenta
    # agrupo varias rondas, manda el instante del pedido mas antiguo (FR-012a).
    # `NULL` en toda venta emitida antes de esta spec y en cualquier venta sin un
    # pedido congelado detras (venta de mostrador directa) -- sin backfill,
    # Principio VII / FR-011. Mismo criterio de `DateTime(timezone=True)` (aware
    # UTC) que `customer_orders.promotion_evaluated_at` -- data-model.md.
    promotion_evaluated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Efectivo recibido y cambio entregado (RF-029). Nullable: ventas antiguas
    # o sin desglose no lo registran.
    paid_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    change_given: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)

    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default="issued")

    # spec 123: distingue la venta del cobro normal del pedido ('original') de
    # la que liquida un saldo pendiente generado por adiciones agregadas
    # después de facturar ('complementario'). Toda venta anterior a esta spec
    # nace/queda 'original' por el default -- es exactamente lo que era, la
    # única clase de venta que existía hasta ahora (Principio VII).
    kind: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="original"
    )

    sold_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    items: Mapped[List["SaleItem"]] = relationship(
        back_populates="sale", cascade="all, delete-orphan"
    )

    payments: Mapped[List["Payment"]] = relationship(
        back_populates="sale", cascade="all, delete-orphan"
    )

    # Solo lectura: la factura la emite `invoices.service.issue_for_sale` dentro
    # de la transacción del cobro, y la mesa la fija el cierre de la sesión. Se
    # exponen para poder reimprimir el ticket completo desde el detalle de venta.
    invoice: Mapped[Optional["Invoice"]] = relationship(uselist=False, viewonly=True)

    dining_table: Mapped[Optional["DiningTable"]] = relationship(viewonly=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('issued', 'paid', 'void')", name="ck_sale_status"
        ),
        CheckConstraint(
            "kind IN ('original', 'complementario')", name="ck_sale_kind"
        ),
        {"schema": "tenant"},
    )


class SaleItem(UUIDPrimaryKeyMixin, Base):
    """Línea de venta. `description`/`options` son copias inmutables al momento
    de la venta (historial)."""

    __tablename__ = "sale_items"

    sale_id: Mapped[UUID] = mapped_column(
        ForeignKey("sales.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sale: Mapped["Sale"] = relationship(back_populates="items")

    product_variant_id: Mapped[UUID] = mapped_column(
        ForeignKey("product_variants.id"), nullable=False
    )

    description: Mapped[str] = mapped_column(String(500), nullable=False)

    options: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    # Promoción de combo que generó esta línea (selección explícita). Varias
    # líneas comparten el mismo combo_id: son los componentes de un mismo combo.
    combo_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("promotions.id", ondelete="SET NULL"), nullable=True, index=True
    )

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_sale_item_quantity_positive"),
        {"schema": "tenant"},
    )
