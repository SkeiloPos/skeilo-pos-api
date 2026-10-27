from typing import List, TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import mapped_column, Mapped, relationship

from app.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from .unit_measure import UnitMeasure


class UnitMeasureGroup(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Grupo de unidades de medida con una unidad base explícita (spec 137).

    Única fuente de verdad del grupo/dimensión de una unidad (spec 139 eliminó la
    columna `UnitMeasure.dimension`, que era un texto libre espejo de `name`).
    `app/core/units.py::convert()` valida compatibilidad comparando `group_id`.
    """

    __tablename__ = "unit_measure_groups"

    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    units: Mapped[List["UnitMeasure"]] = relationship(back_populates="group")

    __table_args__ = ({"schema": "tenant"},)
