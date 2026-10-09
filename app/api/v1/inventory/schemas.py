from enum import Enum
from uuid import UUID
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.core.timezone import UtcDatetime


class InventoryItemType(str, Enum):
    RAW_MATERIAL = "raw_material"
    PACKAGED = "packaged"


# ---------- Insumos ----------
class InventoryItemCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, examples=["Leche entera", "Fresa"])
    unit_measure_id: UUID
    type: InventoryItemType = InventoryItemType.RAW_MATERIAL
    current_stock: Decimal = Field(0, ge=0, max_digits=12, decimal_places=3)
    min_stock: Decimal = Field(0, ge=0, max_digits=12, decimal_places=3)
    unit_cost: Decimal = Field(0, ge=0, max_digits=12, decimal_places=2)


class InventoryItemUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    unit_measure_id: UUID | None = None
    type: InventoryItemType | None = None
    min_stock: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=3)
    unit_cost: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    active: bool | None = None


class InventoryItemResponse(BaseModel):
    id: UUID
    name: str
    unit_measure_id: UUID
    type: InventoryItemType
    current_stock: Decimal
    min_stock: Decimal
    unit_cost: Decimal
    active: bool
    created_at: UtcDatetime
    updated_at: UtcDatetime | None = None
    # Nombre del proveedor preferido activo (spec 106, RN-16); None si el insumo no tiene
    # ninguna relación proveedor-insumo preferida. Se asigna fuera del ORM (ver
    # supplier_items.attach_preferred_supplier_names) -- no es una columna mapeada.
    preferred_supplier_name: str | None = None

    model_config = ConfigDict(from_attributes=True)


# ---------- Ajustes ----------
class AdjustmentIn(BaseModel):
    signed_delta: Decimal = Field(
        ..., max_digits=12, decimal_places=3,
        description="Delta con signo: positivo suma, negativo resta.",
        examples=["-2.5"],
    )
    reason: str | None = Field(None, max_length=255, examples=["Merma por derrame"])


class MovementResponse(BaseModel):
    id: UUID
    inventory_item_id: UUID
    type: str
    quantity: Decimal
    reason: str | None = None
    reference_type: str | None = None
    reference_id: UUID | None = None
    moved_at: UtcDatetime

    model_config = ConfigDict(from_attributes=True)


# ---------- Proveedores ----------
class SupplierCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    tax_id: str | None = Field(None, max_length=50)
    phone: str | None = Field(None, max_length=50)
    email: str | None = Field(None, max_length=255)


class SupplierUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    tax_id: str | None = Field(None, max_length=50)
    phone: str | None = Field(None, max_length=50)
    email: str | None = Field(None, max_length=255)
    active: bool | None = None


class SupplierResponse(BaseModel):
    id: UUID
    name: str
    tax_id: str | None = None
    phone: str | None = None
    email: str | None = None
    active: bool
    is_system: bool

    model_config = ConfigDict(from_attributes=True)


# ---------- Relación proveedor-insumo (spec 106) ----------
class SupplierItemCreate(BaseModel):
    """Alta/reactivación de una relación proveedor-insumo (FR-001/FR-004/FR-005).

    Las dos rutas de creación (desde el insumo o desde el proveedor) fijan uno de los dos
    ids por la URL; el que falte aquí se completa con el de la ruta.
    """
    supplier_id: UUID | None = None
    inventory_item_id: UUID | None = None
    presentation: str = Field(..., min_length=1, max_length=100, examples=["Bolsa x 500 g"])
    conversion_factor: Decimal = Field(..., max_digits=12, decimal_places=3)
    last_cost: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)


class SupplierItemUpdate(BaseModel):
    presentation: str | None = Field(None, min_length=1, max_length=100)
    conversion_factor: Decimal | None = Field(None, max_digits=12, decimal_places=3)
    last_cost: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    active: bool | None = None
    preferred: bool | None = None


class SupplierItemResponse(BaseModel):
    id: UUID
    supplier_id: UUID
    supplier_name: str
    inventory_item_id: UUID
    inventory_item_name: str
    presentation: str
    conversion_factor: Decimal
    last_cost: Decimal | None = None
    last_cost_at: UtcDatetime | None = None
    active: bool
    preferred: bool
    equivalence: str | None = None
    was_preferred: bool = False


class SupplierItemConflict(BaseModel):
    """Cuerpo del 409 de FR-004: la relación activa con la que colisionó el alta."""
    existing_id: UUID
    detail: str


# ---------- Compras ----------
class PurchaseItemIn(BaseModel):
    inventory_item_id: UUID
    quantity: Decimal = Field(..., gt=0, max_digits=12, decimal_places=3)
    unit_cost: Decimal = Field(0, ge=0, max_digits=12, decimal_places=2)


class PurchaseCreate(BaseModel):
    supplier_id: UUID | None = None
    invoice_number: str | None = Field(None, max_length=100)
    items: list[PurchaseItemIn] = Field(..., min_length=1)


class PurchaseItemResponse(BaseModel):
    id: UUID
    inventory_item_id: UUID
    quantity: Decimal
    received_quantity: Decimal
    unit_cost: Decimal

    model_config = ConfigDict(from_attributes=True)


class PurchaseResponse(BaseModel):
    id: UUID
    supplier_id: UUID | None = None
    invoice_number: str | None = None
    status: str
    total: Decimal
    purchased_at: UtcDatetime
    items: list[PurchaseItemResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


# ---------- Recepción parcial (RF-022) ----------
class PurchaseReceiveItemIn(BaseModel):
    purchase_item_id: UUID
    quantity: Decimal = Field(..., gt=0, max_digits=12, decimal_places=3)


class PurchaseReceiveIn(BaseModel):
    items: list[PurchaseReceiveItemIn] = Field(..., min_length=1)


class LowStockResponse(BaseModel):
    id: UUID
    name: str
    current_stock: Decimal
    min_stock: Decimal

    model_config = ConfigDict(from_attributes=True)
