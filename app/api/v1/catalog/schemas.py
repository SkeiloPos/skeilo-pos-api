from typing import Literal
from uuid import UUID
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.variant_name import normalize_variant_name


# ---------- Variantes ----------
class VariantCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    # spec 084 (A-79): la presentación del catálogo sigue siendo la única fuente
    # estructural del tamaño. `None` = "Presentación única".
    presentation_id: UUID | None = None
    price: Decimal = Field(0, ge=0, max_digits=12, decimal_places=2)
    sku: str | None = Field(None, max_length=100)
    # spec 099: alias comercial opcional, puramente cosmético. Ausente/`None` -> `""`.
    # `validate_default=True`: el validador debe normalizar también cuando el campo se
    # omite del payload, no solo cuando llega explícito (si no, Pydantic deja el `None`
    # del default sin pasar por el validador, y `""` nunca se materializa).
    name: str | None = Field(None, validate_default=True)

    @field_validator("name", mode="before")
    @classmethod
    def _normalize_name(cls, value: str | None) -> str:
        return normalize_variant_name(value)


class VariantUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    # Ausente o `None` = sin cambio de presentación.
    presentation_id: UUID | None = None
    price: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    sku: str | None = Field(None, max_length=100)
    active: bool | None = None
    # spec 099: mismo criterio "ausente = sin cambio" que el resto de los campos de
    # este PATCH parcial (price, active, presentation_id) -- distinto del contrato de
    # `VariantSaveIn`/`VariantCreate` (reemplazo total, FR-012/Escenario 14), que este
    # endpoint standalone no usa. Para quitar el nombre propio aquí, se envía `""`
    # explícito (no ausente, no `null`).
    name: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_variant_name(value)


class VariantResponse(BaseModel):
    id: UUID
    product_id: UUID
    sku: str | None = None
    price: Decimal
    active: bool
    # spec 084 (A-79): `presentation_id` sigue siendo la fuente estructural del tamaño.
    presentation_id: UUID
    presentation_name: str
    # spec 099: alias comercial (crudo) y etiqueta calculada que ve el comensal.
    name: str
    display_name: str

    model_config = ConfigDict(from_attributes=True)


# Orden de presentaciones (spec 042): el endpoint dedicado de reordenamiento
# (`VariantReorderRequest`/`VariantOrderEntry`/`VariantReorderResponse`) se retiró en spec 043
# (A-55, registro-de-anomalias.md) -- el orden ahora se envía como la posición de cada
# presentación dentro de `variants[]` del guardado consolidado (`VariantSaveIn` abajo).


# ---------- Receta (BOM): insumos fijos ----------
class RecipeItemIn(BaseModel):
    """Un insumo que la variante consume siempre (200 g de fruta). Lo que el cliente
    elige va en `variant_option_groups`, no aquí."""

    inventory_item_id: UUID
    quantity: Decimal = Field(..., gt=0, max_digits=12, decimal_places=3)


class RecipeItemResponse(BaseModel):
    id: UUID
    inventory_item_id: UUID
    # spec 105 (research.md): denormalizados desde `RecipeItem.inventory_item` (ya
    # cargado vía `lazy="joined"`) -- mismo patrón que `category_name`/`presentation_name`
    # (spec 097/103) -- para que el cliente muestre el nombre/unidad del insumo sin un
    # `GET /inventory/items/{id}` por cada línea de receta.
    inventory_item_name: str
    unit_measure_id: UUID
    quantity: Decimal

    model_config = ConfigDict(from_attributes=True)


# ---------- Grupos de opciones por variante ----------
class VariantOptionGroupIn(BaseModel):
    """Un grupo que ofrece esta variante: cuántas opciones elige el cliente y cuánto
    descuenta **cada una** de ellas.

    `quantity_per_option` es por opción elegida, no el total del grupo: dos sabores con
    120 descuentan 120 de cada uno. En 0 el grupo se ofrece pero no descuenta por sí
    mismo (el consumo, si lo hay, sale de `options.item_quantity`).
    """

    option_group_id: UUID
    min_select: int = Field(0, ge=0)
    max_select: int = Field(1, ge=1)
    quantity_per_option: Decimal = Field(0, ge=0, max_digits=12, decimal_places=3)

    @model_validator(mode="after")
    def _max_ge_min(self) -> "VariantOptionGroupIn":
        if self.max_select < self.min_select:
            raise ValueError("max_select no puede ser menor que min_select")
        return self


class VariantOptionGroupResponse(BaseModel):
    id: UUID
    product_variant_id: UUID
    option_group_id: UUID
    min_select: int
    max_select: int
    quantity_per_option: Decimal

    model_config = ConfigDict(from_attributes=True)


# ---------- Guardado consolidado de producto (spec 043) ----------
class VariantSaveIn(BaseModel):
    """Una presentación dentro del árbol que aceptan `POST`/`PATCH`/`PUT /products` (spec 043).

    `id` distingue crear de actualizar: ausente/`None` crea una presentación nueva; presente
    actualiza la fila existente con ese id (debe pertenecer al producto). Solo tiene sentido en
    `PATCH`/`PUT /products/{id}` -- en `POST /products` el producto todavía no tiene
    presentaciones, así que siempre llega `None` ahí.

    `recipe`/`option_groups` son reemplazo total (mismo patrón que tenían los endpoints ya
    retirados `PUT /variants/{id}/recipe`/`PUT /variants/{id}/option-groups`, A-55). La posición
    de esta entrada dentro de la lista `variants` del body determina su `display_order` (1-based).
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    id: UUID | None = None
    price: Decimal = Field(0, ge=0, max_digits=12, decimal_places=2)
    sku: str | None = Field(None, max_length=100)
    active: bool = True
    recipe: list[RecipeItemIn] = Field(default_factory=list)
    option_groups: list[VariantOptionGroupIn] = Field(default_factory=list)
    # spec 084 (A-79): presentación del catálogo (spec 083) que nombra estructuralmente
    # a esta variante. `None` = "Presentación única" (productos sin tamaños).
    presentation_id: UUID | None = None
    # spec 099: alias comercial opcional (reemplazo total como el resto de esta clase,
    # FR-012/Escenario 14): ausente/`None` = "sin nombre propio" (`""`), no "sin cambio".
    # `validate_default=True`: ver el mismo comentario en `VariantCreate.name`.
    name: str | None = Field(None, validate_default=True)

    @field_validator("name", mode="before")
    @classmethod
    def _normalize_name(cls, value: str | None) -> str:
        return normalize_variant_name(value)


# ---------- Grupos de opciones ----------
class OptionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, examples=["Fresa", "Chocolate"])
    extra_price: Decimal = Field(0, ge=0, max_digits=12, decimal_places=2)
    inventory_item_id: UUID | None = Field(
        None, description="Insumo que descuenta al elegir esta opción."
    )
    item_quantity: Decimal = Field(0, ge=0, max_digits=12, decimal_places=3)


class OptionUpdate(BaseModel):
    """Parcial: solo se aplican los campos presentes en el body. `inventory_item_id`
    en `null` desliga el insumo (usar `model_fields_set` para distinguirlo de ausente)."""

    name: str | None = Field(None, min_length=1, max_length=255)
    extra_price: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    inventory_item_id: UUID | None = None
    item_quantity: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=3)
    active: bool | None = None


class OptionResponse(BaseModel):
    id: UUID
    option_group_id: UUID
    name: str
    extra_price: Decimal
    inventory_item_id: UUID | None = None
    # spec 105 (research.md): denormalizados desde `Option.inventory_item` (ya cargado
    # vía `lazy="joined"`), igual que `RecipeItemResponse` -- `None` si la opción no
    # tiene insumo ligado.
    inventory_item_name: str | None = None
    unit_measure_id: UUID | None = None
    item_quantity: Decimal
    active: bool

    model_config = ConfigDict(from_attributes=True)


class OptionSelectionIn(BaseModel):
    """Una opción elegida junto con cuántas unidades de ella (spec 065). `quantity`
    default 1 -- el mismo significado que "incluir este id en `option_ids`" tenía antes
    de esta spec. Compartida por `CartItemIn`/`CartItemUpdate`, la línea de `POST
    /orders`, `orders/consolidation.py` y la línea de venta de mostrador."""

    option_id: UUID
    quantity: int = Field(1, ge=1)


class ToppingSelectionIn(BaseModel):
    """Una adición (producto-topping) elegida junto con cuántas unidades de ella
    (spec 129, FR-018). Igual forma que `OptionSelectionIn`, pero referenciando un
    producto marcado `is_topping=true` en vez de una `Option` -- las adiciones ya no
    viven en grupos de opciones (research.md D4 de spec 129). Compartida por
    `CartItemIn`/`CartItemUpdate` y `POST /orders/items/{item_id}/toppings`."""

    topping_product_id: UUID
    quantity: int = Field(1, ge=1)


class UpdateToppingQuantityIn(BaseModel):
    """Cuerpo de `PATCH /orders/items/{item_id}/toppings/{topping_product_id}`: la
    cantidad absoluta nueva de una adición ya agregada a la línea (spec 129), mismo
    criterio que `UpdateAdditionQuantityIn` -- no un delta."""

    quantity: int = Field(..., ge=1)


class UpdateAdditionQuantityIn(BaseModel):
    """Cuerpo de `PATCH /orders/items/{item_id}/additions/{option_id}`: la cantidad
    absoluta nueva de una adición ya agregada a la línea (spec 123), igual convención
    que `CartItemUpdate.quantity` -- no un delta. Puede subirla o bajarla, nunca a la
    misma que ya tiene (`orders/additions.py` rechaza ese caso); `ge=1` ya impide
    bajarla a 0 -- quitarla del todo sigue yendo por anular la línea."""

    quantity: int = Field(..., ge=1)


class OptionGroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, examples=["Sabores de helado"])
    min_select: int = Field(0, ge=0)
    max_select: int = Field(1, ge=1)
    # spec 064: sin default -- "incluido" (sabor cubierto por el precio de la
    # presentación) y "con_recargo" (topping con precio propio) son dos casos de uso
    # igual de válidos; el administrador debe elegir uno explícitamente (FR-001).
    pricing_type: Literal["incluido", "con_recargo"]
    # spec 065: a diferencia de pricing_type, "conteo" (el comportamiento de hoy) sí es
    # un default de negocio razonable -- omitirlo no rompe ningún catálogo existente
    # (FR-001, research.md Decisión 5).
    selection_mode: Literal["conteo", "cantidad"] = "conteo"
    max_quantity_per_option: int | None = Field(None, gt=0)
    max_total_quantity: int | None = Field(None, gt=0)


class OptionGroupUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    min_select: int | None = Field(None, ge=0)
    max_select: int | None = Field(None, ge=1)
    active: bool | None = None
    pricing_type: Literal["incluido", "con_recargo"] | None = None
    selection_mode: Literal["conteo", "cantidad"] | None = None
    max_quantity_per_option: int | None = Field(None, gt=0)
    max_total_quantity: int | None = Field(None, gt=0)


class OptionGroupResponse(BaseModel):
    id: UUID
    name: str
    min_select: int
    max_select: int
    active: bool
    pricing_type: str
    selection_mode: str
    max_quantity_per_option: int | None
    max_total_quantity: int | None
    options: list[OptionResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


# La asignación grupo<->producto desapareció: los grupos cuelgan de la VARIANTE
# (`VariantOptionGroupIn` arriba), porque cuántas opciones se eligen y cuánto descuenta
# cada una cambian con el tamaño.
