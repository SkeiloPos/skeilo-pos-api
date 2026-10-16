from datetime import date
from enum import Enum
from uuid import UUID
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.core.timezone import UtcDatetime


class CashMovementKind(str, Enum):
    INGRESO = "ingreso"
    EGRESO = "egreso"
    RETIRO = "retiro"


# ---------- Cajas ----------
class RegisterCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, examples=["Caja principal"])


class RegisterResponse(BaseModel):
    id: UUID
    name: str
    active: bool

    model_config = ConfigDict(from_attributes=True)


# ---------- Turnos ----------
class ShiftOpen(BaseModel):
    cash_register_id: UUID
    opening_amount: Decimal = Field(0, max_digits=12, decimal_places=2)


class DenominationIn(BaseModel):
    # spec 121: `extra="allow"` (no "forbid") para que `reject_client_totals` pueda
    # rechazar `subtotal`/`total` con un 422 en español. Con "forbid" FastAPI
    # devolvería `detail` como lista y en inglés (research.md R4).
    model_config = ConfigDict(extra="allow")

    denomination: Decimal = Field(..., gt=0, max_digits=12, decimal_places=2)
    quantity: int = Field(..., ge=0)


class ShiftClose(BaseModel):
    # spec 121 (D-1, A-130): el efectivo contado ya NO lo envía el cliente; el
    # servidor lo calcula del desglose. Un `counted_amount` (u otro total) con valor
    # llega aquí como campo extra y lo rechaza `denominations.reject_client_totals`.
    model_config = ConfigDict(extra="allow")

    denominations: list[DenominationIn] = Field(default_factory=list)
    # Obligatoria si difference != 0 (se valida en el router).
    close_note: str | None = Field(None, max_length=500)


class ShiftResponse(BaseModel):
    id: UUID
    cash_register_id: UUID
    user_id: UUID
    user_name: str | None = None
    opening_amount: Decimal
    opened_at: UtcDatetime
    closed_at: UtcDatetime | None = None
    counted_amount: Decimal | None = None
    status: str
    close_note: str | None = None
    # spec 121 (D-5, FR-029): quien CERRÓ el turno (`user_*` es quien lo abrió).
    # Nulos en turnos abiertos e históricos.
    closed_by_user_id: UUID | None = None
    closed_by_user_name: str | None = None

    model_config = ConfigDict(from_attributes=True)


# ---------- Movimientos de efectivo ----------
class CashMovementIn(BaseModel):
    kind: CashMovementKind
    amount: Decimal = Field(..., gt=0, max_digits=12, decimal_places=2)
    category: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(None, max_length=255)


class CashMovementResponse(BaseModel):
    id: UUID
    cash_shift_id: UUID
    kind: str
    amount: Decimal
    category: str | None = None
    description: str | None = None
    user_name: str | None = None
    occurred_at: UtcDatetime

    model_config = ConfigDict(from_attributes=True)


# ---------- Arqueo / Reconciliación ----------
class SalesByMethod(BaseModel):
    method_id: UUID
    method_name: str
    method_type: str
    # Venta neta del método (lo facturado). Para `method_type="cash"`, desde
    # spec 108 ya viene con el cambio entregado descontado — coincide con
    # `ReconciliationResponse.ventas_efectivo`.
    total: Decimal
    count: int


class ReconciliationResponse(BaseModel):
    cash_shift_id: UUID
    status: str
    opening_amount: Decimal
    # Ventas del turno desglosadas por clasificación del método de pago.
    ventas_efectivo: Decimal       # ÚNICA que suma a expected
    ventas_tarjeta: Decimal        # informativa
    ventas_transferencia: Decimal  # informativa
    # Cambio entregado del cajón. Dato informativo (spec 108): ni `ventas_efectivo`
    # ni las filas de `sales_by_method` necesitan sumarlo de vuelta, ambos ya son
    # netos.
    cambio_entregado: Decimal
    # Una fila por método de pago activo, aunque no haya vendido nada en el turno.
    sales_by_method: list[SalesByMethod]
    # Movimientos manuales por kind.
    ingresos: Decimal
    egresos: Decimal
    retiros: Decimal
    expected: Decimal
    counted_amount: Decimal | None = None
    difference: Decimal | None = None
    # DEPRECADO: alias de ventas_efectivo por compatibilidad con el frontend.
    cash_sales: Decimal


# ---------- Reporte de cierre ----------
class ShiftReportResponse(BaseModel):
    shift: ShiftResponse
    reconciliation: ReconciliationResponse
    movements: list[CashMovementResponse]
    denominations: list[DenominationIn]
    close_note: str | None = None


# ---------- Histórico de turnos / cierres ----------
class ShiftSummaryResponse(BaseModel):
    """Fila del histórico: datos del turno + arqueo (esperado/diferencia calculados)."""
    id: UUID
    cash_register_id: UUID
    register_name: str
    user_name: str | None = None
    opening_amount: Decimal
    counted_amount: Decimal | None = None
    opened_at: UtcDatetime
    closed_at: UtcDatetime | None = None
    status: str
    close_note: str | None = None
    expected: Decimal
    difference: Decimal | None = None


# ---------- Reporte operativo por turno (spec 119) ----------
class ProductBreakdownRow(BaseModel):
    product_variant_id: UUID
    name: str
    units: int
    revenue: Decimal


class CategoryBreakdownRow(BaseModel):
    category_id: UUID | None = None
    category_name: str
    units: int
    revenue: Decimal


class ShiftSalesReportResponse(BaseModel):
    shift_id: UUID
    cash_register_id: UUID
    cash_register_name: str
    status: str
    # `true` si `status="open"`, o si `status="closed"` sin snapshot todavía
    # (turno cerrado antes de que esta funcionalidad existiera, research.md D8).
    is_live: bool
    opened_at: UtcDatetime
    closed_at: UtcDatetime | None = None
    total_sales: Decimal
    order_count: int
    avg_ticket: Decimal
    top_products: list[ProductBreakdownRow]
    categories: list[CategoryBreakdownRow]
    # Solo relevante/no-null cuando status="open" (FR-012).
    active_orders_count: int | None = None


class PeriodSalesReportResponse(BaseModel):
    period: str  # "shift" | "current_shift" | "day" | "week" | "month" | "range"
    date_from: date
    date_to: date
    cash_register_id: UUID | None = None
    shift_ids: list[UUID]
    is_live: bool
    total_sales: Decimal
    order_count: int
    avg_ticket: Decimal
    top_products: list[ProductBreakdownRow]
    categories: list[CategoryBreakdownRow]


# ---------- Pedidos activos / bloqueo de cierre (spec 119) ----------
class ActiveOrderSummary(BaseModel):
    id: UUID
    order_type: str | None = None
    status: str
    dining_table_id: UUID | None = None
    table_number: int | None = None
    customer_name: str | None = None
    created_at: UtcDatetime

    model_config = ConfigDict(from_attributes=True)
