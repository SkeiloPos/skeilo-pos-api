from decimal import Decimal
from typing import TYPE_CHECKING

from app.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import mapped_column, Mapped, relationship
from sqlalchemy import String, Boolean, Numeric, ForeignKey

if TYPE_CHECKING:
    from .unit_measure_group import UnitMeasureGroup


class UnitMeasure(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Unidad de medida simple (litro, gramo, ml, unidad, bola).

    `factor_to_base` (spec 134) activa `app/core/units.py::convert()`: lleva la cantidad a
    la base del grupo de la unidad (kg→1000 g).

    `group_id`/`is_base` (spec 137) formalizan el grupo/dimensión de la unidad como
    `UnitMeasureGroup`, la única fuente de verdad (spec 139 eliminó la columna `dimension`,
    que hasta entonces era un espejo de `group.name`).
    """

    __tablename__ = "unit_measures"

    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    abbreviation: Mapped[str] = mapped_column(
        String(50), nullable=False, unique=True, index=True
    )

    factor_to_base: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=1, server_default="1"
    )

    active: Mapped[bool] = mapped_column(Boolean, default=True)

    group_id: Mapped[UUID] = mapped_column(
        ForeignKey("unit_measure_groups.id"), nullable=False, index=True
    )
    group: Mapped["UnitMeasureGroup"] = relationship(back_populates="units")

    is_base: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    __table_args__ = ({"schema": "tenant"},)
