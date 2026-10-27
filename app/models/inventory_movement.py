from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import String, Numeric, ForeignKey, CheckConstraint, DateTime, func, Index
from sqlalchemy.orm import mapped_column, Mapped
from typing import Optional
from decimal import Decimal
from datetime import datetime


class InventoryMovement(UUIDPrimaryKeyMixin, Base):
    """Kardex: toda variación de stock queda registrada aquí.

    `reference_type`/`reference_id` apuntan (soft) al origen ('sale', 'purchase',
    'adjustment'). `user_id` es una referencia blanda a shared.users.id (sin FK
    cross-schema).
    """

    __tablename__ = "inventory_movements"

    inventory_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("inventory_items.id"), nullable=False, index=True
    )

    type: Mapped[str] = mapped_column(String(20), nullable=False)

    # Numeric(18,6) (spec 134, research.md D4): consistente con InventoryItem.current_stock,
    # que alimenta con conversiones de unidad que pueden tener más de 3 decimales.
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)

    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    reference_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    reference_id: Mapped[Optional[UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Referencia blanda a shared.users.id (sin FK cross-schema).
    user_id: Mapped[Optional[UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    moved_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "type IN ('in', 'out', 'adjustment')",
            name="ck_inventory_movement_type",
        ),
        CheckConstraint("quantity > 0", name="ck_inventory_movement_qty_positive"),
        Index("idx_invmov_ref", "reference_type", "reference_id"),
        {"schema": "tenant"},
    )
