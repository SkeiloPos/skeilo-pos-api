from app.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import String, Boolean, Numeric, ForeignKey, CheckConstraint, DateTime
from sqlalchemy.orm import mapped_column, Mapped
from typing import Optional
from decimal import Decimal
from datetime import datetime


class SupplierItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Relación N:M proveedor-insumo (spec 106). La presentación y el factor de conversión
    viven aquí, no en el insumo (D2 del enunciado) -- el insumo solo guarda su unidad base.
    """

    __tablename__ = "supplier_items"

    supplier_id: Mapped[UUID] = mapped_column(
        ForeignKey("suppliers.id"), nullable=False, index=True
    )

    inventory_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("inventory_items.id"), nullable=False, index=True
    )

    presentation: Mapped[str] = mapped_column(String(100), nullable=False)

    # Cuántas unidades base trae 1 presentación (RN-16/RN-17 enunciado). > 0, ver
    # ck__supplier_items__conversion_factor.
    conversion_factor: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)

    last_cost: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)

    last_cost_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # A lo sumo una preferida activa por insumo -- forzado por el índice parcial
    # ux__supplier_items__preferred (no DEFERRABLE, ver migración y
    # app/api/v1/inventory/supplier_items.py::set_preferred).
    preferred: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    __table_args__ = (
        CheckConstraint("conversion_factor > 0", name="ck__supplier_items__conversion_factor"),
        {"schema": "tenant"},
    )
