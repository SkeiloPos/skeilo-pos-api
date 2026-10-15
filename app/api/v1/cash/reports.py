"""Reporte operativo de ventas por turno de caja (spec 119, research.md D5).

Vive en `cash`, no en `app/api/v1/reports/`: ese paquete atribuye por fecha
calendario de `Sale.sold_at` (criterio que la spec 119 declara intacto,
Clarifications) — este módulo atribuye por `Sale.cash_shift_id`, el vínculo
autoritativo venta↔turno (research.md D3). Una sola función
(`compute_shift_sales_report`) calcula el reporte de un turno tanto EN VIVO
(turno abierto, US4) como para el snapshot que `close_shift` persiste una
sola vez al cerrar (research.md D2) — garantiza que el valor congelado sea
exactamente el último valor en vivo visible antes del cierre.
"""
import calendar
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.core.timezone import local_day_bounds_utc, resolve_timezone, utc_now
from app.models.cash_shift import CashShift
from app.models.category import Category
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.sale import Sale, SaleItem
from app.models.shift_sales_report import ShiftSalesReport

# research.md D5, confirmado con el usuario 2026-10-15 -- mismo valor que ya
# usa el frontend para GET /reports/top-products.
TOP_PRODUCTS_LIMIT = 10


def compute_shift_sales_report(db: Session, shift: CashShift) -> dict:
    """Reporte de UN turno: replica el join `Sale → SaleItem →
    ProductVariant → Product → Category` de
    `reports/service.py::products_report`/`categories_report`, acotado por
    `cash_shift_id` en vez de por rango de fechas. Devuelve el desglose
    COMPLETO (no top-N, FR-017) ya ordenado por ingreso descendente -- el
    recorte al top-N visible lo hace el llamador (`aggregate_shifts`)."""
    conds = [Sale.cash_shift_id == shift.id, Sale.status == "paid"]

    total_sales, order_count = db.execute(
        select(func.coalesce(func.sum(Sale.total), 0), func.count(Sale.id)).where(*conds)
    ).one()
    total_sales = Decimal(total_sales)
    avg_ticket = (
        (total_sales / order_count).quantize(Decimal("0.01"))
        if order_count else Decimal("0.00")
    )

    product_rows = db.execute(
        select(
            SaleItem.product_variant_id,
            func.max(SaleItem.description),
            func.sum(SaleItem.quantity),
            func.sum(SaleItem.line_total),
        )
        .select_from(SaleItem).join(Sale, Sale.id == SaleItem.sale_id).where(*conds)
        .group_by(SaleItem.product_variant_id)
        .order_by(func.sum(SaleItem.line_total).desc())
    ).all()
    products_breakdown = [
        {
            "product_variant_id": str(vid), "name": desc,
            "units": int(units), "revenue": str(Decimal(rev)),
        }
        for vid, desc, units, rev in product_rows
    ]

    # LEFT JOIN + "Sin categoría" (data-model.md §2): `Product.category_id`
    # es NOT NULL hoy, así que esta rama es defensiva (ninguna fila real cae
    # ahí todavía), no un comportamiento observado en el catálogo actual.
    category_rows = db.execute(
        select(
            Category.id, Category.name,
            func.sum(SaleItem.quantity), func.sum(SaleItem.line_total),
        )
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .join(ProductVariant, ProductVariant.id == SaleItem.product_variant_id)
        .join(Product, Product.id == ProductVariant.product_id)
        .outerjoin(Category, Category.id == Product.category_id)
        .where(*conds)
        .group_by(Category.id, Category.name)
        .order_by(func.sum(SaleItem.line_total).desc())
    ).all()
    categories_breakdown = [
        {
            "category_id": str(cid) if cid else None,
            "category_name": cname or "Sin categoría",
            "units": int(u), "revenue": str(Decimal(r)),
        }
        for cid, cname, u, r in category_rows
    ]

    return {
        "total_sales": total_sales,
        "order_count": order_count,
        "avg_ticket": avg_ticket,
        "products_breakdown": products_breakdown,
        "categories_breakdown": categories_breakdown,
    }


def aggregate_shifts(db: Session, shifts: list[CashShift]) -> dict:
    """Suma `total_sales`/`order_count` y recombina `products_breakdown`/
    `categories_breakdown` de varios turnos -- snapshot ya persistido
    (`shift_sales_reports`) si el turno está `closed` y lo tiene, cálculo en
    vivo (`compute_shift_sales_report`) en cualquier otro caso (`open`, o
    `closed` sin fila -- turno anterior a esta spec, research.md D8) -- antes
    de recortar al top-N visible (FR-017: nunca sumar top-N ya recortados).
    Con `shifts=[]` devuelve todos los totales en cero (US4, agregación "sin
    ningún turno abierto")."""
    total_sales = Decimal("0")
    order_count = 0
    is_live = False
    products: dict[str, dict] = {}
    categories: dict[str | None, dict] = {}

    for shift in shifts:
        snapshot = None
        if shift.status == "closed":
            snapshot = db.execute(
                select(ShiftSalesReport).where(ShiftSalesReport.cash_shift_id == shift.id)
            ).scalar_one_or_none()

        if snapshot is not None:
            data = {
                "total_sales": Decimal(snapshot.total_sales),
                "order_count": snapshot.order_count,
                "products_breakdown": snapshot.products_breakdown,
                "categories_breakdown": snapshot.categories_breakdown,
            }
        else:
            is_live = True
            data = compute_shift_sales_report(db, shift)

        total_sales += data["total_sales"]
        order_count += data["order_count"]
        for row in data["products_breakdown"]:
            key = row["product_variant_id"]
            acc = products.setdefault(key, {
                "product_variant_id": key, "name": row["name"],
                "units": 0, "revenue": Decimal("0"),
            })
            acc["units"] += row["units"]
            acc["revenue"] += Decimal(row["revenue"])
        for row in data["categories_breakdown"]:
            key = row["category_id"]
            acc = categories.setdefault(key, {
                "category_id": key, "category_name": row["category_name"],
                "units": 0, "revenue": Decimal("0"),
            })
            acc["units"] += row["units"]
            acc["revenue"] += Decimal(row["revenue"])

    avg_ticket = (
        (total_sales / order_count).quantize(Decimal("0.01"))
        if order_count else Decimal("0.00")
    )
    top_products = sorted(
        products.values(), key=lambda r: r["revenue"], reverse=True
    )[:TOP_PRODUCTS_LIMIT]
    categories_out = sorted(
        categories.values(), key=lambda r: r["revenue"], reverse=True
    )

    return {
        "shift_ids": [s.id for s in shifts],
        "is_live": is_live,
        "total_sales": total_sales,
        "order_count": order_count,
        "avg_ticket": avg_ticket,
        "top_products": top_products,
        "categories": categories_out,
    }


def business_today(tenant) -> date:
    """"Hoy" en la zona horaria del tenant (mismo mecanismo central de
    `app/core/timezone.py` que usa `reports/service.py`)."""
    return utc_now().astimezone(resolve_timezone(tenant)).date()


def _week_bounds(day: date) -> tuple[date, date]:
    """Lunes a domingo de la semana calendario a la que pertenece `day`
    (research.md D4, confirmado con el usuario 2026-10-15 -- NO la ventana
    móvil de 7 días que usa `/dashboard/reports`)."""
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def _month_bounds(day: date) -> tuple[date, date]:
    """Día 1 al último día del mes calendario de `day` (research.md D4)."""
    last_day = calendar.monthrange(day.year, day.month)[1]
    return day.replace(day=1), day.replace(day=last_day)


def resolve_period_bounds(
    tenant, period: str, ref_date: date | None,
    date_from: date | None, date_to: date | None,
) -> tuple[date, date]:
    """`date_from`/`date_to` calendario del período solicitado. Para
    `"range"` se asume que el llamador ya validó que ambos vienen y que
    `date_from <= date_to` (422 en el router)."""
    if period == "day":
        d = ref_date or business_today(tenant)
        return d, d
    if period == "week":
        return _week_bounds(ref_date or business_today(tenant))
    if period == "month":
        return _month_bounds(ref_date or business_today(tenant))
    return date_from, date_to


def _shifts_for_period(
    db: Session, tenant, date_from: date, date_to: date, cash_register_id: UUID | None,
) -> list[CashShift]:
    """Turnos cuya fecha operativa de apertura (zona del tenant) cae en
    `[date_from, date_to]` -- nunca por la hora individual de cada venta
    (RN4/research.md D4)."""
    tz = resolve_timezone(tenant)
    start, _ = local_day_bounds_utc(date_from, tz)
    _, end = local_day_bounds_utc(date_to, tz)
    conds = [CashShift.opened_at >= start, CashShift.opened_at < end]
    if cash_register_id is not None:
        conds.append(CashShift.cash_register_id == cash_register_id)
    return db.execute(
        select(CashShift).where(*conds).order_by(CashShift.opened_at.asc())
    ).scalars().all()


def compute_period_sales_report(
    db: Session, tenant, period: str, *,
    ref_date: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    cash_register_id: UUID | None = None,
) -> dict:
    """Reporte agregado "del día"/semana/mes/rango (FR-005/FR-006/FR-017):
    resuelve el conjunto de turnos por fecha de apertura y recombina su
    desglose completo (`aggregate_shifts`)."""
    bounds_from, bounds_to = resolve_period_bounds(tenant, period, ref_date, date_from, date_to)
    shifts = _shifts_for_period(db, tenant, bounds_from, bounds_to, cash_register_id)
    agg = aggregate_shifts(db, shifts)
    return {
        "period": period,
        "date_from": bounds_from,
        "date_to": bounds_to,
        "cash_register_id": cash_register_id,
        **agg,
    }
