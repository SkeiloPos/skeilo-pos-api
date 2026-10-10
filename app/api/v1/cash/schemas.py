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
    opening_amount: Decimal = Field(0, ge=0, max_digits=12, decimal_places=2)


class DenominationIn(BaseModel):
    denomination: Decimal = Field(..., gt=0, max_digits=12, decimal_places=2)
    quantity: int = Field(..., ge=0)


class ShiftClose(BaseModel):
    counted_amount: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
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
