from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
from app.core.timezone import UtcDatetime


class PresentationSummary(BaseModel):
    """Presentación asociada a una categoría, solo lectura (spec 083, FR-004)."""

    id: UUID = Field(..., description="Identificador único de la presentación.")
    name: str = Field(..., description="Nombre de la presentación.", examples=["Pequeño"])

    model_config = ConfigDict(from_attributes=True)


class CategoryCreate(BaseModel):
    name: str = Field(
        ..., min_length=1, max_length=255,
        description="Nombre de la categoría. Debe ser único.",
        examples=["Bebidas"],
    )
    description: str | None = Field(
        None, max_length=255,
        description="Descripción opcional de la categoría.",
        examples=["Gaseosas, jugos y aguas"],
    )
    display_order: int | None = Field(
        None, ge=0,
        description="Posición en el filtro del menú QR; si se omite, se asigna automáticamente al final de la lista actual.",
        examples=[10],
    )
    presentation_ids: list[UUID] | None = Field(
        None,
        description="Presentaciones del catálogo global asociadas a esta categoría (spec 083, FR-004). Ausente o vacía: sin ninguna asociada.",
    )


class CategoryUpdate(BaseModel):
    name: str | None = Field(
        None, min_length=1, max_length=255,
        description="Nuevo nombre de la categoría. Debe seguir siendo único.",
        examples=["Bebidas frías"],
    )
    description: str | None = Field(
        None, max_length=255,
        description="Nueva descripción de la categoría.",
        examples=["Gaseosas, jugos y aguas"],
    )
    active: bool | None = Field(
        None,
        description="Estado activo/inactivo de la categoría.",
        examples=[True],
    )
    # spec 128 (FR-001/FR-005/FR-006): visibilidad específica del menú QR,
    # independiente de `active`. Solo ADMIN puede cambiarla, y solo mientras
    # la categoría sigue activa (guarda en update_category).
    visible_en_qr: bool | None = Field(
        None,
        description="Visibilidad específica en el menú QR, independiente de `active`. Solo editable por un Administrador, y solo mientras la categoría está activa.",
        examples=[False],
    )
    display_order: int | None = Field(
        None, ge=0,
        description="Posición en el filtro del menú QR; si se omite, se asigna automáticamente al final de la lista actual.",
        examples=[10],
    )
    presentation_ids: list[UUID] | None = Field(
        None,
        description="Reemplazo total de las presentaciones asociadas (spec 083, FR-004). Ausente (`None`) no toca la asociación existente; `[]` desasocia todas.",
    )


class CategoryResponse(BaseModel):
    id: UUID = Field(..., description="Identificador único de la categoría.")
    name: str = Field(..., description="Nombre de la categoría.", examples=["Bebidas"])
    description: str | None = Field(
        None, description="Descripción de la categoría.",
        examples=["Gaseosas, jugos y aguas"],
    )
    active: bool = Field(..., description="Indica si la categoría está activa.", examples=[True])
    visible_en_qr: bool = Field(
        ..., description="Indica si la categoría es visible en el menú QR.", examples=[True],
    )
    display_order: int = Field(..., description="Posición en el filtro del menú QR.", examples=[10])
    created_at: UtcDatetime = Field(..., description="Fecha de creación del registro.")
    updated_at: UtcDatetime | None = Field(None, description="Fecha de la última actualización.")
    presentations: list[PresentationSummary] = Field(
        default_factory=list,
        description="Presentaciones del catálogo global asociadas actualmente a esta categoría (spec 083, FR-004).",
    )

    model_config = ConfigDict(from_attributes=True)
