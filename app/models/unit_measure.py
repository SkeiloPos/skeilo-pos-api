from decimal import Decimal

from app.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin
from sqlalchemy.orm import mapped_column, Mapped
from sqlalchemy import String, Boolean, Numeric


class UnitMeasure(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Unidad de medida simple (litro, gramo, ml, unidad, bola).

    `dimension`/`factor_to_base` (spec 134) activan `app/core/units.py::convert()`: dos
    unidades son convertibles entre sí si `dimension` coincide exactamente, y
    `factor_to_base` lleva la cantidad a la base de esa dimensión (kg→1000 g).
    """

    __tablename__ = "unit_measures"

    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    abbreviation: Mapped[str] = mapped_column(
        String(50), nullable=False, unique=True, index=True
    )

    # Sin default explícito de negocio (research.md D1: sin catálogo cerrado, el admin lo
    # escribe igual que `name`/`abbreviation`) -- el `server_default` solo cubre el mismo
    # valor que usó el backfill de la migración 134, para no romper scripts/importaciones
    # preexistentes que crean una UnitMeasure sin pasar por el endpoint nuevo.
    dimension: Mapped[str] = mapped_column(
        String(100), nullable=False, default="general", server_default="general"
    )

    factor_to_base: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=1, server_default="1"
    )

    active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = ({"schema": "tenant"},)
