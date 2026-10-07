from uuid import UUID
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.core.schema_types import AssetUrl
from app.api.v1.promotions.schemas import PromotionType


class MenuOptionResponse(BaseModel):
    id: UUID
    name: str
    extra_price: Decimal
    # Hay stock del insumo que consume esta opción. Solo el booleano: el comensal es
    # anónimo y no debe ver ni los insumos ni las existencias del negocio.
    available: bool = True

    model_config = ConfigDict(from_attributes=True)


class MenuOptionGroupResponse(BaseModel):
    id: UUID
    name: str
    min_select: int
    max_select: int
    # El grupo descuenta inventario por cada opción elegida. El cliente no ve
    # cuánto (eso es del negocio), pero sí necesita el booleano: en un grupo así
    # elegir menos del máximo sirve de más y descuenta de menos, así que la UI
    # tiene que exigir el máximo en vez del mínimo.
    consume: bool = False
    # spec 065: "conteo" (min/max_select cuentan opciones distintas, comportamiento
    # de siempre) o "cantidad" (unidades libres por opción, sin mínimo posible;
    # min_select/max_select se ignoran en ese modo). Los dos topes solo tienen
    # efecto en modo "cantidad"; `None` = sin tope.
    selection_mode: str = "conteo"
    max_quantity_per_option: int | None = None
    max_total_quantity: int | None = None
    options: list[MenuOptionResponse] = Field(default_factory=list)


class MenuVariantPromotion(BaseModel):
    """spec 066 (FR-007): información de la **única** regla vigente que cubre esta
    presentación en este instante — FR-012 y la spec 063 FR-014 impiden que haya
    dos. Derivada, nunca persistida.

    Viaja con los textos **ya compuestos**: el redondeo al peso sobre `Decimal` no
    tiene equivalente exacto en el `number` de JavaScript, así que componerlos una
    sola vez en Python es lo que hace verificable SC-005 (research.md D-4). El
    frontend imprime cadenas; no recalcula importes (FR-007).

    El importe vinculante lo sigue calculando el cobro (FR-011): esto es
    informativo."""
    condition_text: str          # FR-004 completo — idéntico al cartel y a administración
    short_condition: str         # "2 x $12.000" | "3 x -15%"            (FR-008)
    unit_equivalent: Decimal     # ya redondeado al peso                  (FR-009)
    unit_equivalent_approx: bool # el exacto no era entero en pesos       (FR-009)
    unit_equivalent_text: str    # "$6.000 c/u" | "≈ $4.333 c/u"          (FR-009)
    display_text: str            # "2 x $12.000 · $6.000 c/u"             (FR-008)
    type: PromotionType
    min_qty: int
    value: Decimal


class MenuVariantResponse(BaseModel):
    id: UUID
    presentation_id: UUID
    presentation_name: str
    # spec 099 (FR-006): la etiqueta que debe mostrar el comensal (detalle, carrito,
    # resumen de checkout) -- `presentation_name` se conserva sin cambios para los
    # clientes que ya lo leen (Escenario 15).
    display_name: str
    price: Decimal
    # Precio ya con el mejor descuento vigente aplicado, o `None` si ninguna
    # promoción aplica. Se evalúa asumiendo cantidad 1 (aún no hay carrito).
    # spec 066 (A-68): cubre también `package_price` con `min_qty == 1`, donde vale
    # el valor de la regla **tal cual** — es el importe que el cobro aplica, incluso
    # si resulta mayor o igual que `price` (FR-010, FR-015).
    discounted_price: Decimal | None = None
    # Tipo de la promoción que generó `discounted_price`, o `None` si no hay
    # descuento — el cliente lo usa solo para elegir cómo mostrar la insignia,
    # nunca para recalcular precios. spec 066: lleva el tipo **real** de la regla,
    # así que ahora puede valer `"package_price"` (research.md D-13).
    discount_kind: PromotionType | None = None
    # spec 066 (FR-007): la regla vigente que cubre esta presentación, ya calculada.
    # Aditivo y opcional: un cliente sin desplegar lo ignora y sigue igual que hoy.
    promotion: MenuVariantPromotion | None = None
    # Los grupos cuelgan de la presentación: cuántas opciones se eligen cambia con el
    # tamaño (la ensalada pequeña 1 sabor, la mediana 2). Esta es la fuente autoritativa.
    option_groups: list[MenuOptionGroupResponse] = Field(default_factory=list)
    available: bool = True

    model_config = ConfigDict(from_attributes=True)


class MenuProductResponse(BaseModel):
    id: UUID
    name: str
    description: str | None = None
    # spec 080: la columna guarda la key; se ensambla la URL contra
    # ASSETS_BASE_URL al serializar (FR-005). Tolerancia de lectura de una fila
    # aún no migrada incluida (FR-009b).
    image_url: AssetUrl = None
    variants: list[MenuVariantResponse] = Field(default_factory=list)
    # Unión de los grupos de todas las presentaciones. **Solo sirve para resolver
    # nombres y precios de una opción** (tickets, comandas, carrito): su `min/max_select`
    # es el de la primera presentación que lo ofrece y no significa nada. Para saber
    # qué puede elegir el cliente hay que mirar `variants[].option_groups`.
    option_groups: list[MenuOptionGroupResponse] = Field(default_factory=list)
    # False si ninguna presentación se puede pedir (todas tienen algún grupo
    # obligatorio sin opciones con stock).
    available: bool = True
    # spec 093 (FR-019/FR-020): marcado manual de "Agotado" (Cajero/Admin, Carta del
    # menú) -- independiente de `available` (arriba). `True` = no se puede agregar al
    # pedido, aunque siga visible en el menú.
    sold_out: bool = False


class MenuCategoryResponse(BaseModel):
    id: UUID
    name: str
    products: list[MenuProductResponse] = Field(default_factory=list)


class MenuPromotionRule(BaseModel):
    # Texto legible construido en el backend (spec 063, `variant_set_condition_text`),
    # p. ej.: "Llevando 2 de estas 8 variantes pagas $12.000".
    text: str
    # spec 063: el anuncio describe el conjunto de variantes, ya no una presentación.
    variant_count: int
    min_qty: int
    value: Decimal


class MenuPromotionAnnouncement(BaseModel):
    """Anuncio de una promoción por conjunto de variantes **vigente en este
    instante** (spec 063, FR-022). Se expone aparte de `GET /menu`: su
    `response_model` y `_build_menu` no cambian (research.md D9)."""
    promotion_id: UUID
    promotion_name: str
    rules: list[MenuPromotionRule] = Field(default_factory=list)


class MenuTableResponse(BaseModel):
    id: UUID
    number: int
    name: str | None = None

    model_config = ConfigDict(from_attributes=True)


class MenuBusinessResponse(BaseModel):
    """Branding del negocio para el menú público del QR.

    El comensal es anónimo y no puede llamar a `GET /tenant` (requiere auth), así
    que el nombre y el logo del negocio viajan dentro de la respuesta del menú.
    """

    name: str
    # spec 080: key en base de datos, URL ensamblada contra ASSETS_BASE_URL al
    # serializar (FR-005/FR-006).
    logo_url: AssetUrl = None

    model_config = ConfigDict(from_attributes=True)
