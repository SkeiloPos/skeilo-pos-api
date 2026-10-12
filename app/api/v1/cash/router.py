from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
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
from app.api.v1.cash import service
from app.api.v1.cash.schemas import (
    RegisterCreate, RegisterResponse,
    ShiftOpen, ShiftClose, ShiftResponse,
    CashMovementIn, CashMovementResponse,
    ReconciliationResponse, ShiftReportResponse,
    DenominationIn,
    ShiftSummaryResponse,
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
def close_shift(shift_id: UUID, body: ShiftClose, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    shift = get_or_404(db, CashShift, shift_id, "Shift not found")
    if shift.status == "closed":
        raise HTTPException(status.HTTP_409_CONFLICT, "El turno ya está cerrado")

    # counted = Σ(denominación*cantidad) si se envían denominaciones; si no, el valor enviado.
    if body.denominations:
        counted = sum((d.denomination * d.quantity for d in body.denominations), start=0)
    else:
        counted = body.counted_amount

    shift.counted_amount = counted

    # difference != 0 exige observación (close_note).
    if counted is not None:
        recon = service.reconcile(db, shift)
        if recon["difference"] != 0 and not (body.close_note and body.close_note.strip()):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "El arqueo no cuadra: la observación (close_note) es obligatoria",
            )

    if body.denominations:
        for d in body.denominations:
            db.add(CashCountDenomination(
                cash_shift_id=shift.id, denomination=d.denomination, quantity=d.quantity
            ))

    shift.close_note = body.close_note
    shift.status = "closed"
    shift.closed_at = utc_now()
    db.commit()
    db.refresh(shift)
    return shift


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
