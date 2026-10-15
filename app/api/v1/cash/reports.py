"""Reporte operativo de ventas por turno de caja (spec 119, research.md D5).

Vive en `cash`, no en `app/api/v1/reports/`: ese paquete atribuye por fecha
calendario de `Sale.sold_at` (criterio que la spec 119 declara intacto,
Clarifications) — este módulo atribuye por `Sale.cash_shift_id`, el vínculo
autoritativo venta↔turno (research.md D3).

Un turno cerrado tiene un snapshot inmutable (cabecera `ShiftSalesReport` +
filas de producto/categoría) escrito UNA sola vez al cerrar; uno abierto, o
cerrado antes de esta spec (D8), se calcula EN VIVO desde las ventas. La rama
en vivo es una única definición (`_live_*`) que usan tanto el cierre
(`compute_shift_sales_report`) como la agregación de período: el valor
congelado y el último valor visible en vivo no pueden divergir.

US6 (FR-018 a FR-023, research.md D10/D11): `aggregate_shifts` arma el reporte
con CUATRO consultas fijas — sea cual sea el número de turnos — que reciben un
*selector de turnos* (condiciones sobre `cash_shifts`) y unen con `UNION ALL`
las filas congeladas con el cálculo en vivo. La recombinación, el nombre
mostrado, el orden y el `LIMIT` se resuelven en la base de datos, con SQL
estándar que corre igual en PostgreSQL y en SQLite (la suite): ninguna función
exclusiva de PostgreSQL (T068 lo comprueba con una búsqueda de texto).
"""
import calendar
from datetime import date, timedelta
from decimal import Decimal
from typing import Sequence
from uuid import UUID

from sqlalchemy import case, func, insert, select, union_all
from sqlalchemy.orm import Session
from sqlalchemy.sql import ColumnElement, Select

from app.core.timezone import local_day_bounds_utc, resolve_timezone, utc_now
from app.models.cash_shift import CashShift
from app.models.category import Category
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.sale import Sale, SaleItem
from app.models.shift_sales_report import (
    ShiftSalesReport, ShiftSalesReportCategory, ShiftSalesReportProduct,
)

# research.md D5, confirmado con el usuario 2026-10-15 -- mismo valor que ya
# usa el frontend para GET /reports/top-products.
TOP_PRODUCTS_LIMIT = 10

NO_CATEGORY_NAME = "Sin categoría"

# Selector de turnos: condiciones sobre `cash_shifts` que cada consulta repite
# (research.md D10) -- sin listas de ids en los parámetros.
ShiftSelector = Sequence[ColumnElement[bool]]


# ─── Rama EN VIVO: una sola definición (research.md D10) ─────────────────────
def _scope_live(q: Select, selector: ShiftSelector, only_unfrozen: bool) -> Select:
    """Ventas pagadas de los turnos del selector; con `only_unfrozen`, solo las
    de los turnos SIN cabecera de snapshot (abiertos o anteriores a la spec, D8)."""
    if only_unfrozen:
        q = q.outerjoin(
            ShiftSalesReport, ShiftSalesReport.cash_shift_id == CashShift.id
        ).where(ShiftSalesReport.id.is_(None))
    return q.where(Sale.status == "paid", *selector)


def _live_totals(selector: ShiftSelector, *, only_unfrozen: bool) -> Select:
    q = (
        select(
            func.coalesce(func.sum(Sale.total), 0).label("total_sales"),
            func.count(Sale.id).label("order_count"),
        )
        .select_from(Sale)
        .join(CashShift, CashShift.id == Sale.cash_shift_id)
    )
    return _scope_live(q, selector, only_unfrozen)


def _live_products(selector: ShiftSelector, *, only_unfrozen: bool) -> Select:
    """(variante, turno) -> nombre (`MAX(description)` del turno), unidades, ingreso."""
    q = (
        select(
            SaleItem.product_variant_id.label("variant_id"),
            func.max(SaleItem.description).label("name"),
            CashShift.opened_at.label("opened_at"),
            CashShift.id.label("shift_id"),
            func.sum(SaleItem.quantity).label("units"),
            func.sum(SaleItem.line_total).label("revenue"),
        )
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .join(CashShift, CashShift.id == Sale.cash_shift_id)
    )
    return _scope_live(q, selector, only_unfrozen).group_by(
        SaleItem.product_variant_id, CashShift.id, CashShift.opened_at
    )


def _live_categories(selector: ShiftSelector, *, only_unfrozen: bool) -> Select:
    """(categoría, turno) -> nombre, unidades, ingreso. LEFT JOIN + "Sin
    categoría" (data-model.md §2): `Product.category_id` es NOT NULL hoy, así
    que esa rama es defensiva, no un comportamiento observado en el catálogo."""
    q = (
        select(
            Category.id.label("category_id"),
            func.coalesce(Category.name, NO_CATEGORY_NAME).label("category_name"),
            CashShift.opened_at.label("opened_at"),
            CashShift.id.label("shift_id"),
            func.sum(SaleItem.quantity).label("units"),
            func.sum(SaleItem.line_total).label("revenue"),
        )
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .join(CashShift, CashShift.id == Sale.cash_shift_id)
        .join(ProductVariant, ProductVariant.id == SaleItem.product_variant_id)
        .join(Product, Product.id == ProductVariant.product_id)
        .outerjoin(Category, Category.id == Product.category_id)
    )
    return _scope_live(q, selector, only_unfrozen).group_by(
        Category.id, Category.name, CashShift.id, CashShift.opened_at
    )


# ─── Rama CONGELADA: filas del snapshot de los turnos con cabecera ───────────
# Se une siempre con la cabecera: unas filas hijas huérfanas se ignoran.
def _frozen_totals(selector: ShiftSelector) -> Select:
    return (
        select(
            func.coalesce(func.sum(ShiftSalesReport.total_sales), 0).label("total_sales"),
            func.coalesce(func.sum(ShiftSalesReport.order_count), 0).label("order_count"),
        )
        .select_from(ShiftSalesReport)
        .join(CashShift, CashShift.id == ShiftSalesReport.cash_shift_id)
        .where(*selector)
    )


def _frozen_products(selector: ShiftSelector) -> Select:
    return (
        select(
            ShiftSalesReportProduct.product_variant_id.label("variant_id"),
            ShiftSalesReportProduct.name.label("name"),
            CashShift.opened_at.label("opened_at"),
            CashShift.id.label("shift_id"),
            ShiftSalesReportProduct.units.label("units"),
            ShiftSalesReportProduct.revenue.label("revenue"),
        )
        .select_from(ShiftSalesReportProduct)
        .join(CashShift, CashShift.id == ShiftSalesReportProduct.cash_shift_id)
        .join(ShiftSalesReport, ShiftSalesReport.cash_shift_id == CashShift.id)
        .where(*selector)
    )


def _frozen_categories(selector: ShiftSelector) -> Select:
    return (
        select(
            ShiftSalesReportCategory.category_id.label("category_id"),
            ShiftSalesReportCategory.category_name.label("category_name"),
            CashShift.opened_at.label("opened_at"),
            CashShift.id.label("shift_id"),
            ShiftSalesReportCategory.units.label("units"),
            ShiftSalesReportCategory.revenue.label("revenue"),
        )
        .select_from(ShiftSalesReportCategory)
        .join(CashShift, CashShift.id == ShiftSalesReportCategory.cash_shift_id)
        .join(ShiftSalesReport, ShiftSalesReport.cash_shift_id == CashShift.id)
        .where(*selector)
    )


def _avg_ticket(total_sales: Decimal, order_count: int) -> Decimal:
    """Ticket promedio calculado en Python: `Decimal.quantize` es half-even y el
    `ROUND` de SQL es half-away-from-zero; difieren justo en los empates de medio
    centavo y romperían "al centavo" (research.md D11)."""
    if not order_count:
        return Decimal("0.00")
    return (total_sales / order_count).quantize(Decimal("0.01"))


def compute_shift_sales_report(db: Session, shift: CashShift) -> dict:
    """Reporte de UN turno calculado EN VIVO desde sus ventas pagadas -- el que
    `close_shift` congela (`persist_shift_snapshot`) y el de un turno abierto.
    Usa la misma definición `_live_*` que la agregación de período. Devuelve el
    desglose COMPLETO (no top-N, FR-017) ordenado por ingreso descendente; el
    recorte al top-N visible lo hace la agregación."""
    selector = [CashShift.id == shift.id]

    total_sales, order_count = db.execute(_live_totals(selector, only_unfrozen=False)).one()
    total_sales = Decimal(total_sales)

    products = _live_products(selector, only_unfrozen=False)
    product_rows = db.execute(products.order_by(
        func.sum(SaleItem.line_total).desc(), func.max(SaleItem.description).asc(),
        SaleItem.product_variant_id.asc(),
    )).all()
    products_breakdown = [
        {
            "product_variant_id": str(vid), "name": name,
            "units": int(units), "revenue": str(Decimal(rev)),
        }
        for vid, name, _opened_at, _shift_id, units, rev in product_rows
    ]

    categories = _live_categories(selector, only_unfrozen=False)
    category_rows = db.execute(categories.order_by(
        func.sum(SaleItem.line_total).desc(), Category.name.asc(), Category.id.asc().nulls_last(),
    )).all()
    categories_breakdown = [
        {
            "category_id": str(cid) if cid else None,
            "category_name": cname,
            "units": int(units), "revenue": str(Decimal(rev)),
        }
        for cid, cname, _opened_at, _shift_id, units, rev in category_rows
    ]

    return {
        "total_sales": total_sales,
        "order_count": order_count,
        "avg_ticket": _avg_ticket(total_sales, order_count),
        "products_breakdown": products_breakdown,
        "categories_breakdown": categories_breakdown,
    }


def persist_shift_snapshot(db: Session, shift: CashShift, data: dict) -> None:
    """Escribe el snapshot inmutable del turno (FR-013, research.md D9/D15):
    la cabecera con los totales y el desglose COMPLETO (no top-N) como filas
    de las dos tablas hijas. `data` es la salida de `compute_shift_sales_report`.

    Una inserción masiva por tabla hija -- el número de sentencias no depende
    de cuántos productos se vendieron (data-model.md §2.4). No hace `commit`:
    va dentro de la transacción de `close_shift`."""
    db.add(ShiftSalesReport(
        cash_shift_id=shift.id,
        total_sales=data["total_sales"],
        order_count=data["order_count"],
        avg_ticket=data["avg_ticket"],
    ))
    if data["products_breakdown"]:
        db.execute(insert(ShiftSalesReportProduct), [
            {
                "cash_shift_id": shift.id,
                "product_variant_id": UUID(row["product_variant_id"]),
                "name": row["name"],
                "units": row["units"],
                "revenue": Decimal(row["revenue"]),
            }
            for row in data["products_breakdown"]
        ])
    if data["categories_breakdown"]:
        db.execute(insert(ShiftSalesReportCategory), [
            {
                "cash_shift_id": shift.id,
                "category_id": UUID(row["category_id"]) if row["category_id"] else None,
                "category_name": row["category_name"],
                "units": row["units"],
                "revenue": Decimal(row["revenue"]),
            }
            for row in data["categories_breakdown"]
        ])


def aggregate_shifts(db: Session, selector: ShiftSelector) -> dict:
    """Reporte agregado de los turnos que cumplen `selector` (condiciones sobre
    `cash_shifts`): CUATRO consultas fijas, sea cual sea el número de turnos
    (FR-018, research.md D10).

    1. turnos incluidos (con `LEFT JOIN` a la cabecera) -> `shift_ids`, `is_live`;
    2. totales: cabeceras `UNION ALL` ventas pagadas de los turnos sin cabecera;
    3. productos: filas congeladas `UNION ALL` cálculo en vivo -> nombre del turno
       más antiguo, suma, orden y `LIMIT` en la base de datos (FR-019);
    4. categorías: igual, sin `LIMIT`.

    Un turno tiene snapshot si y solo si existe su fila de cabecera (D9). Con cero
    turnos solo se ejecuta la primera consulta."""
    shift_rows = db.execute(
        select(CashShift.id, ShiftSalesReport.id)
        .select_from(CashShift)
        .outerjoin(ShiftSalesReport, ShiftSalesReport.cash_shift_id == CashShift.id)
        .where(*selector)
        .order_by(CashShift.opened_at.asc(), CashShift.id.asc())
    ).all()
    shift_ids = [row[0] for row in shift_rows]
    is_live = any(row[1] is None for row in shift_rows)

    if not shift_ids:
        return {
            "shift_ids": [], "is_live": False,
            "total_sales": Decimal("0"), "order_count": 0, "avg_ticket": Decimal("0.00"),
            "top_products": [], "categories": [],
        }

    total_rows = db.execute(
        union_all(_frozen_totals(selector), _live_totals(selector, only_unfrozen=True))
    ).all()
    # `is not None` y no `or 0`: `Decimal("0.00")` es falsy y perdería su escala
    # (el contrato devuelve "0.00" para un turno cerrado sin ventas).
    total_sales = sum((Decimal(r[0]) for r in total_rows if r[0] is not None), Decimal("0"))
    order_count = sum(int(r[1]) for r in total_rows if r[1] is not None)

    return {
        "shift_ids": shift_ids,
        "is_live": is_live,
        "total_sales": total_sales,
        "order_count": order_count,
        "avg_ticket": _avg_ticket(total_sales, order_count),
        "top_products": _top_products(db, selector),
        "categories": _category_breakdown(db, selector),
    }


def _top_products(db: Session, selector: ShiftSelector) -> list[dict]:
    parts = union_all(
        _frozen_products(selector), _live_products(selector, only_unfrozen=True)
    ).subquery("parts")
    # El nombre mostrado es el del turno más antiguo donde aparece la variante
    # (FR-022); función de ventana estándar, no una exclusiva de PostgreSQL: este SQL
    # corre también en SQLite.
    ranked = select(
        parts.c.variant_id, parts.c.name, parts.c.units, parts.c.revenue,
        func.row_number().over(
            partition_by=parts.c.variant_id, order_by=(parts.c.opened_at, parts.c.shift_id)
        ).label("rn"),
    ).subquery("ranked")
    name = func.max(case((ranked.c.rn == 1, ranked.c.name)))
    revenue = func.sum(ranked.c.revenue)
    rows = db.execute(
        select(ranked.c.variant_id, name, func.sum(ranked.c.units), revenue)
        .group_by(ranked.c.variant_id)
        .order_by(revenue.desc(), name.asc(), ranked.c.variant_id.asc())
        .limit(TOP_PRODUCTS_LIMIT)
    ).all()
    return [
        {
            "product_variant_id": str(vid), "name": nm,
            "units": int(units), "revenue": Decimal(rev),
        }
        for vid, nm, units, rev in rows
    ]


def _category_breakdown(db: Session, selector: ShiftSelector) -> list[dict]:
    parts = union_all(
        _frozen_categories(selector), _live_categories(selector, only_unfrozen=True)
    ).subquery("parts")
    ranked = select(
        parts.c.category_id, parts.c.category_name, parts.c.units, parts.c.revenue,
        func.row_number().over(
            partition_by=parts.c.category_id, order_by=(parts.c.opened_at, parts.c.shift_id)
        ).label("rn"),
    ).subquery("ranked")
    name = func.max(case((ranked.c.rn == 1, ranked.c.category_name)))
    revenue = func.sum(ranked.c.revenue)
    rows = db.execute(
        select(ranked.c.category_id, name, func.sum(ranked.c.units), revenue)
        .group_by(ranked.c.category_id)
        # `NULLS LAST` explícito: PostgreSQL y SQLite difieren en su valor por defecto.
        .order_by(revenue.desc(), name.asc(), ranked.c.category_id.asc().nulls_last())
    ).all()
    return [
        {
            "category_id": str(cid) if cid else None, "category_name": nm,
            "units": int(units), "revenue": Decimal(rev),
        }
        for cid, nm, units, rev in rows
    ]


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


def period_selector(
    tenant, date_from: date, date_to: date, cash_register_id: UUID | None,
) -> list[ColumnElement[bool]]:
    """Selector de los turnos cuya fecha operativa de apertura (zona del tenant)
    cae en `[date_from, date_to]` -- nunca por la hora individual de cada venta
    (RN4/research.md D4)."""
    tz = resolve_timezone(tenant)
    start, _ = local_day_bounds_utc(date_from, tz)
    _, end = local_day_bounds_utc(date_to, tz)
    conds = [CashShift.opened_at >= start, CashShift.opened_at < end]
    if cash_register_id is not None:
        conds.append(CashShift.cash_register_id == cash_register_id)
    return conds


def shift_selector(shift_id: UUID) -> list[ColumnElement[bool]]:
    """Selector de un turno puntual."""
    return [CashShift.id == shift_id]


def open_shifts_selector(cash_register_id: UUID | None) -> list[ColumnElement[bool]]:
    """Selector de los turnos abiertos ("turno actual"), opcionalmente de una caja."""
    conds = [CashShift.status == "open"]
    if cash_register_id is not None:
        conds.append(CashShift.cash_register_id == cash_register_id)
    return conds


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
    agg = aggregate_shifts(db, period_selector(tenant, bounds_from, bounds_to, cash_register_id))
    return {
        "period": period,
        "date_from": bounds_from,
        "date_to": bounds_to,
        "cash_register_id": cash_register_id,
        **agg,
    }
