from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.core.db import get_db
from app.core.dependencies import get_current_user
from app.core.crud import get_or_404, ensure_unique
from app.core.models import User
from app.core.plan_limits import require_module_access
from app.models.unit_measure import UnitMeasure
from app.models.unit_measure_group import UnitMeasureGroup
from app.api.v1.unit_measures.service import unit_measure_in_use
from app.api.v1.unit_measures.schemas import UnitMeasureResponse
from app.api.v1.unit_measure_groups.schemas import (
    UnitMeasureGroupCreate, UnitMeasureGroupAddUnit, UnitMeasureGroupRename,
    UnitMeasureGroupBaseReassign, UnitMeasureGroupResponse,
)

router = APIRouter(
    prefix="/unit-measure-groups", tags=["unit_measure_groups"],
    dependencies=[Depends(require_module_access("inventario"))],
)


def _blocking_units_conflict(units: list[UnitMeasure]) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "error": "No se puede completar la acción: hay unidades en uso.",
            "blocking_units": [
                {"id": str(u.id), "name": u.name, "abbreviation": u.abbreviation}
                for u in units
            ],
        },
    )


def _ensure_unit_fields_unique(db: Session, name: str, abbreviation: str) -> None:
    ensure_unique(db, UnitMeasure, UnitMeasure.name, name, "Unit measure name already exists")
    ensure_unique(db, UnitMeasure, UnitMeasure.abbreviation, abbreviation, "Unit measure abbreviation already exists")


@router.get(
    "",
    response_model=list[UnitMeasureGroupResponse],
    summary="Listar grupos de unidades de medida",
    responses={401: {"description": "No autenticado o token inválido."}},
)
def list_unit_measure_groups(
    active: bool | None = Query(
        None, description="Filtra grupos que tengan al menos una unidad con ese estado.",
    ),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    groups = db.execute(select(UnitMeasureGroup)).scalars().all()
    if active is None:
        return groups
    return [g for g in groups if any(u.active == active for u in g.units)]


@router.post(
    "",
    response_model=UnitMeasureGroupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear un grupo con su unidad base",
    responses={
        401: {"description": "No autenticado o token inválido."},
        409: {"description": "Nombre de grupo o de unidad duplicado."},
        422: {"description": "Datos de entrada inválidos."},
    },
)
def create_unit_measure_group(
    body: UnitMeasureGroupCreate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    ensure_unique(db, UnitMeasureGroup, UnitMeasureGroup.name, body.name, "Unit measure group name already exists")
    _ensure_unit_fields_unique(db, body.base_unit.name, body.base_unit.abbreviation)

    group = UnitMeasureGroup(name=body.name)
    db.add(group)
    db.flush()

    base_unit = UnitMeasure(
        name=body.base_unit.name,
        abbreviation=body.base_unit.abbreviation,
        dimension=group.name,
        factor_to_base=1,
        group_id=group.id,
        is_base=True,
    )
    db.add(base_unit)
    db.commit()
    db.refresh(group)
    return group


@router.post(
    "/{id}/units",
    response_model=UnitMeasureResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Agregar una unidad adicional a un grupo existente",
    responses={
        401: {"description": "No autenticado o token inválido."},
        404: {"description": "El grupo no existe."},
        409: {"description": "Nombre o abreviatura de unidad duplicado."},
        422: {"description": "Datos de entrada inválidos."},
    },
)
def add_unit_to_group(
    id: UUID,
    body: UnitMeasureGroupAddUnit,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    group = get_or_404(db, UnitMeasureGroup, id, "Unit measure group not found")
    _ensure_unit_fields_unique(db, body.name, body.abbreviation)

    unit = UnitMeasure(
        name=body.name,
        abbreviation=body.abbreviation,
        dimension=group.name,
        factor_to_base=body.factor_to_base,
        group_id=group.id,
        is_base=False,
    )
    db.add(unit)
    db.commit()
    db.refresh(unit)
    return unit


@router.patch(
    "/{id}",
    response_model=UnitMeasureGroupResponse,
    summary="Renombrar un grupo (sincroniza dimension de sus unidades)",
    responses={
        401: {"description": "No autenticado o token inválido."},
        404: {"description": "El grupo no existe."},
        409: {"description": "Nombre de grupo duplicado."},
    },
)
def rename_unit_measure_group(
    id: UUID,
    body: UnitMeasureGroupRename,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    group = get_or_404(db, UnitMeasureGroup, id, "Unit measure group not found")
    if body.name != group.name:
        ensure_unique(
            db, UnitMeasureGroup, UnitMeasureGroup.name, body.name,
            "Unit measure group name already exists", exclude_id=group.id,
        )
        group.name = body.name
        for unit in group.units:
            unit.dimension = body.name
    db.commit()
    db.refresh(group)
    return group


@router.patch(
    "/{id}/base-unit",
    response_model=UnitMeasureGroupResponse,
    summary="Reasignar la unidad base del grupo",
    responses={
        401: {"description": "No autenticado o token inválido."},
        404: {"description": "El grupo o la unidad no existen, o la unidad no pertenece al grupo."},
        409: {"description": "Alguna unidad del grupo está en uso."},
    },
)
def reassign_base_unit(
    id: UUID,
    body: UnitMeasureGroupBaseReassign,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    group = get_or_404(db, UnitMeasureGroup, id, "Unit measure group not found")
    new_base = next((u for u in group.units if u.id == body.unit_measure_id), None)
    if new_base is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit measure not found in this group")

    blocking = [u for u in group.units if unit_measure_in_use(db, u.id)]
    if blocking:
        raise _blocking_units_conflict(blocking)

    if not new_base.is_base:
        new_base_factor = new_base.factor_to_base
        for unit in group.units:
            if unit.id == new_base.id:
                continue
            unit.factor_to_base = unit.factor_to_base / new_base_factor
            unit.is_base = False
        # Flush con la base saliente ya en is_base=False antes de marcar la nueva base:
        # el índice único parcial `uq__unit_measures__group_id__is_base_true` no es
        # diferible, así que ambas filas no pueden tener is_base=true en el mismo UPDATE.
        db.flush()
        new_base.is_base = True
        new_base.factor_to_base = 1

    db.commit()
    db.refresh(group)
    return group


@router.delete(
    "/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar un grupo completo (hard-delete)",
    responses={
        401: {"description": "No autenticado o token inválido."},
        404: {"description": "El grupo no existe."},
        409: {"description": "Alguna unidad del grupo está en uso."},
    },
)
def delete_unit_measure_group(
    id: UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    group = get_or_404(db, UnitMeasureGroup, id, "Unit measure group not found")
    blocking = [u for u in group.units if unit_measure_in_use(db, u.id)]
    if blocking:
        raise _blocking_units_conflict(blocking)

    for unit in list(group.units):
        db.delete(unit)
    db.delete(group)
    db.commit()
