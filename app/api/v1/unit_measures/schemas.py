from uuid import UUID
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field
from app.core.timezone import UtcDatetime


class UnitMeasureCreate(BaseModel):
    name: str = Field(
        ..., min_length=1, max_length=255,
        description="Nombre de la unidad de medida.",
        examples=["Litro", "Gramo", "Bola"],
    )
    abbreviation: str = Field(
        ..., min_length=1, max_length=50,
        description="Abreviatura de la unidad de medida. Debe ser única.",
        examples=["L", "g", "bola"],
    )
    group_id: UUID = Field(
        ...,
        description="Grupo al que pertenece la unidad (spec 137). Retirado del flujo "
        "normal: usar POST /unit-measure-groups/{id}/units desde el frontend nuevo; "
        "se mantiene por compatibilidad con integraciones existentes.",
    )
    factor_to_base: Decimal = Field(
        ..., gt=0, max_digits=18, decimal_places=6,
        description="Factor hacia la base de su dimensión (spec 134), p. ej. kg→1000 g.",
        examples=["1000.000000"],
    )


class UnitMeasureUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    abbreviation: str | None = Field(None, min_length=1, max_length=50)
    factor_to_base: Decimal | None = Field(None, gt=0, max_digits=18, decimal_places=6)
    active: bool | None = None


class UnitMeasureResponse(BaseModel):
    id: UUID
    name: str
    abbreviation: str
    factor_to_base: Decimal
    active: bool
    group_id: UUID
    is_base: bool
    created_at: UtcDatetime
    updated_at: UtcDatetime | None = None

    model_config = ConfigDict(from_attributes=True)


class FactorChangeResponse(BaseModel):
    id: UUID
    unit_measure_id: UUID
    old_factor: Decimal
    new_factor: Decimal
    changed_by: UUID
    changed_at: UtcDatetime

    model_config = ConfigDict(from_attributes=True)
