"""Service de compras: registra la compra y da alta de stock por cada item
(equivalente al trigger `fn_add_inventory_on_purchase` de schema.sql, en la capa
de aplicación).

spec 107: una línea de compra puede venir en unidad base (`quantity`/`unit_cost`, como siempre)
o en una presentación guardada (`supplier_item_id` + `presentation_quantity` +
`presentation_unit_cost`) -- `_resolve_purchase_line` es el único lugar que decide cuál de los
dos casos aplica y calcula la conversión; `create_purchase`/`create_purchase_order` la
reutilizan para no duplicar esa lógica (research.md D2 de spec 107)."""
import logging
import re
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.core.crud import get_or_404
from app.core.timezone import utc_now_naive
from app.core import units
from app.models.inventory_item import InventoryItem
from app.models.inventory_import_job import InventoryImportJob
from app.models.supplier import Supplier
from app.models.supplier_item import SupplierItem
from app.models.unit_measure import UnitMeasure
from app.models.purchase import Purchase, PurchaseItem
from app.api.v1.inventory import supplier_items
from app.api.v1.inventory.stock import record_movement
from app.api.v1.inventory.schemas import PurchaseCreate, PurchaseItemIn, PurchaseReceiveIn
from app.core import inventory_reasons as reasons

logger = logging.getLogger(__name__)

# research.md D5: muy por encima de lo que toma procesar ≈500 filas -- un job
# "procesando" más viejo que esto se considera abandonado (el worker murió sin
# actualizar su estado), no una importación genuinamente en curso.
IMPORT_JOB_TIMEOUT = timedelta(minutes=30)


def find_blocking_import_job(db: Session) -> InventoryImportJob | None:
    """FR-010/research.md D5: devuelve el job `procesando` vigente del tenant, o
    `None` si no hay ninguno que realmente bloquee una importación nueva. Un job
    `procesando` que ya superó `IMPORT_JOB_TIMEOUT` se marca `interrumpido` (sin
    revertir nada ya aplicado, FR-015) y deja de bloquear."""
    candidatos = db.execute(
        select(InventoryImportJob).where(InventoryImportJob.status == "procesando")
    ).scalars().all()
    ahora = utc_now_naive()
    for job in candidatos:
        if ahora - job.started_at > IMPORT_JOB_TIMEOUT:
            job.status = "interrumpido"
            job.finished_at = ahora
            db.add(job)
        else:
            db.commit()
            return job
    db.commit()
    return None


def list_items_query(
    search: str | None = None,
    type_: str | None = None,
    active: bool | None = None,
    low_stock: bool | None = None,
    supplier_id: UUID | None = None,
) -> Select:
    """Construye el Select filtrado/ordenado para GET /inventory/items.

    `paginate()` (app/core/pagination.py) aplica offset/limit sobre este Select.
    """
    stmt = select(InventoryItem).order_by(InventoryItem.name)
    if search:
        stmt = stmt.where(
            or_(
                InventoryItem.name.ilike(f"%{search}%"),
                InventoryItem.code.ilike(f"%{search}%"),
            )
        )
    if type_ is not None:
        stmt = stmt.where(InventoryItem.type == type_)
    if active is not None:
        stmt = stmt.where(InventoryItem.active == active)
    if low_stock:
        stmt = stmt.where(InventoryItem.current_stock <= InventoryItem.min_stock)
    if supplier_id is not None:
        stmt = stmt.where(
            exists().where(
                SupplierItem.inventory_item_id == InventoryItem.id,
                SupplierItem.supplier_id == supplier_id,
                SupplierItem.active.is_(True),
            )
        )
    return stmt


def list_all_items_for_export(db: Session) -> list[InventoryItem]:
    """Todos los insumos del tenant (activos e inactivos), sin paginar ni filtrar —
    respaldo completo para GET /inventory/items/export (FR-003). Mismo orden por
    defecto que `list_items_query()`."""
    return db.execute(
        select(InventoryItem).order_by(InventoryItem.name)
    ).scalars().all()


_NUMERIC_CODE = re.compile(r"^\d+$")


def generate_next_code(db: Session) -> str:
    """Código secuencial con cero a la izquierda (spec 109, research.md D3): el
    siguiente entero después del código numérico más alto ya asignado en este
    tenant, con al menos 4 dígitos. Códigos no numéricos que el administrador haya
    fijado a mano no interfieren con la secuencia automática -- se ignoran al
    buscar el máximo."""
    codes = db.execute(select(InventoryItem.code)).scalars().all()
    max_numeric = 0
    for code in codes:
        if _NUMERIC_CODE.match(code):
            max_numeric = max(max_numeric, int(code))
    return f"{max_numeric + 1:04d}"


def resolve_or_create_by_code(
    db: Session, code: str | None, **fields
) -> tuple[InventoryItem, bool]:
    """Upsert por código (spec 109, FR-007): si `code` coincide con un insumo ya
    existente del tenant, lo actualiza con `fields`; si no coincide con ninguno, o
    viene vacío, crea uno nuevo (asignándole un código automático con
    `generate_next_code` cuando viene vacío). Devuelve `(insumo, fue_creado)`.
    Usado tanto por el alta manual de un insumo (router.py) como por cada fila de
    una importación masiva (import_.py)."""
    item = None
    if code:
        item = db.execute(
            select(InventoryItem).where(InventoryItem.code == code)
        ).scalar_one_or_none()

    if item is not None:
        for key, value in fields.items():
            setattr(item, key, value)
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise ValueError(
                f"Ya existe un insumo con el nombre '{fields.get('name')}'"
            ) from exc
        return item, False

    item = InventoryItem(code=code or generate_next_code(db), **fields)
    db.add(item)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError(
            f"Ya existe un insumo con el nombre '{fields.get('name')}'"
        ) from exc
    return item, True


def _resolve_purchase_line(
    db: Session, purchase: Purchase, it: PurchaseItemIn, tenant,
) -> tuple[PurchaseItem, Decimal, InventoryItem]:
    """Resuelve una línea de alta directa u orden: si trae `supplier_item_id`, calcula
    `quantity`/`unit_cost` en unidad base a partir de la presentación (spec 106/107,
    FR-003/FR-004), conserva lo capturado originalmente (`presentation_quantity`, snapshot de
    `presentation_label`) y aplica RN-15; si trae `unit_measure_id`, resuelve el factor de
    conversión hacia la unidad base del insumo (spec 134, FR-008); si no trae ninguno de los
    dos, se comporta exactamente igual que antes de esta spec (FR-007). Devuelve el
    `PurchaseItem` sin persistir todavía, el total de la línea con los valores originales
    (FR-005), y el `InventoryItem` ya cargado."""
    item = get_or_404(db, InventoryItem, it.inventory_item_id, "Inventory item not found")

    if it.supplier_item_id is not None:
        relation = get_or_404(
            db, SupplierItem, it.supplier_item_id, "Relación proveedor-insumo no encontrada",
        )
        if not relation.active:
            raise HTTPException(422, "La relación proveedor-insumo no está activa")
        if relation.inventory_item_id != it.inventory_item_id:
            raise HTTPException(
                422, "La presentación elegida no corresponde al insumo de esta línea",
            )
        if relation.supplier_id != purchase.supplier_id:
            raise HTTPException(
                422, "La presentación elegida no corresponde al proveedor de esta compra",
            )
        if relation.conversion_factor is None:
            # spec 138: una relación creada por import de inventario puede no tener
            # presentación/factor todavía -- no se puede usar en una compra hasta
            # completarla desde el módulo de proveedores.
            raise HTTPException(
                422, "Esta relación todavía no tiene un factor de conversión asignado; "
                     "complétala desde el módulo de proveedores antes de usarla en una compra",
            )

        quantity = it.presentation_quantity * relation.conversion_factor
        unit_cost = (it.presentation_unit_cost / relation.conversion_factor).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP,
        )
        line_total = it.presentation_quantity * it.presentation_unit_cost

        purchase_item = PurchaseItem(
            purchase_id=purchase.id,
            inventory_item_id=it.inventory_item_id,
            quantity=quantity,
            unit_cost=unit_cost,
            supplier_item_id=relation.id,
            presentation_quantity=it.presentation_quantity,
            presentation_label=relation.presentation,
            # La conversión de esta ruta la gobierna `relation.conversion_factor`, no esta
            # spec: se guarda la unidad base del insumo con factor 1 (research.md D5).
            unit_measure_id=item.unit_measure_id,
            conversion_factor_applied=Decimal("1"),
            quantity_base=quantity,
            cost_base=unit_cost,
        )
        supplier_items.apply_purchase_cost(db, relation, it.presentation_unit_cost)
    elif it.unit_measure_id is not None:
        chosen_unit = get_or_404(db, UnitMeasure, it.unit_measure_id, "Unit measure not found")
        if not chosen_unit.active:
            raise HTTPException(422, "La unidad de medida no está activa")
        base_unit = get_or_404(db, UnitMeasure, item.unit_measure_id, "Unit measure not found")
        if (
            tenant is not None
            and not tenant.unit_conversion_enabled
            and chosen_unit.id != base_unit.id
        ):
            raise HTTPException(
                422,
                "La conversión de unidades está desactivada para este negocio; "
                "usa la unidad base del insumo",
            )

        conversion_factor_applied = units.convert(Decimal("1"), chosen_unit, base_unit)
        quantity_base = it.quantity * conversion_factor_applied
        cost_base = it.unit_cost / conversion_factor_applied
        line_total = it.quantity * it.unit_cost

        purchase_item = PurchaseItem(
            purchase_id=purchase.id,
            inventory_item_id=it.inventory_item_id,
            quantity=it.quantity,
            unit_cost=it.unit_cost,
            unit_measure_id=chosen_unit.id,
            conversion_factor_applied=conversion_factor_applied,
            quantity_base=quantity_base,
            cost_base=cost_base,
        )
    else:
        purchase_item = PurchaseItem(
            purchase_id=purchase.id,
            inventory_item_id=it.inventory_item_id,
            quantity=it.quantity,
            unit_cost=it.unit_cost,
            unit_measure_id=item.unit_measure_id,
            conversion_factor_applied=Decimal("1"),
            quantity_base=it.quantity,
            cost_base=it.unit_cost,
        )
        line_total = it.quantity * it.unit_cost

    return purchase_item, line_total, item


def create_purchase(
    db: Session, data: PurchaseCreate, user_id: UUID | None, tenant=None,
) -> Purchase:
    if data.supplier_id is not None:
        get_or_404(db, Supplier, data.supplier_id, "Supplier not found")

    try:
        purchase = Purchase(
            supplier_id=data.supplier_id,
            invoice_number=data.invoice_number,
            user_id=user_id,
            total=Decimal("0"),
        )
        db.add(purchase)
        db.flush()

        total = Decimal("0")
        for it in data.items:
            purchase_item, line_total, item = _resolve_purchase_line(db, purchase, it, tenant)
            purchase_item.received_quantity = purchase_item.quantity  # alta directa: recibida por completo
            db.add(purchase_item)
            # Alta de stock + kardex + actualización del costo unitario, en unidad base
            # (spec 134: `quantity_base`/`cost_base` ya resueltos por `_resolve_purchase_line`).
            record_movement(
                db, item.id, type="in", quantity=purchase_item.quantity_base,
                reason=reasons.COMPRA, reference_type=reasons.REF_PURCHASE, reference_id=purchase.id,
                user_id=user_id,
            )
            item.unit_cost = purchase_item.cost_base
            total += line_total

        purchase.status = "received"
        purchase.total = total
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Error registrando compra")
        raise
    db.refresh(purchase)
    return purchase


def create_purchase_order(
    db: Session, data: PurchaseCreate, user_id: UUID | None, tenant=None,
) -> Purchase:
    """Crea una orden de compra en estado `draft` SIN dar alta de stock (RF-022).
    El stock se suma luego con `receive_purchase` (parcial o total)."""
    if data.supplier_id is not None:
        get_or_404(db, Supplier, data.supplier_id, "Supplier not found")
    try:
        purchase = Purchase(
            supplier_id=data.supplier_id, invoice_number=data.invoice_number,
            user_id=user_id, status="draft", total=Decimal("0"),
        )
        db.add(purchase)
        db.flush()
        total = Decimal("0")
        for it in data.items:
            purchase_item, line_total, _item = _resolve_purchase_line(db, purchase, it, tenant)
            purchase_item.received_quantity = Decimal("0")
            db.add(purchase_item)
            total += line_total
        purchase.total = total
        db.commit()
    except HTTPException:
        db.rollback(); raise
    except Exception:
        db.rollback(); logger.exception("Error creando orden de compra"); raise
    db.refresh(purchase)
    return purchase


def receive_purchase(db: Session, purchase_id: UUID, data: PurchaseReceiveIn,
                     user_id: UUID | None) -> Purchase:
    """Recibe (parcial o totalmente) una orden de compra: suma stock por los
    ítems indicados, incrementa `received_quantity` y ajusta el estado.

    spec 107: si el ítem se pactó en una presentación (`supplier_item_id` no NULL), la
    recepción viene en `presentation_quantity` y se convierte con el factor que ya está fijado
    en el propio `PurchaseItem` -- nunca con uno que llegue en el request (research.md D4),
    así que el factor no puede cambiar entre crear la orden y recibirla."""
    purchase = get_or_404(db, Purchase, purchase_id, "Purchase not found")
    if purchase.status == "received":
        raise HTTPException(409, "La compra ya fue recibida por completo")

    items = {i.id: i for i in purchase.items}
    try:
        for r in data.items:
            pi = items.get(r.purchase_item_id)
            if pi is None:
                raise HTTPException(404, f"Ítem de compra {r.purchase_item_id} no encontrado")

            if pi.supplier_item_id is not None:
                if r.presentation_quantity is None:
                    raise HTTPException(
                        422,
                        f"El ítem {pi.id} se pactó en una presentación; indica "
                        f"presentation_quantity, no quantity",
                    )
                relation = get_or_404(
                    db, SupplierItem, pi.supplier_item_id,
                    "Relación proveedor-insumo no encontrada",
                )
                quantity_received = r.presentation_quantity * relation.conversion_factor
            else:
                if r.quantity is None:
                    raise HTTPException(
                        422, f"El ítem {pi.id} no usa presentación; indica quantity",
                    )
                quantity_received = r.quantity

            remaining = Decimal(pi.quantity) - Decimal(pi.received_quantity)
            if quantity_received > remaining:
                raise HTTPException(
                    422,
                    f"Recepción {quantity_received} excede lo pendiente ({remaining}) del ítem {pi.id}",
                )
            # `quantity_received` está en la unidad capturada de la línea (`pi.unit_measure_id`,
            # spec 134); el stock/kardex se mueven en unidad base del insumo.
            quantity_received_base = quantity_received * Decimal(pi.conversion_factor_applied)
            record_movement(
                db, pi.inventory_item_id, type="in", quantity=quantity_received_base,
                reason=reasons.COMPRA, reference_type=reasons.REF_PURCHASE,
                reference_id=purchase.id, user_id=user_id,
            )
            pi.received_quantity = Decimal(pi.received_quantity) + quantity_received
            inv = db.get(InventoryItem, pi.inventory_item_id)
            if inv is not None:
                inv.unit_cost = pi.cost_base

        fully = all(Decimal(i.received_quantity) >= Decimal(i.quantity) for i in purchase.items)
        any_received = any(Decimal(i.received_quantity) > 0 for i in purchase.items)
        purchase.status = "received" if fully else ("partial" if any_received else "draft")
        db.commit()
    except HTTPException:
        db.rollback(); raise
    except Exception:
        db.rollback(); logger.exception("Error recibiendo compra"); raise
    db.refresh(purchase)
    return purchase


def void_purchase(db: Session, purchase_id: UUID, user_id: UUID | None) -> Purchase:
    """Anula una compra confirmada (spec 134, US3, FR-012/FR-013): revierte el stock
    exactamente en la cantidad base guardada en cada línea (`quantity_base`), nunca
    recalculada con el factor de conversión vigente -- `conversion_factor_applied` es
    inmutable por diseño (FR-010). Rechaza una segunda anulación de la misma compra."""
    purchase = get_or_404(db, Purchase, purchase_id, "Purchase not found")
    if purchase.voided_at is not None:
        raise HTTPException(409, "La compra ya fue anulada")

    try:
        for pi in purchase.items:
            record_movement(
                db, pi.inventory_item_id, type="out", quantity=pi.quantity_base,
                reason=reasons.AJUSTE, reference_type=reasons.REF_PURCHASE_VOID,
                reference_id=purchase.id, user_id=user_id,
            )
        purchase.voided_at = utc_now_naive()
        purchase.voided_by = user_id
        db.commit()
    except HTTPException:
        db.rollback(); raise
    except Exception:
        db.rollback(); logger.exception("Error anulando compra"); raise
    db.refresh(purchase)
    return purchase
