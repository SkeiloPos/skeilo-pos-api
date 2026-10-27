from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import String, Numeric, ForeignKey, DateTime, Boolean, func, CheckConstraint
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

    # Anulación de una compra confirmada (spec 134, US3, FR-012/FR-013). NULL = no
    # anulada; ortogonal a `status`, que sigue gobernando solo la recepción.
    voided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Referencia blanda a shared.users.id (sin FK cross-schema), mismo patrón que user_id.
    voided_by: Mapped[Optional[UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    # spec 140: solo tiene sentido junto a `status='draft'`. `true` = borrador incompleto
    # guardado sin exigir líneas completas (User Story 3); `false` (default) preserva el
    # significado histórico de "orden de compra pendiente" (User Story 2) para toda fila
    # existente antes de esta columna.
    is_incomplete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
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

    # Relación proveedor-insumo usada en esta línea (spec 106, poblada desde spec 107): cuando
    # no es NULL, `quantity`/`unit_cost` de abajo son valores derivados de
    # `presentation_quantity`/`presentation_unit_cost` (ver service.py), no capturados
    # directamente. También es lo que hace que el factor de esa relación quede bloqueado
    # (FR-014, spec 106).
    supplier_item_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("supplier_items.id"), nullable=True, index=True
    )

    # Cuántas presentaciones capturó el administrador (spec 107, FR-002); NULL si la línea se
    # compró directo en la unidad base del insumo, como siempre.
    presentation_quantity: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 3), nullable=True
    )

    # Copia del texto de `SupplierItem.presentation` en el momento de la compra -- no un join en
    # vivo: esa presentación se puede editar después (spec 106) y una compra ya registrada no
    # puede cambiar de aspecto retroactivamente (research.md D1, Principio VII).
    presentation_label: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # "Capturado originalmente" (FR-009), en la unidad de `unit_measure_id` de esta misma
    # fila -- no necesariamente la unidad base del insumo. Lo que alimenta stock/costo es
    # `quantity_base`/`cost_base` (abajo).
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)

    # Cantidad ya recibida (RF-022); el resto queda pendiente. Misma unidad que `quantity`.
    received_quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 3), nullable=False, default=0, server_default="0"
    )

    unit_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=0, server_default="0"
    )

    # Unidad en la que se capturó esta línea (spec 134). Para una línea con
    # `supplier_item_id` (presentación de proveedor, spec 106/107, sin cambios) se guarda la
    # unidad base del insumo -- esa conversión la sigue gobernando
    # `SupplierItem.conversion_factor`, no esta columna.
    unit_measure_id: Mapped[UUID] = mapped_column(
        ForeignKey("unit_measures.id"), nullable=False, index=True
    )

    # Factor resuelto en servidor al confirmar (FR-008); congelado para siempre (FR-010) --
    # un cambio posterior de `UnitMeasure.factor_to_base` no recalcula ninguna compra ya
    # confirmada.
    conversion_factor_applied: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=1, server_default="1"
    )

    # Cantidad/costo ya convertidos a la unidad base del insumo -- lo que alimenta
    # `record_movement()` y `InventoryItem.unit_cost` (FR-006/FR-007). Para las dos rutas
    # que ya existían (directa y `supplier_item_id`), coinciden con `quantity`/`unit_cost`.
    quantity_base: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)

    cost_base: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_purchase_item_qty_positive"),
        CheckConstraint(
            "received_quantity >= 0 AND received_quantity <= quantity",
            name="ck_purchase_item_received_range",
        ),
        {"schema": "tenant"},
    )
