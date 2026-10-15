from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy import (
    Integer, Numeric, ForeignKey, DateTime, func, CheckConstraint, UniqueConstraint,
)
from sqlalchemy.orm import mapped_column, Mapped
from decimal import Decimal
from datetime import datetime


class ShiftSalesReport(UUIDPrimaryKeyMixin, Base):
    """Snapshot inmutable del reporte operativo de ventas de un turno (spec
    119, FR-013/research.md D2). Se escribe una sola vez, dentro de la misma
    transacción de `close_shift`, con el desglose COMPLETO (no top-N) de
    productos/categorías — nunca se actualiza después, ni siquiera si una
    venta del turno se anula (SC-003)."""

    __tablename__ = "shift_sales_reports"

    cash_shift_id: Mapped[UUID] = mapped_column(
        ForeignKey("cash_shifts.id", ondelete="CASCADE"), nullable=False
    )

    total_sales: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    order_count: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_ticket: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    # Lista completa `[{product_variant_id, name, units, revenue}]` (FR-017).
    products_breakdown: Mapped[list] = mapped_column(JSONB, nullable=False)
    # Lista completa `[{category_id, category_name, units, revenue}]`.
    categories_breakdown: Mapped[list] = mapped_column(JSONB, nullable=False)

    computed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("cash_shift_id", name="ux__shift_sales_reports__cash_shift_id"),
        CheckConstraint("total_sales >= 0", name="ck_shift_sales_report_total_sales"),
        CheckConstraint("order_count >= 0", name="ck_shift_sales_report_order_count"),
        CheckConstraint("avg_ticket >= 0", name="ck_shift_sales_report_avg_ticket"),
        {"schema": "tenant"},
    )
