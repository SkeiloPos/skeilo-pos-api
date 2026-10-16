from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import (
    Integer, Numeric, String, ForeignKey, DateTime, Index, func, CheckConstraint,
    UniqueConstraint,
)
from sqlalchemy.orm import mapped_column, Mapped
from decimal import Decimal
from datetime import datetime
from typing import Optional

_CASH_SHIFT_FK = "cash_shifts.id"


class ShiftSalesReport(UUIDPrimaryKeyMixin, Base):
    """Cabecera del snapshot inmutable del reporte operativo de ventas de un
    turno (spec 119, FR-013/research.md D2). Se escribe una sola vez, dentro
    de la misma transacción de `close_shift` — nunca se actualiza después,
    ni siquiera si una venta del turno se anula (SC-003).

    Solo lleva los totales; el desglose COMPLETO (no top-N, FR-017) vive en
    las filas de `ShiftSalesReportProduct` y `ShiftSalesReportCategory`
    (US6, research.md D9). La existencia de esta fila es el discriminador de
    "este turno tiene snapshot"."""

    __tablename__ = "shift_sales_reports"

    cash_shift_id: Mapped[UUID] = mapped_column(
        ForeignKey(_CASH_SHIFT_FK, ondelete="CASCADE"), nullable=False
    )

    total_sales: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    order_count: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_ticket: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

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


class ShiftSalesReportProduct(UUIDPrimaryKeyMixin, Base):
    """Una fila por variante de producto con unidades vendidas en el turno
    (spec 119 US6, data-model.md §2.2, research.md D9). `product_variant_id`
    **sin FK**: un producto eliminado después sigue figurando en el reporte
    histórico por su nombre congelado."""

    __tablename__ = "shift_sales_report_products"

    cash_shift_id: Mapped[UUID] = mapped_column(
        ForeignKey(_CASH_SHIFT_FK, ondelete="CASCADE"), nullable=False
    )
    product_variant_id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    revenue: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)

    __table_args__ = (
        # Su índice, con `cash_shift_id` primero, también respalda la lectura
        # por turno: no hace falta otro.
        UniqueConstraint(
            "cash_shift_id", "product_variant_id",
            name="ux__shift_sales_report_products__shift_variant",
        ),
        # Nombres cortos: la convención de nombres antepone `ck__<tabla>__` y
        # PostgreSQL corta los identificadores a 63 caracteres (SQLAlchemy los
        # trunca con un hash). Cortos, no hay truncamiento y el modelo y la
        # Rev. B de Alembic generan exactamente el mismo nombre.
        CheckConstraint("units >= 0", name="units_nonneg"),
        CheckConstraint("revenue >= 0", name="revenue_nonneg"),
        {"schema": "tenant"},
    )


class ShiftSalesReportCategory(UUIDPrimaryKeyMixin, Base):
    """Una fila por categoría con ventas en el turno (spec 119 US6,
    data-model.md §2.3). `category_id` **sin FK** y nulable: `NULL` es "Sin
    categoría". No hay `UNIQUE (cash_shift_id, category_id)`: en PostgreSQL
    `NULL` no cuenta como igual, y la unicidad por turno la garantiza la
    propia agrupación al escribir."""

    __tablename__ = "shift_sales_report_categories"

    cash_shift_id: Mapped[UUID] = mapped_column(
        ForeignKey(_CASH_SHIFT_FK, ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[Optional[UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    category_name: Mapped[str] = mapped_column(String(255), nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    revenue: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)

    __table_args__ = (
        Index("ix__shift_sales_report_categories__cash_shift_id", "cash_shift_id"),
        CheckConstraint("units >= 0", name="units_nonneg"),
        CheckConstraint("revenue >= 0", name="revenue_nonneg"),
        {"schema": "tenant"},
    )
