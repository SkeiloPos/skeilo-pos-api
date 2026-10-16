from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.core.db import get_db, get_tenant
from app.core.crud import get_or_404, ensure_unique
from app.core.dependencies import get_current_user, require_tenant_admin
from app.core.models import Tenant, User
from app.core.pagination import Page
from app.core.plan_limits import enforce_plan_limit
from app.core.timezone import utc_now
from app.models.cash_register import CashRegister
from app.models.cash_shift import CashShift
from app.models.cash_movement import CashMovement
from app.models.cash_count_denomination import CashCountDenomination
from app.api.v1.cash import service, reports, active_orders, denominations
from app.api.v1.cash.schemas import (
    RegisterCreate, RegisterResponse,
    ShiftOpen, ShiftClose, ShiftResponse,
    CashMovementIn, CashMovementResponse,
    ReconciliationResponse, ShiftReportResponse,
    DenominationIn,
    ShiftSummaryResponse,
    ShiftSalesReportResponse, PeriodSalesReportResponse, ActiveOrderSummary,
)

router = APIRouter(prefix="/cash", tags=["cash"])


# ============================ Cajas ============================
@router.get("/registers", response_model=list[RegisterResponse], summary="Listar cajas")
def list_registers(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return db.execute(select(CashRegister).order_by(CashRegister.name)).scalars().all()


@router.post("/registers", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED, summary="Crear caja")
def create_register(
    body: RegisterCreate,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_tenant),
    _: User = Depends(require_tenant_admin),
):
    ensure_unique(db, CashRegister, CashRegister.name, body.name, "Register name already exists")
    enforce_plan_limit(db, tenant, "cajas")  # spec 033, FR-005/FR-006
    reg = CashRegister(name=body.name)
    db.add(reg)
    db.commit()
    db.refresh(reg)
    return reg


# ============================ Turnos ============================
@router.get("/shifts", response_model=Page[ShiftSummaryResponse],
            summary="Histórico de turnos (cierres) — gestión admin")
def list_shifts(
    status: str | None = Query("closed", description="Filtra por estado ('closed' | 'open')."),
    cash_register_id: UUID | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(require_tenant_admin),
):
    return service.list_shifts(db, status, cash_register_id, page, size)


@router.post("/shifts/open", response_model=ShiftResponse, status_code=status.HTTP_201_CREATED, summary="Abrir turno de caja")
def open_shift(body: ShiftOpen, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    get_or_404(db, CashRegister, body.cash_register_id, "Register not found")
    if body.opening_amount <= 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "El saldo inicial es obligatorio y debe ser mayor a cero",
        )
    shift = CashShift(
        cash_register_id=body.cash_register_id,
        user_id=user.id,
        user_name=user.name,
        opening_amount=body.opening_amount,
        status="open",
    )
    db.add(shift)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "La caja ya tiene un turno abierto")
    db.refresh(shift)
    return shift


@router.get("/shifts/current", response_model=ShiftResponse, summary="Turno abierto actual de una caja")
def current_shift(cash_register_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    shift = service.get_open_shift(db, cash_register_id)
    if shift is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "La caja no tiene un turno abierto")
    return shift


@router.post("/shifts/{shift_id}/close", response_model=ShiftResponse, summary="Cerrar turno (arqueo)")
def close_shift(
    shift_id: UUID, body: ShiftClose, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # spec 121 (D-1, A-130, FR-002): ningún valor monetario del cliente. Va PRIMERO,
    # antes de leer el turno y antes del bloqueo por pedidos activos: la causa más
    # probable es una pantalla desactualizada y ahí lo útil es decir "recarga".
    denominations.reject_client_totals(body)

    # spec 121 (FR-019, R5): `SELECT ... FOR UPDATE` serializa los cierres
    # simultáneos del mismo turno (cierra la ventana de carrera A-17 en este punto).
    shift = service.get_shift_for_update(db, shift_id)
    if shift.status == "closed":
        raise HTTPException(status.HTTP_409_CONFLICT, "El turno ya está cerrado")

    # spec 119 (FR-007/FR-008/FR-009, A-129): no se permite cerrar ningún
    # turno mientras existan pedidos activos sin cobrar en TODO el tenant
    # (research.md D1) -- se re-evalúa contra el estado vigente en cada
    # intento, sin caché de un intento anterior (contracts/active-orders-
    # close-gate-api.md). El cuerpo del 409 es plano (sin envolver en otro
    # "detail") para que el frontend lea `active_orders` directo.
    # US6 (FR-023, research.md D12): se cuenta primero con un COUNT y la lista
    # solo se trae si hay algo que mostrar. Si entre el conteo y la lista el
    # pedido se resolvió (lista vacía), el cierre continúa con el estado vigente.
    active = (
        active_orders.list_active_orders(db) if active_orders.count_active_orders(db) else []
    )
    if active:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": (
                    f"No se puede cerrar el turno: hay {len(active)} "
                    "pedidos activos sin cobrar."
                ),
                "active_orders": [
                    ActiveOrderSummary.model_validate(o).model_dump(mode="json")
                    for o in active
                ],
            },
        )

    # spec 121 (D-1/D-2, A-130/A-131): el efectivo contado lo calcula SOLO el
    # servidor como Σ(denominación × cantidad) de las líneas validadas contra el
    # catálogo del tenant. Ya no existe el cierre "sin conteo" (RN-CASH-09, A-20):
    # sin líneas el contado es $0 y, si hay esperado, la diferencia exige observación.
    lines = denominations.resolve_count_lines(db, body.denominations)
    counted = denominations.total_of(lines)

    shift.counted_amount = counted

    # difference != 0 exige observación (close_note).
    recon = service.reconcile(db, shift)
    if recon["difference"] != 0 and not (body.close_note and body.close_note.strip()):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "El arqueo no cuadra: la observación (close_note) es obligatoria",
        )

    # spec 121 (D-3, D-4): una fila por línea con cantidad > 0, con su tipo (snapshot).
    for line in lines:
        db.add(CashCountDenomination(
            cash_shift_id=shift.id, denomination=line.denomination,
            kind=line.kind, quantity=line.quantity,
        ))

    shift.close_note = body.close_note
    shift.status = "closed"
    shift.closed_at = utc_now()
    # spec 121 (D-5, A-134, FR-029): quién confirma el conteo (puede no ser quien abrió).
    shift.closed_by_user_id = user.id
    shift.closed_by_user_name = user.name

    # spec 119 (FR-013, research.md D2): snapshot inmutable del reporte
    # operativo de ventas, calculado una última vez y persistido dentro de
    # la MISMA transacción de cierre -- si el cómputo fallara, todo el
    # cierre revierte (nunca un turno 'closed' sin su reporte).
    report_data = reports.compute_shift_sales_report(db, shift)
    reports.persist_shift_snapshot(db, shift, report_data)

    # spec 121 (FR-019): si aun así dos cierres chocaran contra el índice único
    # (cash_shift_id, denomination), el perdedor recibe 409, nunca un 500.
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "El turno ya está cerrado")
    db.refresh(shift)
    return shift


# ============================ Reporte operativo por turno (spec 119) ============================
@router.get("/shifts/{shift_id}/sales-report", response_model=ShiftSalesReportResponse,
            summary="Reporte operativo de ventas de un turno puntual")
def shift_sales_report(
    shift_id: UUID, db: Session = Depends(get_db), _: User = Depends(require_tenant_admin),
):
    shift = get_or_404(db, CashShift, shift_id, "Shift not found")
    agg = reports.aggregate_shifts(db, reports.shift_selector(shift.id))
    register_name = db.execute(
        select(CashRegister.name).where(CashRegister.id == shift.cash_register_id)
    ).scalar_one_or_none() or "—"

    # Solo relevante cuando el turno sigue abierto (FR-012) -- tenant-wide,
    # igual que el propio bloqueo de cierre (research.md D1).
    active_orders_count = (
        active_orders.count_active_orders(db) if shift.status == "open" else None
    )

    return {
        "shift_id": shift.id,
        "cash_register_id": shift.cash_register_id,
        "cash_register_name": register_name,
        "status": shift.status,
        "is_live": agg["is_live"],
        "opened_at": shift.opened_at,
        "closed_at": shift.closed_at,
        "total_sales": agg["total_sales"],
        "order_count": agg["order_count"],
        "avg_ticket": agg["avg_ticket"],
        "top_products": agg["top_products"],
        "categories": agg["categories"],
        "active_orders_count": active_orders_count,
    }


@router.get("/reports/current", response_model=PeriodSalesReportResponse,
            summary="Reporte agregado del/de los turno(s) actualmente abiertos")
def current_shift_report(
    cash_register_id: UUID | None = Query(None),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_tenant),
    _: User = Depends(require_tenant_admin),
):
    if cash_register_id is not None:
        get_or_404(db, CashRegister, cash_register_id, "Register not found")
        if service.get_open_shift(db, cash_register_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "La caja no tiene un turno abierto")
    # Sin filtro de caja: agrega TODOS los turnos abiertos del tenant, incluido el
    # caso de cero turnos abiertos (200 con totales en cero, no 404 --
    # contracts/shift-sales-report-api.md).
    agg = reports.aggregate_shifts(db, reports.open_shifts_selector(cash_register_id))
    today = reports.business_today(tenant)
    return {
        "period": "current_shift",
        "date_from": today,
        "date_to": today,
        "cash_register_id": cash_register_id,
        **agg,
    }


@router.get("/reports/period", response_model=PeriodSalesReportResponse,
            summary="Reporte agregado por día, semana, mes o rango personalizado")
def period_sales_report(
    period: Literal["day", "week", "month", "range"] = Query(...),
    date: date | None = Query(None, description="Día de referencia (day/week/month)."),
    date_from: date | None = Query(None, description="Obligatorio si period='range'."),
    date_to: date | None = Query(None, description="Obligatorio si period='range'."),
    cash_register_id: UUID | None = Query(None),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_tenant),
    _: User = Depends(require_tenant_admin),
):
    if period == "range":
        if date_from is None or date_to is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "date_from y date_to son obligatorios cuando period='range'",
            )
        if date_from > date_to:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "date_from no puede ser posterior a date_to",
            )
    if cash_register_id is not None:
        get_or_404(db, CashRegister, cash_register_id, "Register not found")

    return reports.compute_period_sales_report(
        db, tenant, period, ref_date=date, date_from=date_from, date_to=date_to,
        cash_register_id=cash_register_id,
    )


# ============================ Pedidos activos / bloqueo de cierre (spec 119) ============================
@router.get("/active-orders", response_model=list[ActiveOrderSummary],
            summary="Pedidos activos que bloquean cualquier intento de cierre")
def list_active_orders_endpoint(
    db: Session = Depends(get_db), _: User = Depends(get_current_user),
):
    return active_orders.list_active_orders(db)


@router.get("/shifts/{shift_id}/reconciliation", response_model=ReconciliationResponse, summary="Arqueo del turno")
def reconciliation(shift_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    shift = get_or_404(db, CashShift, shift_id, "Shift not found")
    return service.reconcile(db, shift)


@router.get("/shifts/{shift_id}/report", response_model=ShiftReportResponse, summary="Reporte de cierre consolidado")
def shift_report(shift_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    shift = get_or_404(db, CashShift, shift_id, "Shift not found")
    movements = db.execute(
        select(CashMovement)
        .where(CashMovement.cash_shift_id == shift.id)
        .order_by(CashMovement.occurred_at.desc())
    ).scalars().all()
    denominations = db.execute(
        select(CashCountDenomination)
        .where(CashCountDenomination.cash_shift_id == shift.id)
        .order_by(CashCountDenomination.denomination.desc())
    ).scalars().all()
    return {
        "shift": shift,
        "reconciliation": service.reconcile(db, shift),
        "movements": movements,
        "denominations": [
            DenominationIn(denomination=d.denomination, quantity=d.quantity)
            for d in denominations
        ],
        "close_note": shift.close_note,
    }


# ============================ Movimientos de efectivo ============================
@router.get("/shifts/{shift_id}/movements", response_model=list[CashMovementResponse], summary="Listar movimientos del turno")
def list_movements(shift_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    get_or_404(db, CashShift, shift_id, "Shift not found")
    return db.execute(
        select(CashMovement)
        .where(CashMovement.cash_shift_id == shift_id)
        .order_by(CashMovement.occurred_at.desc())
    ).scalars().all()


@router.post("/shifts/{shift_id}/movements", response_model=CashMovementResponse, status_code=status.HTTP_201_CREATED, summary="Registrar ingreso/egreso/retiro de efectivo")
def add_movement(shift_id: UUID, body: CashMovementIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    shift = get_or_404(db, CashShift, shift_id, "Shift not found")
    if shift.status != "open":
        raise HTTPException(status.HTTP_409_CONFLICT, "El turno está cerrado")
    mov = CashMovement(
        cash_shift_id=shift.id, kind=body.kind.value, amount=body.amount,
        category=body.category, description=body.description,
        user_id=user.id, user_name=user.name,
    )
    db.add(mov)
    db.commit()
    db.refresh(mov)
    return mov
