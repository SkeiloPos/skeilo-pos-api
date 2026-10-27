from typing import Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import InstrumentedAttribute, Session


def get_or_404(db: Session, model: type, id: UUID, detail: str = "Not found"):
    """Devuelve la instancia por id o lanza 404."""
    obj = db.get(model, id)
    if not obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    return obj


def ensure_unique(
    db: Session,
    model: type,
    field: InstrumentedAttribute,
    value,
    detail: str = "Already exists",
    exclude_id: Optional[UUID] = None,
) -> None:
    """Lanza 409 si ya existe un registro de `model` con `field == value`.

    `exclude_id` excluye el propio registro (para updates).
    """
    existing = db.execute(
        select(model).where(field == value)
    ).scalar_one_or_none()
    if existing and (exclude_id is None or existing.id != exclude_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def ensure_unique_ci(
    db: Session,
    model: type,
    field: InstrumentedAttribute,
    value: str,
    detail: str = "Already exists",
    exclude_id: Optional[UUID] = None,
    scope: Optional[tuple[InstrumentedAttribute, object]] = None,
) -> None:
    """Como `ensure_unique`, pero sin distinguir mayúsculas/minúsculas (`upper(field) ==
    upper(value)`) -- respaldado por un índice único de expresión equivalente a nivel de BD
    (`upper(...)`), así que "BLS" y "bls" se tratan como la misma abreviatura sea cual sea la
    ruta de entrada (este chequeo evita el 409 crudo del `IntegrityError`; el índice es la
    garantía real contra condiciones de carrera entre el SELECT y el INSERT).

    `scope`, si se da (ej. `(UnitMeasure.group_id, group.id)`), limita la unicidad a los
    registros con ese valor -- "única dentro del grupo", no en todo el tenant (spec 140)."""
    stmt = select(model).where(func.upper(field) == value.upper())
    if scope is not None:
        scope_field, scope_value = scope
        stmt = stmt.where(scope_field == scope_value)
    existing = db.execute(stmt).scalar_one_or_none()
    if existing and (exclude_id is None or existing.id != exclude_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
