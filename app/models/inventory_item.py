from app.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import String, Boolean, Numeric, ForeignKey, CheckConstraint
from sqlalchemy.orm import mapped_column, Mapped, relationship
from typing import Optional, TYPE_CHECKING
from decimal import Decimal

if TYPE_CHECKING:
    from .unit_measure import UnitMeasure


class InventoryItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Insumo de inventario con stock único (sin lotes ni vencimiento).

    `type` distingue materia prima (se consume por receta) de un producto
    empacado que se vende/descuenta directamente.
    """

    __tablename__ = "inventory_items"

    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)

    # Identificador de negocio para el upsert de la importación masiva (spec 109).
    # Generado automáticamente (secuencial con cero a la izquierda) cuando no se
    # especifica; editable por el administrador.
    code: Mapped[str] = mapped_column(String(20), nullable=False, unique=True, index=True)

    unit_measure_id: Mapped[UUID] = mapped_column(
        ForeignKey("unit_measures.id"), nullable=False, index=True
    )
    unit_measure: Mapped["UnitMeasure"] = relationship()

    type: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="raw_material"
    )

    # Numeric(18,6) (spec 134, research.md D4): la cantidad/costo en unidad base puede
    # tener más de 3/2 decimales significativos tras convertir desde otra unidad (p. ej.
    # LB→g, factor 453.592). `min_stock` no se ensancha -- lo captura el administrador
    # directamente en unidad base, sin conversión de por medio.
    current_stock: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=0, server_default="0"
    )

    min_stock: Mapped[Decimal] = mapped_column(
        Numeric(12, 3), nullable=False, default=0, server_default="0"
    )

    unit_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=0, server_default="0"
    )

    active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        CheckConstraint(
            "type IN ('raw_material', 'packaged')",
            name="ck_inventory_item_type",
        ),
        {"schema": "tenant"},
    )
