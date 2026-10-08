from enum import Enum
from uuid import UUID
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.schema_types import AssetRefIn, AssetUrl
from app.api.v1.catalog.schemas import (
    VariantSaveIn,
    VariantResponse,
    RecipeItemResponse,
    VariantOptionGroupResponse,
)


class PreparationType(str, Enum):
    """'prepared' = se arma con receta; 'packaged' = se vende empacado."""
    PREPARED = "prepared"
    PACKAGED = "packaged"


class ProductCreate(BaseModel):
    category_id: UUID = Field(..., description="Categoría del producto.")
    name: str = Field(..., min_length=1, max_length=255, examples=["Helado en copa"])
    description: str | None = Field(None, max_length=500)
    preparation_type: PreparationType = Field(
        PreparationType.PREPARED,
        description="prepared (receta) o packaged (empacado).",
    )
    # spec 080: en base de datos vive solo la key relativa. Si el cliente reenvía
    # una URL absoluta del bucket gestionado (dominio viejo o nuevo) se normaliza
    # a key antes de persistir (FR-004); una URL de otro origen se conserva.
    image_url: AssetRefIn = Field(None, max_length=500)
    available: bool = True
    tracks_inventory: bool = Field(
        False, description="Si el producto exige y aplica descuento de inventario en sus presentaciones."
    )
    variants: list[VariantSaveIn] = Field(
        default_factory=list,
        description=(
            "Presentaciones iniciales del producto, con su receta y grupos de opciones (spec "
            "043). Si viene vacía, se hereda una presentación por cada una asociada a la "
            "categoría (spec 083, FR-005), o se crea automáticamente 'Presentación única' a "
            "precio 0 si la categoría no tiene ninguna asociada (FR-006, RN-CAT-05, A-74)."
        ),
    )


class ProductUpdate(BaseModel):
    category_id: UUID | None = None
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=500)
    preparation_type: PreparationType | None = None
    image_url: AssetRefIn = Field(None, max_length=500)
    # spec 088 (FR-002, research D5): imagen que el formulario mostraba al abrirse. Una
    # imagen enviada solo cuenta como cambio si esta base coincide con la vigente; un
    # formulario desactualizado se ignora en silencio. "No enviada" (ausente) es distinto
    # de `null` explícito ("el formulario no vio ninguna imagen"): el servicio los
    # distingue con `model_fields_set`. Solo existe al actualizar, no al crear.
    image_url_base: AssetRefIn = Field(None, max_length=500)
    active: bool | None = None
    available: bool | None = None
    tracks_inventory: bool | None = None
    variants: list[VariantSaveIn] | None = Field(
        None,
        description=(
            "Árbol completo de presentaciones deseado (spec 043). Ausente = no tocar ninguna "
            "presentación (back-compat). Presente (incluida lista vacía) = reemplazo total: crea "
            "las entradas sin `id`, actualiza las que traen `id`, desactiva cualquier "
            "presentación activa no listada. Distinguir 'ausente' de '[]' requiere leer "
            "`model_fields_set`, no solo `is None` (ver ProductService.update_product)."
        ),
    )


class ProductAvailabilityUpdate(BaseModel):
    """Body de `PATCH /products/{id}/availability` (spec 093, research.md D3): el único
    campo que ese endpoint angosto acepta, para que Cajero y Admin puedan tocar
    `available` sin alcanzar el resto de `ProductUpdate`."""

    available: bool


class ProductResponse(BaseModel):
    id: UUID
    category_id: UUID
    # spec 097: resuelto en el propio servidor (Product.category_name), sin depender de
    # que el cliente haya cargado `GET /categories` (tope 100 por página) por separado.
    category_name: str | None = None
    name: str
    description: str | None = None
    preparation_type: PreparationType
    # spec 080: la columna guarda la key; se ensambla la URL de visualización
    # contra ASSETS_BASE_URL al serializar (FR-005/FR-006).
    image_url: AssetUrl = None
    active: bool
    available: bool
    # spec 093 (FR-013, data-model.md): quién marcó/desmarcó `available` por última
    # vez y cuándo. `None` = nunca se tocó el interruptor dedicado (incluye todo
    # producto creado antes de esta spec).
    available_changed_at: datetime | None = None
    available_changed_by_name: str | None = None
    tracks_inventory: bool
    created_at: datetime
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class ProductListResponse(ProductResponse):
    pass


class VariantDetailResponse(VariantResponse):
    """Spec 103-detalle-producto-consolidado (research.md D1): extiende `VariantResponse`
    sin tocarlo -- `VariantResponse` sigue sin receta/grupos de opciones para cualquier
    otro consumidor (p. ej. `GET /products/{id}/variants`). `recipe`/`option_groups` no se
    pueden poblar por `from_attributes` porque el modelo ORM los expone como
    `recipe_items`/`option_groups` -- el servicio los arma explícitamente
    (`to_detail_response`), ocultando `recipe` para el rol Cajero (FR-003, preserva FR-018
    de la spec 093) sin ocultar `option_groups`."""

    recipe: list[RecipeItemResponse] = Field(default_factory=list)
    option_groups: list[VariantOptionGroupResponse] = Field(default_factory=list)


class ProductDetailResponse(ProductResponse):
    """Spec 093 (escenario 9, research.md D9): a diferencia de `ProductListResponse`,
    incluye las presentaciones activas con su precio. Spec 103: cada variante ahora
    también trae su receta y sus grupos de opciones embebidos (`VariantDetailResponse`),
    sin peticiones adicionales por tamaño -- ver esa clase para el resguardo de rol.

    `deactivated` (spec 103, ajuste posterior a la implementación inicial): las
    presentaciones desactivadas, en el shape `VariantResponse` (sin receta/grupos --
    restaurarlas ya pide esos datos aparte si hace falta). Vienen del mismo
    `product.variants` ya precargado por `get_detail_or_404` -- no agregan ninguna
    consulta nueva. Esto hace innecesario un segundo `GET /products/{id}/variants`
    solo para listar las desactivadas al abrir el formulario de edición."""

    variants: list[VariantDetailResponse] = Field(default_factory=list)
    deactivated: list[VariantResponse] = Field(default_factory=list)


class VariantSaveOut(VariantResponse):
    """Estado final de una presentación tras un guardado consolidado (spec 043).

    Extiende `VariantResponse` (`id`, `product_id`, `presentation_id`, `presentation_name`, `name`, `display_name`, `sku`, `price`, `active`); `recipe` y
    `option_groups` no se pueden poblar por `from_attributes` porque el modelo ORM los expone como
    `recipe_items`/`option_groups` con otro shape -- el servicio los arma explícitamente al
    construir la respuesta.
    """

    display_order: int
    recipe: list[RecipeItemResponse] = Field(default_factory=list)
    option_groups: list[VariantOptionGroupResponse] = Field(default_factory=list)


class ProductSaveResponse(ProductResponse):
    """Respuesta de `POST`/`PATCH`/`PUT /products` (spec 043, FR-006): el árbol completo y final
    del producto guardado, para que el formulario no necesite una lectura adicional."""

    variants: list[VariantSaveOut] = Field(default_factory=list)
