from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.schema_types import AssetRefIn, AssetUrl
from app.api.v1.catalog.schemas import (
    VariantSaveIn,
    VariantResponse,
    RecipeItemResponse,
    VariantOptionGroupResponse,
)
from app.core.timezone import UtcDatetime


class PreparationType(str, Enum):
    """'prepared' = se arma con receta; 'packaged' = se vende empacado."""
    PREPARED = "prepared"
    PACKAGED = "packaged"


# ---------- Plantilla única de export/import de la carta (spec 143, research.md D8) ----------
# Una sola definición de cabeceras por hoja, en orden -- `export.py` e `import_.py` las
# importan en vez de repetirlas cada uno por su lado (FR-001). Ver data-model.md para el
# origen/destino y tipo esperado de cada columna.
PRODUCT_TEMPLATE_HEADERS = [
    "Código",
    "Nombre",
    "Categoría",
    "Tipo de preparación",
    "Descripción",
    "Activo",
    "Disponible",
    "Rastrea inventario",
    "Es adición",
    "Visible en menú QR",
]

VARIANT_TEMPLATE_HEADERS = [
    "Código de producto",
    "Presentación",
    "SKU",
    "Nombre comercial",
    "Precio",
    "Orden",
    "Activo",
]

RECIPE_TEMPLATE_HEADERS = [
    "Código de producto",
    "Presentación",
    "Insumo",
    "Cantidad",
]

TOPPING_TEMPLATE_HEADERS = [
    "Código de producto",
    "Código de producto adición",
]

# Nombre de hoja -> cabeceras esperadas, en el orden de procesamiento FR-007.
PRODUCT_IMPORT_SHEETS = {
    "Productos": PRODUCT_TEMPLATE_HEADERS,
    "Presentaciones y precios": VARIANT_TEMPLATE_HEADERS,
    "Receta": RECIPE_TEMPLATE_HEADERS,
    "Adiciones": TOPPING_TEMPLATE_HEADERS,
}


class ToppingSummaryResponse(BaseModel):
    """spec 129: nombre de una adición asociada, denormalizado (igual criterio que
    `category_name` en `Product`) -- evita que el selector de "Adiciones disponibles"
    tenga que resolver cada id por separado contra `GET /products/{id}`."""

    id: UUID
    name: str

    model_config = ConfigDict(from_attributes=True)


class ProductCreate(BaseModel):
    code: str | None = Field(
        None, min_length=1, max_length=20,
        description="Si no se indica, se asigna uno automáticamente (spec 143).",
    )
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
    # spec 129 (FR-001): marca este producto como una adición (topping) ofrecible
    # dentro de otros productos, además de poder venderse como línea independiente.
    is_topping: bool = Field(
        False, description="Si este producto funciona como adición (topping) de otros productos."
    )
    # spec 129 (research.md D6): `None` = usar el default calculado en servicio
    # (`false` si `is_topping=true`, `true` en caso contrario); un valor explícito
    # siempre gana sobre ese default.
    visible_en_qr: bool | None = Field(
        None,
        description=(
            "Visibilidad como ítem propio en el menú QR. Si se omite, se calcula: "
            "oculto si es una adición, visible en caso contrario."
        ),
    )
    # spec 129 (FR-002): adiciones que este producto ofrece, a nivel de producto
    # (no de variante) -- aplica automáticamente a todas sus variantes.
    topping_ids: list[UUID] = Field(
        default_factory=list,
        description="Ids de productos marcados `is_topping=true` que este producto ofrece.",
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
    code: str | None = Field(None, min_length=1, max_length=20)
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
    # spec 129: `None` = no tocar (igual criterio que el resto de campos opcionales
    # de este esquema). A diferencia de `ProductCreate`, aquí `None` nunca significa
    # "recalcular el default" -- ese default solo tiene sentido al crear.
    is_topping: bool | None = None
    visible_en_qr: bool | None = None
    # spec 129 (FR-002): `None` = no tocar la asociación de adiciones existente;
    # presente (incluida lista vacía) = reemplazo total, mismo criterio que
    # `variants` abajo y que `category.presentations` (spec 083).
    topping_ids: list[UUID] | None = None
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
    code: str
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
    available_changed_at: UtcDatetime | None = None
    available_changed_by_name: str | None = None
    tracks_inventory: bool
    # spec 129: valores ya resueltos (nunca `null`) -- el default calculado de
    # `visible_en_qr` se aplica en servicio antes de llegar a esta respuesta.
    is_topping: bool = False
    visible_en_qr: bool = True
    created_at: UtcDatetime
    updated_at: UtcDatetime | None = None

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
    # spec 129 (FR-002): adiciones asociadas a este producto. No se puede poblar por
    # `from_attributes` (el modelo ORM expone `toppings: list[ProductTopping]`, no
    # una lista de ids) -- el servicio lo arma explícitamente, mismo criterio que
    # `recipe`/`option_groups` en `VariantDetailResponse`.
    topping_ids: list[UUID] = Field(default_factory=list)
    # spec 129: nombres denormalizados de las mismas adiciones de `topping_ids`, para
    # que el selector de "Adiciones disponibles" no tenga que resolverlas por id.
    toppings: list[ToppingSummaryResponse] = Field(default_factory=list)


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
    topping_ids: list[UUID] = Field(default_factory=list)
    toppings: list[ToppingSummaryResponse] = Field(default_factory=list)


# ---------- Importación/exportación masiva de la carta (spec 143) ----------
class ProductImportJobResponse(BaseModel):
    id: UUID
    status: str
    file_name: str
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    total_rows: int
    created_count: int
    updated_count: int
    failed_count: int
    # No es una columna mapeada -- lo fija el router al construir la respuesta
    # (mismo patrón que `InventoryImportJobResponse.has_errors`), para que el
    # frontend decida si ofrece la descarga de errores sin mirar el conteo.
    has_errors: bool

    model_config = ConfigDict(from_attributes=True)


class ImportStructureError(BaseModel):
    """Cuerpo del 422 de FR-010: problemas de estructura, uno por línea."""
    problems: list[str]
