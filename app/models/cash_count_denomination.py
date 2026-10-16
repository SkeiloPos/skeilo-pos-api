from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import Integer, Numeric, String, ForeignKey, CheckConstraint, UniqueConstraint
from sqlalchemy.orm import mapped_column, Mapped
from decimal import Decimal


class CashCountDenomination(UUIDPrimaryKeyMixin, Base):
    """Conteo de billetes/monedas al cierre del turno (arqueo por denominación).

    spec 121: es un snapshot (FR-014) -- copia valor, tipo y cantidad al cierre y
    NO tiene clave foránea al catálogo, así que un cambio futuro del catálogo no
    altera turnos cerrados. Solo se guardan líneas con cantidad > 0 (D-3). El
    subtotal no se almacena: se deriva (`denomination * quantity`) al leer."""

    __tablename__ = "cash_count_denominations"

    cash_shift_id: Mapped[UUID] = mapped_column(
        ForeignKey("cash_shifts.id", ondelete="CASCADE"), nullable=False, index=True
    )

    denomination: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    # spec 121 (D-4): 'billete' | 'moneda', tomado del catálogo al cerrar.
    kind: Mapped[str] = mapped_column(String(10), nullable=False)

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("denomination > 0", name="ck_cash_count_denomination_positive"),
        # spec 121 (D-3, FR-015): antes `quantity >= 0`. Mismo nombre, otra
        # expresión; el DELETE de las filas en cero va antes en la migración.
        CheckConstraint("quantity > 0", name="ck_cash_count_quantity_positive"),
        CheckConstraint("kind IN ('billete','moneda')", name="ck_cash_count_kind"),
        UniqueConstraint(
            "cash_shift_id", "denomination",
            name="uq__cash_count_denominations__cash_shift_id__denomination",
        ),
        {"schema": "tenant"},
    )
