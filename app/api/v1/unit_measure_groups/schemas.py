from uuid import UUID
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field


class UnitMeasureGroupUnitCreate(BaseModel):
    name: str = Field(
        ..., min_length=1, max_length=255,
        description="Nombre de la unidad base del grupo.",
        examples=["Mililitro"],
    )
    abbreviation: str = Field(
        ..., min_length=1, max_length=50,
        description="Abreviatura de la unidad base. Debe ser única en el tenant.",
        examples=["ml"],
    )


class UnitMeasureGroupCreate(BaseModel):
    name: str = Field(
        ..., min_length=1, max_length=255,
        description="Nombre del grupo. Debe ser único en el tenant.",
        examples=["Volumen"],
    )
    base_unit: UnitMeasureGroupUnitCreate


class UnitMeasureGroupAddUnit(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, examples=["Litro"])
    abbreviation: str = Field(..., min_length=1, max_length=50, examples=["L"])
    factor_to_base: Decimal = Field(
        ..., gt=0, max_digits=18, decimal_places=6,
        description="Factor hacia la unidad base del grupo.",
        examples=["1000.000000"],
    )


class UnitMeasureGroupRename(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class UnitMeasureGroupBaseReassign(BaseModel):
    unit_measure_id: UUID


class UnitInGroupResponse(BaseModel):
    id: UUID
    name: str
    abbreviation: str
    factor_to_base: Decimal
    is_base: bool
    active: bool

    model_config = ConfigDict(from_attributes=True)


class UnitMeasureGroupResponse(BaseModel):
    id: UUID
    name: str
    units: list[UnitInGroupResponse]
    created_at: datetime
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class BlockingUnit(BaseModel):
    id: UUID
    name: str
    abbreviation: str | None = None
    reason: str = "en uso"
