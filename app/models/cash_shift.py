from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import (
    String, Numeric, ForeignKey, DateTime, func, CheckConstraint, Index, text,
)
from sqlalchemy.orm import mapped_column, Mapped
from typing import Optional
from decimal import Decimal
from datetime import datetime


class CashShift(UUIDPrimaryKeyMixin, Base):
    """Turno de caja (arqueo). `user_id`/`user_name` referencian (soft) al
    cajero de shared.users; el nombre es un snapshot histórico."""

    __tablename__ = "cash_shifts"

    cash_register_id: Mapped[UUID] = mapped_column(
        ForeignKey("cash_registers.id"), nullable=False, index=True
    )

    # Referencia blanda a shared.users.id (sin FK cross-schema) + snapshot.
    user_id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    opening_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=0, server_default="0"
    )

    opened_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    counted_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)

    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default="open")

    # Observación del arqueo; obligatoria si difference != 0 (se valida en el router).
    close_note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        CheckConstraint("opening_amount > 0", name="ck_cash_shift_opening_positive"),
        CheckConstraint(
            "status = 'open' OR closed_at IS NOT NULL",
            name="ck_cash_shift_closed_has_timestamp",
        ),
        CheckConstraint("status IN ('open', 'closed')", name="ck_cash_shift_status"),
        # Sólo un turno abierto por caja a la vez.
        Index(
            "idx_open_shift_per_register",
            "cash_register_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        {"schema": "tenant"},
    )
