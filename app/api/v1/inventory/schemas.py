from enum import Enum
from uuid import UUID
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.timezone import UtcDatetime


class InventoryItemType(str, Enum):
    RAW_MATERIAL = "raw_material"
    PACKAGED = "packaged"


# ---------- Plantilla única de export/import (spec 109, research.md D6) ----------
# Una sola definición de cabeceras, en orden -- `export.py` e `import_.py` la importan
# en vez de repetirla cada uno por su lado (FR-001). Ver data-model.md para el origen/
# tipo esperado de cada columna.
INVENTORY_TEMPLATE_HEADERS = [
    "Código",
    "Nombre",
    "Tipo",
    "Unidad",
    "Grupo",
    "Stock",
    "Mínimo",
    "Costo",
    "Proveedor preferido",
    "Unidad de compra",
    "Factor de conversión (compra)",
]


# ---------- Insumos ----------
class InventoryItemCreate(BaseModel):
    code: str | None = Field(
        None, min_length=1, max_length=20,
        description="Si no se indica, se asigna uno automáticamente (spec 109).",
    )
    name: str = Field(..., min_length=1, max_length=255, examples=["Leche entera", "Fresa"])
    unit_measure_id: UUID
    type: InventoryItemType = InventoryItemType.RAW_MATERIAL
    current_stock: Decimal = Field(0, ge=0, max_digits=12, decimal_places=3)
    min_stock: Decimal = Field(0, ge=0, max_digits=12, decimal_places=3)
    unit_cost: Decimal = Field(0, ge=0, max_digits=12, decimal_places=2)


class InventoryItemUpdate(BaseModel):
    code: str | None = Field(None, min_length=1, max_length=20)
    name: str | None = Field(None, min_length=1, max_length=255)
    unit_measure_id: UUID | None = None
    type: InventoryItemType | None = None
    min_stock: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=3)
    unit_cost: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    active: bool | None = None


class InventoryItemResponse(BaseModel):
    id: UUID
    code: str
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

    spec 140: la "presentación" de compra ahora se elige entre las `UnitMeasure` del mismo
    grupo que la unidad base del insumo (`unit_measure_id`) -- ya no se captura como texto
    libre + factor escrito a mano. Se conserva el modo legado (`presentation` +
    `conversion_factor`) solo para no romper integraciones existentes; el formulario de la
    app ya no lo usa para crear relaciones nuevas."""
    supplier_id: UUID | None = None
    inventory_item_id: UUID | None = None
    unit_measure_id: UUID | None = None
    presentation: str | None = Field(None, min_length=1, max_length=100, examples=["Bolsa x 500 g"])
    conversion_factor: Decimal | None = Field(None, max_digits=12, decimal_places=3)
    last_cost: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)

    @model_validator(mode="after")
    def _exactly_one_mode(self):
        has_unit = self.unit_measure_id is not None
        has_legacy = self.presentation is not None or self.conversion_factor is not None
        if has_unit and has_legacy:
            raise ValueError("indica unit_measure_id o presentation/conversion_factor, no ambos")
        if not has_unit:
            if self.presentation is None:
                raise ValueError("presentation es obligatorio cuando no se indica unit_measure_id")
            if self.conversion_factor is None:
                raise ValueError("conversion_factor es obligatorio cuando no se indica unit_measure_id")
        return self


class SupplierItemUpdate(BaseModel):
    unit_measure_id: UUID | None = None
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
    # spec 140: cuando la relación está vinculada a una `UnitMeasure`, estos dos campos
    # reflejan sus valores EN VIVO (nombre/factor_to_base), no un texto/número guardado
    # aparte -- ver `SupplierItem.effective_presentation`/`effective_conversion_factor`.
    unit_measure_id: UUID | None = None
    # Opcionales (spec 138): una relación creada por import de inventario desde la columna
    # Proveedor preferido no trae presentación/factor -- el admin la completa después.
    presentation: str | None = None
    conversion_factor: Decimal | None = None
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
    """Una línea viene en unidad base (`quantity`/`unit_cost`, como siempre), en una
    presentación guardada (`supplier_item_id` + `presentation_quantity` +
    `presentation_unit_cost`, spec 106/107) o en otra unidad del mismo grupo de medida del
    insumo (`unit_measure_id`, spec 134) -- nunca una mezcla de estos tres modos
    (FR-001/FR-002/FR-007, FR-015)."""
    inventory_item_id: UUID
    quantity: Decimal | None = Field(None, gt=0, max_digits=12, decimal_places=3)
    unit_cost: Decimal = Field(0, ge=0, max_digits=12, decimal_places=2)
    supplier_item_id: UUID | None = None
    presentation_quantity: Decimal | None = Field(None, gt=0, max_digits=12, decimal_places=3)
    presentation_unit_cost: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    # Unidad en la que se capturaron quantity/unit_cost de esta línea (spec 134), cuando no
    # es la unidad base del insumo ni una presentación de proveedor.
    unit_measure_id: UUID | None = None

    @model_validator(mode="after")
    def _exactly_one_mode(self):
        if self.supplier_item_id is not None:
            if self.presentation_quantity is None:
                raise ValueError(
                    "presentation_quantity es obligatorio cuando se indica supplier_item_id"
                )
            if self.quantity is not None:
                raise ValueError("no se puede indicar quantity junto con supplier_item_id")
            if self.unit_measure_id is not None:
                raise ValueError("no se puede indicar unit_measure_id junto con supplier_item_id")
        elif self.unit_measure_id is not None:
            if self.quantity is None:
                raise ValueError("quantity es obligatorio cuando se indica unit_measure_id")
            if self.unit_cost <= 0:
                raise ValueError("La cantidad debe ser mayor que cero")
        elif self.quantity is None:
            raise ValueError("quantity es obligatorio cuando no se indica supplier_item_id")
        return self


class PurchaseCreate(BaseModel):
    supplier_id: UUID | None = None
    invoice_number: str | None = Field(None, max_length=100)
    items: list[PurchaseItemIn] = Field(..., min_length=1)


class PurchaseOrderItemIn(BaseModel):
    """Como `PurchaseItemIn`, pero sin su `model_validator`: cuando el padre
    (`PurchaseOrderCreate`) trae `allow_incomplete=True` (spec 140, FR-013, "Guardar
    borrador"), una línea puede llegar sin `quantity`, sin `unit_cost` o sin ningún modo
    (`supplier_item_id`/`unit_measure_id`) resuelto todavía -- la validación de "exactamente
    un modo" se reaplica en `PurchaseOrderCreate` solo cuando `allow_incomplete=False`."""
    inventory_item_id: UUID
    quantity: Decimal | None = Field(None, gt=0, max_digits=12, decimal_places=3)
    unit_cost: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    supplier_item_id: UUID | None = None
    presentation_quantity: Decimal | None = Field(None, gt=0, max_digits=12, decimal_places=3)
    presentation_unit_cost: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    unit_measure_id: UUID | None = None


class PurchaseOrderCreate(BaseModel):
    """`POST /purchases/order` (spec 140 MODIFICADO): `allow_incomplete` es aditivo y
    retrocompatible -- ausente o `false` conserva exactamente el comportamiento de
    `PurchaseCreate` (User Story 2, orden de compra pendiente); `true` permite guardar una
    captura parcial, incluso sin ninguna línea (User Story 3, "Guardar borrador")."""
    supplier_id: UUID | None = None
    invoice_number: str | None = Field(None, max_length=100)
    items: list[PurchaseOrderItemIn] = Field(default_factory=list)
    allow_incomplete: bool = False

    @model_validator(mode="after")
    def _validate_unless_incomplete(self):
        if self.allow_incomplete:
            return self
        if len(self.items) < 1:
            raise ValueError("items debe tener al menos 1 elemento")
        for it in self.items:
            if it.supplier_item_id is not None:
                if it.presentation_quantity is None:
                    raise ValueError(
                        "presentation_quantity es obligatorio cuando se indica supplier_item_id"
                    )
                if it.quantity is not None:
                    raise ValueError("no se puede indicar quantity junto con supplier_item_id")
                if it.unit_measure_id is not None:
                    raise ValueError("no se puede indicar unit_measure_id junto con supplier_item_id")
            elif it.unit_measure_id is not None:
                if it.quantity is None:
                    raise ValueError("quantity es obligatorio cuando se indica unit_measure_id")
                if it.unit_cost is None or it.unit_cost <= 0:
                    raise ValueError("La cantidad debe ser mayor que cero")
            elif it.quantity is None:
                raise ValueError("quantity es obligatorio cuando no se indica supplier_item_id")
        return self


class PurchaseItemResponse(BaseModel):
    id: UUID
    inventory_item_id: UUID
    quantity: Decimal
    received_quantity: Decimal
    unit_cost: Decimal
    supplier_item_id: UUID | None = None
    presentation_quantity: Decimal | None = None
    presentation_label: str | None = None
    # Denormalizado desde SupplierItem.conversion_factor (no es columna de PurchaseItem) --
    # seguro de exponer porque FR-014 (spec 106) ya bloquea editar ese factor una vez usado en
    # una compra, y esta fila es ese mismo uso. Lo llena
    # supplier_items.attach_conversion_factors (ver router.py).
    conversion_factor: Decimal | None = None
    unit_measure_id: UUID
    conversion_factor_applied: Decimal
    quantity_base: Decimal
    cost_base: Decimal

    model_config = ConfigDict(from_attributes=True)


class PurchaseResponse(BaseModel):
    id: UUID
    supplier_id: UUID | None = None
    invoice_number: str | None = None
    status: str
    is_incomplete: bool = False
    total: Decimal
    purchased_at: UtcDatetime
    voided_at: UtcDatetime | None = None
    voided_by: UUID | None = None
    items: list[PurchaseItemResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


# ---------- Recepción parcial (RF-022) ----------
class PurchaseReceiveItemIn(BaseModel):
    """spec 107: si el ítem que se recibe se pactó en una presentación, la recepción viene en
    `presentation_quantity` (cuántas presentaciones se reciben ahora); si no, en `quantity`
    directo en unidad base, como siempre. El factor para convertir se lee del `PurchaseItem` ya
    creado, nunca de este request (research.md D4) -- por eso aquí no hay ningún factor."""
    purchase_item_id: UUID
    quantity: Decimal | None = Field(None, gt=0, max_digits=12, decimal_places=3)
    presentation_quantity: Decimal | None = Field(None, gt=0, max_digits=12, decimal_places=3)

    @model_validator(mode="after")
    def _exactly_one_mode(self):
        if (self.quantity is None) == (self.presentation_quantity is None):
            raise ValueError("indica exactamente uno de quantity o presentation_quantity")
        return self


class PurchaseReceiveIn(BaseModel):
    items: list[PurchaseReceiveItemIn] = Field(..., min_length=1)


class LowStockResponse(BaseModel):
    id: UUID
    name: str
    current_stock: Decimal
    min_stock: Decimal

    model_config = ConfigDict(from_attributes=True)


# ---------- Importación masiva (spec 109) ----------
class InventoryImportJobResponse(BaseModel):
    id: UUID
    status: str
    file_name: str
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    total_rows: int
    created_count: int
    updated_count: int
    failed_count: int
    # No es una columna mapeada -- lo fija el router al construir la respuesta
    # (igual criterio que `preferred_supplier_name`), para que el frontend
    # decida si ofrece la descarga de errores sin tener que mirar el conteo.
    has_errors: bool

    model_config = ConfigDict(from_attributes=True)


class ImportStructureError(BaseModel):
    """Cuerpo del 422 de FR-005: problemas de estructura, uno por línea."""
    problems: list[str]
