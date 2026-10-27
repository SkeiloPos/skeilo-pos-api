from app.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import String, Boolean, Numeric, ForeignKey, CheckConstraint, DateTime
from sqlalchemy.orm import mapped_column, Mapped, relationship
from typing import TYPE_CHECKING, Optional
from decimal import Decimal
from datetime import datetime

if TYPE_CHECKING:
    from .unit_measure import UnitMeasure


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

    # spec 140: la "presentación" ahora se elige entre las `UnitMeasure` del mismo grupo
    # que la unidad base del insumo -- une en vivo, no copia su factor (si alguien cambia
    # `factor_to_base` después, esta relación lo refleja automáticamente). Mutuamente
    # excluyente con `presentation`/`conversion_factor` (modo legado, abajo): una relación
    # tiene uno u otro, nunca ambos (ver `effective_presentation`/`effective_conversion_factor`).
    unit_measure_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("unit_measures.id"), nullable=True, index=True
    )
    unit_measure: Mapped[Optional["UnitMeasure"]] = relationship()

    # Modo legado (antes de spec 140, y relaciones creadas por import -- spec 138 -- que
    # todavía no se completaron): presentación en texto libre + factor escrito a mano, sin
    # vincular a ninguna `UnitMeasure`. Nullable porque una relación de import puede no
    # traer ninguno de los dos todavía, y porque ahora conviven con el modo `unit_measure_id`
    # de arriba (una relación nueva/editada ya no los usa).
    presentation: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Cuántas unidades base trae 1 presentación (RN-16/RN-17 enunciado). > 0 si no es NULL,
    # ver ck__supplier_items__conversion_factor. Solo aplica al modo legado -- en modo
    # `unit_measure_id`, el factor real es `unit_measure.factor_to_base`.
    conversion_factor: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 3), nullable=True)

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

    @property
    def effective_presentation(self) -> Optional[str]:
        """spec 140: la etiqueta a mostrar -- el nombre de la `UnitMeasure` vinculada si la
        relación está en ese modo, o el texto legado si no."""
        if self.unit_measure_id is not None:
            return self.unit_measure.name if self.unit_measure is not None else None
        return self.presentation

    @property
    def effective_conversion_factor(self) -> Optional[Decimal]:
        """spec 140: el factor real a usar -- `unit_measure.factor_to_base` en vivo si la
        relación está vinculada a una `UnitMeasure`, o el factor legado si no."""
        if self.unit_measure_id is not None:
            return self.unit_measure.factor_to_base if self.unit_measure is not None else None
        return self.conversion_factor
