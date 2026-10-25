from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.core.db import get_db
from app.core.dependencies import get_current_user
from app.core.crud import get_or_404, ensure_unique
from app.core.models import User
from app.core.plan_limits import require_module_access
from app.models.unit_measure import UnitMeasure
from app.models.unit_measure_factor_change import UnitMeasureFactorChange
from app.api.v1.unit_measures.schemas import (
    UnitMeasureCreate, UnitMeasureUpdate, UnitMeasureResponse, FactorChangeResponse,
)

router = APIRouter(
    prefix="/unit-measures", tags=["unit_measures"],
    dependencies=[Depends(require_module_access("inventario"))],
)


@router.get(
    "",
    response_model=list[UnitMeasureResponse],
    summary="Listar unidades de medida",
    description="Devuelve todas las unidades de medida. Permite filtrar por estado activo/inactivo.",
    response_description="Lista de unidades de medida.",
    responses={
        401: {"description": "No autenticado o token inválido."},
    },
)
def list_unit_measures(
    active: bool | None = Query(
        None, description="Filtra por estado activo (true) o inactivo (false)."
    ),
    dimension: str | None = Query(
        None, description="Filtra por grupo de medida exacto (spec 134), p. ej. 'Masa'.",
    ),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    query = select(UnitMeasure)
    if active is not None:
        query = query.where(UnitMeasure.active == active)
    if dimension is not None:
        query = query.where(UnitMeasure.dimension == dimension)
    return db.execute(query).scalars().all()


@router.get(
    "/{id}",
    response_model=UnitMeasureResponse,
    summary="Obtener una unidad de medida",
    description="Devuelve una unidad de medida por su identificador único (UUID).",
    response_description="La unidad de medida encontrada.",
    responses={
        401: {"description": "No autenticado o token inválido."},
        404: {"description": "La unidad de medida no existe."},
    },
)
def get_unit_measure(
    id: UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return get_or_404(db, UnitMeasure, id, "Unit measure not found")


@router.post(
    "",
    response_model=UnitMeasureResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear una unidad de medida",
    description="Crea una nueva unidad de medida. La abreviatura debe ser única.",
    response_description="La unidad de medida creada.",
    responses={
        401: {"description": "No autenticado o token inválido."},
        409: {"description": "Ya existe una unidad de medida con esa abreviatura."},
        422: {"description": "Datos de entrada inválidos."},
    },
)
def create_unit_measure(
    body: UnitMeasureCreate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    ensure_unique(db, UnitMeasure, UnitMeasure.abbreviation, body.abbreviation, "Unit measure abbreviation already exists")

    unit_measure = UnitMeasure(
        name=body.name,
        abbreviation=body.abbreviation,
        dimension=body.dimension,
        factor_to_base=body.factor_to_base,
    )
    db.add(unit_measure)
    db.commit()
    db.refresh(unit_measure)
    return unit_measure


@router.patch(
    "/{id}",
    response_model=UnitMeasureResponse,
    summary="Actualizar una unidad de medida",
    description="Actualiza parcialmente una unidad de medida. Solo se modifican los campos enviados.",
    response_description="La unidad de medida actualizada.",
    responses={
        401: {"description": "No autenticado o token inválido."},
        404: {"description": "La unidad de medida no existe."},
        409: {"description": "Ya existe una unidad de medida con esa abreviatura."},
        422: {"description": "Datos de entrada inválidos."},
    },
)
def update_unit_measure(
    id: UUID,
    body: UnitMeasureUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    unit_measure = get_or_404(db, UnitMeasure, id, "Unit measure not found")

    if body.abbreviation is not None and body.abbreviation != unit_measure.abbreviation:
        ensure_unique(db, UnitMeasure, UnitMeasure.abbreviation, body.abbreviation, "Unit measure abbreviation already exists")
        unit_measure.abbreviation = body.abbreviation

    if body.name is not None:
        unit_measure.name = body.name

    if body.dimension is not None:
        unit_measure.dimension = body.dimension

    # Cambiar factor_to_base exige rol ADMIN (spec 134, FR-011a); los demás campos de este
    # mismo endpoint no adquieren esa restricción. Cada cambio queda auditado (FR-011).
    if body.factor_to_base is not None and body.factor_to_base != unit_measure.factor_to_base:
        if not user.role or user.role.name != "ADMIN":
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Tenant admin access required",
            )
        db.add(UnitMeasureFactorChange(
            id=uuid4(),
            unit_measure_id=unit_measure.id,
            old_factor=unit_measure.factor_to_base,
            new_factor=body.factor_to_base,
            changed_by=user.id,
        ))
        unit_measure.factor_to_base = body.factor_to_base

    if body.active is not None:
        unit_measure.active = body.active

    db.commit()
    db.refresh(unit_measure)
    return unit_measure


@router.get(
    "/{id}/factor-changes",
    response_model=list[FactorChangeResponse],
    summary="Historial de cambios de factor_to_base (spec 134)",
    responses={
        401: {"description": "No autenticado o token inválido."},
        404: {"description": "La unidad de medida no existe."},
    },
)
def list_factor_changes(
    id: UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    get_or_404(db, UnitMeasure, id, "Unit measure not found")
    return db.execute(
        select(UnitMeasureFactorChange)
        .where(UnitMeasureFactorChange.unit_measure_id == id)
        .order_by(UnitMeasureFactorChange.changed_at)
    ).scalars().all()


@router.delete(
    "/{id}",
    response_model=UnitMeasureResponse,
    summary="Desactivar una unidad de medida",
    description="Realiza un borrado lógico (soft-delete): marca la unidad de medida como inactiva (active=False) sin eliminarla físicamente.",
    response_description="La unidad de medida desactivada.",
    responses={
        401: {"description": "No autenticado o token inválido."},
        404: {"description": "La unidad de medida no existe."},
    },
)
def delete_unit_measure(
    id: UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    unit_measure = get_or_404(db, UnitMeasure, id, "Unit measure not found")
    unit_measure.active = False
    db.commit()
    db.refresh(unit_measure)
    return unit_measure
