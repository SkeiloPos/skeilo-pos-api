from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, UUIDPrimaryKeyMixin


class UnitMeasureFactorChange(UUIDPrimaryKeyMixin, Base):
    """Historial de cambios de `UnitMeasure.factor_to_base` (spec 134, FR-011).

    Solo lectura desde la API; se inserta exclusivamente desde el handler de
    `PATCH /unit-measures/{id}` cuando el body trae `factor_to_base` distinto del
    actual (exige rol ADMIN, FR-011a).
    """

    __tablename__ = "unit_measure_factor_changes"

    unit_measure_id: Mapped[UUID] = mapped_column(
        ForeignKey("unit_measures.id"), nullable=False, index=True
    )

    old_factor: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)

    new_factor: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)

    # Referencia blanda a shared.users.id (sin FK cross-schema), mismo patrón que
    # Purchase.user_id.
    changed_by: Mapped[UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    changed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = ({"schema": "tenant"},)
