"""Checkout de venta: arma la venta con snapshots inmutables, valida el pago,
la liga al turno de caja y descuenta inventario (receta + opciones). Dueño de la
transacción."""
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import cast, func, select, String
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.sql import Select

from app.core.asset_refs import ImageDecision, resolve_image_change
from app.core.crud import ensure_unique, get_or_404
from app.core.models import User
from app.core.storage import FOLDER_PAYMENT_METHODS, normalize_asset_ref
from app.core.timezone import local_day_bounds_utc, resolve_timezone
from app.models.invoice import Invoice
from app.models.product_variant import ProductVariant
from app.models.product import Product
from app.models.option import Option
from app.models.cash_shift import CashShift
from app.models.payment import Payment, PaymentMethod
from app.models.payment_method_catalog import PaymentMethodCatalog
from app.models.sale import Sale, SaleItem
from app.api.v1.catalog.line_pricing import load_valid_options
from app.catalog_engine import compute_addons_total, compute_unit_price, mark_per_line
from app.catalog_engine.core import format_item_description
from app.api.v1.orders import checkout as orders_checkout
from app.api.v1.sales.consumption import deduct_sale
from app.api.v1.sales.builder import SaleLine, build_sale, ensure_open_shift
from app.api.v1.sales.schemas import (
    SaleCreate, PaymentMethodCreate, PaymentMethodUpdate, CatalogPaymentMethodOption,
)

logger = logging.getLogger(__name__)


# ============================ Métodos de pago (spec 024 / spec 032) ============================

def _validate_payment_info(fields: list[dict], payment_info: dict | None) -> bool:
    """Valida `payment_info` contra `catalog.fields` (obligatoriedad + formato,
    spec 032 FR-009, clarificación 2026-08-24 #3). Devuelve si la
    configuración queda completa (todo lo obligatorio, diligenciado y
    válido). Lanza 422 si algo diligenciado no cumple su formato — un campo
    obligatorio simplemente vacío no es un error, solo deja `is_complete` en
    `False` (FR-009 lo llama "incompleta", no inválida)."""
    info = payment_info or {}
    complete = True
    for field in fields:
        key = field["key"]
        value = info.get(key)
        if value is None or value == "":
            if field.get("required", False):
                complete = False
            continue

        fmt = field.get("format")
        length = field.get("length")
        value_str = str(value)
        if fmt == "numeric" and not value_str.isdigit():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"El campo '{key}' debe ser numérico.",
            )
        if fmt in ("numeric", "text") and length and len(value_str) != length:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"El campo '{key}' debe tener exactamente {length} caracteres.",
            )
    return complete


def _image_field_keys(catalog: PaymentMethodCatalog | None) -> list[str]:
    """Claves de `payment_info` que son un archivo (`format:"image"`, típ. `qr`)."""
    if catalog is None:
        return []
    return [f["key"] for f in (catalog.fields or []) if f.get("format") == "image"]


def _require_schema(tenant_schema: str | None) -> str:
    if not tenant_schema:
        raise ValueError("tenant_schema es obligatorio para validar la imagen del método de pago")
    return tenant_schema


def _verify_new_payment_images(
    db: Session, tenant_schema: str | None, catalog: PaymentMethodCatalog, payment_info: dict | None,
) -> None:
    """Creación (spec 088, FR-001/FR-003/FR-004): cada clave de imagen gestionada debe
    ser una key del propio negocio, carpeta `payment-methods`, y existir en R2. Sin base:
    no hay imagen vigente que comparar. 422/503 antes de crear nada."""
    for key in _image_field_keys(catalog):
        sent = normalize_asset_ref((payment_info or {}).get(key))
        if sent is None:
            continue
        resolve_image_change(
            db, tenant_schema=_require_schema(tenant_schema), folder=FOLDER_PAYMENT_METHODS,
            sent=sent, base_provided=False, base=None, current=None, is_creation=True,
        )


def _resolve_payment_images(
    db: Session,
    tenant_schema: str | None,
    catalog: PaymentMethodCatalog,
    current_info: dict | None,
    sent_info: dict[str, str],
    base_info: dict[str, str] | None,
    base_provided: bool,
) -> dict[str, str]:
    """Edición (spec 088, research D7): devuelve el `payment_info` **resultante**, con la
    regla de imagen aplicada por cada clave `format:"image"` del catálogo. El resto de las
    claves (`celular`, `cuenta`…) se guardan tal cual llegan.

    Por clave `k`: igual a la vigente → sin cambio; forma inválida/ajena → 422; sin
    `payment_info_base` o `base[k]` ≠ vigente (formulario desactualizado) → se conserva
    el valor vigente (o se elimina la clave si no había), en silencio; edición legítima
    con `k` vacío/ausente → se elimina (comportamiento actual, el archivo no se borra,
    D8); edición legítima con valor nuevo → debe existir en R2 (422/503).
    """
    result = dict(sent_info)
    current_info = current_info or {}
    for key in _image_field_keys(catalog):
        sent = normalize_asset_ref(sent_info.get(key))
        current_raw = current_info.get(key)
        current = normalize_asset_ref(current_raw)
        base = normalize_asset_ref((base_info or {}).get(key))
        if sent is None:
            if current is not None and not (base_provided and base == current):
                # Quitar la imagen solo es legítimo con una base que coincida con la
                # vigente; si no, el formulario está desactualizado: se conserva.
                result[key] = current_raw
            continue
        decision = resolve_image_change(
            db, tenant_schema=_require_schema(tenant_schema), folder=FOLDER_PAYMENT_METHODS,
            sent=sent, base_provided=base_provided, base=base, current=current,
            is_creation=False,
        )
        if decision is ImageDecision.IGNORE:
            if current_raw is None or current is None:
                result.pop(key, None)
            else:
                result[key] = current_raw
    return result


def create_payment_method(
    db: Session, data: PaymentMethodCreate, *, tenant_schema: str | None = None
) -> PaymentMethod:
    """Activa, para el tenant, un método del catálogo de la plataforma (spec
    032, FR-007/FR-011). `name`/`type`/`is_cash` se copian del catálogo
    (research.md Decisión 5) — el body ya no los acepta, un tenant no puede
    crear métodos fuera del catálogo.

    Crea fila solo la primera vez por `catalog_id`: si ya existe una (activa
    o no), 409 — reactivar/editar es `update_payment_method` (`PATCH`), no
    esta función (research.md Decisión 9: `catalog_id` es único por tenant
    para siempre, así se conserva el `payment_info` ya capturado al
    reactivar)."""
    catalog = get_or_404(
        db, PaymentMethodCatalog, data.catalog_id,
        "Método de pago no encontrado en el catálogo",
    )
    if not catalog.active:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Este método de pago no está activo en el catálogo de la plataforma.",
        )

    existing = db.execute(
        select(PaymentMethod).where(PaymentMethod.catalog_id == catalog.id)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Ya existe una configuración para este método de pago en este tenant; "
            "use PATCH para editarla o reactivarla.",
        )

    if catalog.type == "mixed":
        # FR-004 (spec 130): "Pago mixto" solo puede activarse si el tenant ya
        # tiene, entre sus métodos activos, al menos uno de efectivo y al menos
        # uno de transferencia — no se reevalúa después de activado (data-model.md).
        active_types = set(
            db.execute(
                select(PaymentMethod.type).where(PaymentMethod.active.is_(True))
            ).scalars().all()
        )
        faltantes = []
        if "cash" not in active_types:
            faltantes.append("efectivo")
        if "transfer" not in active_types:
            faltantes.append("transferencia")
        if faltantes:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Para activar 'Pago mixto' el tenant debe tener activos al menos un "
                "método de efectivo y uno de transferencia. Falta: "
                + " y ".join(faltantes) + ".",
            )

    is_complete = _validate_payment_info(catalog.fields, data.payment_info)
    _verify_new_payment_images(db, tenant_schema, catalog, data.payment_info)
    method = PaymentMethod(
        name=catalog.name,
        type=catalog.type,
        is_cash=(catalog.type == "cash"),
        catalog_id=catalog.id,
        payment_info=data.payment_info,
        is_complete=is_complete,
        active=True,
    )
    db.add(method)
    db.commit()
    db.refresh(method)
    return method


def update_payment_method(
    db: Session, payment_method_id: UUID, data: PaymentMethodUpdate,
    *, tenant_schema: str | None = None,
) -> PaymentMethod:
    """Edita datos de pago/estado de un método ya activado (spec 024 US1,
    spec 032 FR-008/FR-009/FR-010).

    Editar `payment_info` recalcula `is_complete` contra `catalog.fields`
    vigente (spec 032, research.md Decisión 4). Al desactivar (`active:
    False`), exige que quede al menos un método activo en el tenant —
    contado dentro de la misma transacción, excluyendo el propio método
    (research.md spec 024, Decisión 10). Reactivar (`active: True`) es el
    único camino para volver a usar un método que el tenant había
    desactivado — conserva el `payment_info` que ya tenía si no se manda uno
    nuevo (research.md spec 032, Decisión 9)."""
    method = get_or_404(db, PaymentMethod, payment_method_id, "Payment method not found")

    if data.payment_info is not None:
        catalog = db.get(PaymentMethodCatalog, method.catalog_id) if method.catalog_id else None
        payment_info = data.payment_info
        if catalog is not None:
            # spec 088 (FR-001…FR-004, research D7/D8): la clave de imagen se resuelve
            # ANTES de asignar nada; `is_complete` se calcula sobre el `payment_info`
            # resultante (con la imagen conservada si la enviada se ignoró).
            payment_info = _resolve_payment_images(
                db, tenant_schema, catalog, method.payment_info, data.payment_info,
                data.payment_info_base, "payment_info_base" in data.model_fields_set,
            )
        method.payment_info = payment_info
        if catalog is not None:
            method.is_complete = _validate_payment_info(catalog.fields, payment_info)

    if data.active is not None and data.active != method.active:
        if data.active is False:
            active_count = db.execute(
                select(func.count()).select_from(PaymentMethod).where(
                    PaymentMethod.active.is_(True),
                    PaymentMethod.id != payment_method_id,
                )
            ).scalar_one()
            if active_count == 0:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Debe quedar al menos un método de pago activo.",
                )
        method.active = data.active

    db.commit()
    db.refresh(method)
    return method


def list_catalog_for_tenant(db: Session) -> list[CatalogPaymentMethodOption]:
    """Catálogo activo a nivel plataforma, más las entradas que el tenant ya
    activó aunque el Super Admin las haya desactivado después — para que el
    frontend pueda avisar "ya no disponible" (spec 032, FR-005/FR-006)."""
    tenant_catalog_ids = {
        row[0] for row in db.execute(
            select(PaymentMethod.catalog_id).where(PaymentMethod.catalog_id.is_not(None))
        ).all()
    }
    catalogs = db.execute(
        select(PaymentMethodCatalog)
        .where(
            PaymentMethodCatalog.active.is_(True)
            | PaymentMethodCatalog.id.in_(tenant_catalog_ids)
        )
        .order_by(PaymentMethodCatalog.name)
    ).scalars().all()
    return [
        CatalogPaymentMethodOption(
            id=c.id, name=c.name, fields=c.fields, active=c.active,
            already_activated=c.id in tenant_catalog_ids,
        )
        for c in catalogs
    ]


def list_available_payment_methods(db: Session) -> list[PaymentMethod]:
    """Métodos disponibles para cobrar en caja (spec 032, FR-012): activos,
    completos, y con el catálogo todavía activo a nivel plataforma — sin
    exponer `payment_info` (FR-012a, el `PaymentMethodCheckoutOption` del
    router se encarga de eso, no esta consulta).

    `outerjoin`, no `join`: una fila sin `catalog_id` todavía (ventana de
    backfill, FR-016) no debe desaparecer de caja solo por no tener catálogo
    asignado — sigue disponible mientras sea `active`/`is_complete` (default
    `true` hasta que el backfill la procese). Solo se excluye por causa del
    catálogo cuando SÍ tiene `catalog_id` y ese catálogo está inactivo."""
    return db.execute(
        select(PaymentMethod)
        .outerjoin(PaymentMethodCatalog, PaymentMethod.catalog_id == PaymentMethodCatalog.id)
        .where(
            PaymentMethod.active.is_(True),
            PaymentMethod.is_complete.is_(True),
            (PaymentMethod.catalog_id.is_(None)) | (PaymentMethodCatalog.active.is_(True)),
        )
        .order_by(PaymentMethod.name)
    ).scalars().all()


def checkout(db: Session, data: SaleCreate, cashier: User, *, invoice_prefix: str = "") -> Sale:
    """Venta de mostrador: arma la venta, descuenta inventario y emite factura.

    Usa `build_sale` —el mismo constructor que el cobro de mesa— para no volver a
    tener dos implementaciones divergentes de lo mismo. Lo propio de este camino y
    que no vive en el builder: las **promociones** (que ajustan el descuento antes
    de construir) y el **descuento de inventario**, porque aquí no hubo un paso de
    confirmación previo que ya lo hiciera.
    """
    shift = ensure_open_shift(db, data.cash_shift_id)

    try:
        now = datetime.now(timezone.utc)

        # 1. Resolver y valorar las líneas (snapshot de precio: variante + opciones).
        lines: list[SaleLine] = []
        for line in data.items:
            variant = get_or_404(db, ProductVariant, line.product_variant_id, "Variant not found")
            product = db.get(Product, variant.product_id)
            description = format_item_description(product, variant)

            # Deduplica, exige que estén activas y valida la selección contra los
            # grupos del producto. Antes este bucle cargaba las opciones a mano y se
            # saltaba las tres cosas.
            options = load_valid_options(db, line.options, variant=variant, quantity=line.quantity)
            # spec 117 (A-126): nunca es el carrito QR -- `qr_addons_per_line=False`.
            options = mark_per_line(db, options, qr_addons_per_line=False)
            options_snapshot: list[dict] = [
                {
                    "option_id": str(chosen.option.id),
                    "name": chosen.option.name,
                    "extra_price": str(chosen.option.extra_price),
                    "quantity": chosen.quantity,
                    # spec 089/117: ausente = por unidad (snapshots anteriores). Solo se
                    # escribe la clave en los adicionales que se cobran por línea.
                    **({"per_line": True} if chosen.per_line else {}),
                }
                for chosen in options
            ]

            lines.append(SaleLine(
                product_variant_id=variant.id,
                description=description,
                options=options_snapshot,
                quantity=line.quantity,
                unit_price=compute_unit_price(variant, options),
                addons_total=compute_addons_total(options),
            ))

        # 2. Descuento automático (RF-012): motor por conjunto de variantes
        #    (spec 063). Se suma al descuento manual que haya escrito el cajero.
        promo_discount, final_promotion_id, applied = orders_checkout.auto_discount(db, lines, now)

        sale = build_sale(
            db,
            lines=lines,
            shift=shift,
            cashier=cashier,
            payments=data.payments,
            discount=Decimal(data.discount) + promo_discount,
            tax=data.tax,
            tip=data.tip,
            customer_name=data.customer_name,
            dining_table_id=data.dining_table_id,
            participant_id=data.participant_id,
            promotion_id=final_promotion_id,
            applied_promotions=applied,
            invoice_prefix=invoice_prefix,
        )

        # La sesión tiene autoflush=False; forzamos el flush para que deduct_sale
        # vea los sale_items recién insertados.
        db.flush()

        # 3. Inventario: aquí sí descuenta (mesa ya lo hizo al confirmar).
        #    Puede lanzar InsufficientStockError y tumbar toda la transacción.
        deduct_sale(db, sale, user_id=cashier.id)

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Error en checkout de venta")
        raise

    return db.execute(select(Sale).where(Sale.id == sale.id)).scalar_one()


def list_sales_query(
    tenant,
    status: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    invoice_reference: str | None = None,
) -> Select:
    """Construye el `Select` de ventas para el listado paginado (GET /sales).

    `date_from`/`date_to` se interpretan como día calendario en la zona
    horaria del tenant, no medianoche UTC (spec 030, FR-004,
    contracts/date-range-filters.md)."""
    stmt = (
        select(Sale)
        .options(
            selectinload(Sale.items),
            selectinload(Sale.payments),
            selectinload(Sale.invoice),
            selectinload(Sale.dining_table),
        )
        .order_by(Sale.sold_at.desc())
    )
    if status:
        stmt = stmt.where(Sale.status == status)
    if date_from or date_to:
        tz = resolve_timezone(tenant)
        if date_from:
            start, _ = local_day_bounds_utc(date_from, tz)
            stmt = stmt.where(Sale.sold_at >= start)
        if date_to:
            _, end = local_day_bounds_utc(date_to, tz)
            stmt = stmt.where(Sale.sold_at < end)
    if invoice_reference:
        # No hay columna "referencia": se reconstruye prefix + número (6 dígitos)
        # tal como se imprime en el ticket (ver Invoice.full_number).
        full_number = func.concat(Invoice.prefix, func.lpad(cast(Invoice.number, String), 6, "0"))
        stmt = stmt.join(Invoice, Invoice.sale_id == Sale.id).where(
            full_number.ilike(f"%{invoice_reference.strip()}%")
        )
    return stmt
