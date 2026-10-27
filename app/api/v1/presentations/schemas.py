from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
from app.core.timezone import UtcDatetime


class PresentationCreate(BaseModel):
    name: str = Field(
        ..., min_length=1, max_length=255,
        description="Nombre de la presentación. Debe ser único.",
        examples=["Pequeño"],
    )


class PresentationUpdate(BaseModel):
    name: str | None = Field(
        None, min_length=1, max_length=255,
        description="Nuevo nombre de la presentación. Debe seguir siendo único.",
        examples=["Chico"],
    )
    active: bool | None = Field(
        None,
        description="Estado activo/inactivo de la presentación.",
        examples=[True],
    )


class PresentationResponse(BaseModel):
    id: UUID = Field(..., description="Identificador único de la presentación.")
    name: str = Field(..., description="Nombre de la presentación.", examples=["Pequeño"])
    active: bool = Field(..., description="Indica si la presentación está activa.", examples=[True])
    created_at: UtcDatetime = Field(..., description="Fecha de creación del registro.")
    updated_at: UtcDatetime | None = Field(None, description="Fecha de la última actualización.")

    model_config = ConfigDict(from_attributes=True)
