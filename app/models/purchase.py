from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import String, Numeric, ForeignKey, DateTime, func, CheckConstraint
from sqlalchemy.orm import mapped_column, Mapped, relationship
from typing import Optional, List
from decimal import Decimal
from datetime import datetime


class Purchase(UUIDPrimaryKeyMixin, Base):
    """Compra a proveedor. `status` gobierna la recepción (RF-022):
    - `draft`: orden creada, sin dar alta de stock.
    - `partial`: recibida parcialmente.
    - `received`: recibida por completo (el alta directa vía POST /purchases nace así)."""

    __tablename__ = "purchases"

    supplier_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("suppliers.id"), nullable=True, index=True
    )

    # Referencia blanda a shared.users.id (sin FK cross-schema).
    user_id: Mapped[Optional[UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    invoice_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    status: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default="received"
    )

    total: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=0, server_default="0"
    )

    purchased_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    items: Mapped[List["PurchaseItem"]] = relationship(
        back_populates="purchase", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'partial', 'received')", name="ck_purchase_status"
        ),
        {"schema": "tenant"},
    )


class PurchaseItem(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "purchase_items"

    purchase_id: Mapped[UUID] = mapped_column(
        ForeignKey("purchases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purchase: Mapped["Purchase"] = relationship(back_populates="items")

    inventory_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("inventory_items.id"), nullable=False, index=True
    )

    # Relación proveedor-insumo usada en esta línea (spec 106). Nace NULL y hoy ningún flujo
    # de compras la puebla todavía -- la llena HU-05 al implementar el upsert automático
    # (RN-15). Existe para que el bloqueo de edición del factor (FR-014) tenga algo que
    # inspeccionar (research.md D6).
    supplier_item_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("supplier_items.id"), nullable=True, index=True
    )

    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)

    # Cantidad ya recibida (RF-022); el resto queda pendiente.
    received_quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 3), nullable=False, default=0, server_default="0"
    )

    unit_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=0, server_default="0"
    )

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_purchase_item_qty_positive"),
        CheckConstraint(
            "received_quantity >= 0 AND received_quantity <= quantity",
            name="ck_purchase_item_received_range",
        ),
        {"schema": "tenant"},
    )
