"""Generación del `.xlsx` de inventario (spec 086, extendida por spec 109 a la
plantilla única de export/import, `INVENTORY_TEMPLATE_HEADERS`). El workbook se
arma completo en memoria antes de responder — ver research.md §2 de la spec 086
para el porqué (garantiza que un error a mitad de la generación nunca deja un
archivo corrupto/incompleto en la respuesta)."""
import io
from decimal import Decimal
from uuid import UUID

from openpyxl import Workbook

from app.models.inventory_item import InventoryItem
from app.models.unit_measure import UnitMeasure
from app.api.v1.inventory.schemas import INVENTORY_TEMPLATE_HEADERS


def _type_label(type: str) -> str:
    """Replica `typeLabel()` (inventory-page.component.ts:488-490)."""
    return "Empacado" if type == "packaged" else "Materia prima"


def build_inventory_excel(
    items: list[InventoryItem],
    unit_measures_by_id: dict[UUID, UnitMeasure],
    preferred_by_item: dict[UUID, dict] | None = None,
) -> io.BytesIO:
    """Arma el workbook completo en memoria: fila 1 con `INVENTORY_TEMPLATE_HEADERS`
    (FR-001/FR-002) y una fila por cada `InventoryItem` recibido, con Stock/Mínimo/
    Costo/Factor de conversión/Último costo como valores numéricos nativos (sin
    redondear ni truncar). `preferred_by_item` es el resultado de
    `supplier_items.preferred_relations_by_item` -- un insumo sin proveedor
    preferido deja esas 4 columnas en blanco, sin error (spec 109, Historia 1,
    escenario 3)."""
    preferred_by_item = preferred_by_item or {}
    wb = Workbook()
    ws = wb.active
    ws.append(INVENTORY_TEMPLATE_HEADERS)

    for item in items:
        unit = unit_measures_by_id.get(item.unit_measure_id)
        preferred = preferred_by_item.get(item.id)
        ws.append([
            item.code,
            item.name,
            _type_label(item.type),
            unit.abbreviation if unit is not None else "",
            item.current_stock,
            item.min_stock,
            item.unit_cost,
            preferred["supplier_name"] if preferred else "",
            preferred["presentation"] if preferred else "",
            preferred["conversion_factor"] if preferred else "",
            preferred["last_cost"] if preferred else "",
        ])

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


def build_import_errors_excel(errors: list[dict]) -> io.BytesIO:
    """Reporte descargable de las filas fallidas de un `InventoryImportJob` (spec
    109, FR-012): una fila por error, con el número de fila original del archivo
    subido, su código (si lo trajo) y el motivo del fallo."""
    wb = Workbook()
    ws = wb.active
    ws.append(["Fila", "Código", "Motivo"])
    for error in errors:
        ws.append([error.get("fila"), error.get("codigo") or "", error.get("motivo") or ""])

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer
