"""Menú público (front-end del QR). No requiere autenticación de usuario.

`GET /menu` resuelve el tenant por el header x-tenant-host (catálogo genérico del
local); `GET /menu/qr-token/{token}` lo resuelve desde el token firmado, que es la
vía del comensal y la única que identifica una mesa."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.db import get_db, get_tenant
from app.core.models import Tenant
from app.core.qr_context import open_qr_context
from app.core.rate_limit import enforce as rate_limit
from app.models.category import Category
from app.models.inventory_item import InventoryItem
from app.models.option import Option
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.option_group import OptionGroup
from app.models.variant_option_group import VariantOptionGroup
from app.models.dining_table import DiningTable
from app.api.v1.promotions.service import (
    active_variant_set_rules, menu_unit_discount, menu_variant_promotion,
    variant_display_names, variant_set_condition_text,
)
from app.api.v1.menu.schemas import (
    MenuCategoryResponse, MenuProductResponse, MenuVariantResponse,
    MenuOptionGroupResponse, MenuOptionResponse, MenuTableResponse,
    MenuBusinessResponse, MenuPromotionAnnouncement, MenuPromotionRule,
)

router = APIRouter(prefix="/menu", tags=["menu"])


def _option_availability(db: Session) -> dict[UUID, bool]:
    """`{option_id: hay stock}` para todas las opciones que ligan insumo, en dos
    queries constantes (nada de N+1 por producto).

    El flag es **por opción, no por (variante × opción)**: se compara el stock contra
    el peor caso del catálogo, la mayor cantidad que le pide cualquier presentación. O
    sea que "Fresa" puede salir agotada para la ensalada pequeña (60 g) porque la grande
    necesita 180 y solo quedan 100. Es el error seguro; la alternativa exacta obligaría
    a exponer la configuración de cada tamaño en el menú público. `check_availability`
    sigue siendo la red fina, con su 409 al añadir al carrito.
    """
    # Por grupo: la mayor cantidad que define un tamaño, y si alguno se queda en 0 (en
    # cuyo caso manda la cantidad propia de la opción).
    por_grupo: dict[UUID, tuple[Decimal, bool]] = {
        gid: (Decimal(mayor or 0), bool(sin_definir))
        for gid, mayor, sin_definir in db.execute(
            select(
                VariantOptionGroup.option_group_id,
                func.max(VariantOptionGroup.quantity_per_option),
                func.bool_or(VariantOptionGroup.quantity_per_option <= 0),
            ).group_by(VariantOptionGroup.option_group_id)
        ).all()
    }

    rows = db.execute(
        select(
            Option.id, Option.option_group_id, Option.item_quantity,
            InventoryItem.current_stock, InventoryItem.active,
        ).join(InventoryItem, InventoryItem.id == Option.inventory_item_id)
    ).all()

    out: dict[UUID, bool] = {}
    for option_id, group_id, item_qty, stock, item_active in rows:
        mayor, alguno_sin_definir = por_grupo.get(group_id, (Decimal(0), True))
        # La cantidad de la opción solo cuenta si algún tamaño la deja mandar: si todos
        # definen la suya, ese valor no aplica en ningún sitio e inflaría el umbral.
        need = max(mayor, Decimal(item_qty)) if alguno_sin_definir else mayor
        # Una opción ligada a un insumo que no pide cantidad en ningún sitio no
        # descuenta nada, así que no puede agotarse; pero sí desaparece si el insumo
        # se desactiva.
        out[option_id] = bool(item_active) and (need <= 0 or Decimal(stock) >= need)
    return out


def _build_menu(db: Session, tenant: Tenant | None = None) -> list[MenuCategoryResponse]:
    avail = _option_availability(db)
    now = datetime.now(timezone.utc)
    # spec 063 (revisión 2026-09-01): reglas de promoción por conjunto de
    # variantes vigentes en este instante (la vigencia es de la promoción,
    # compartida por todas sus reglas). spec 127: `tenant`, cuando el caller
    # lo tiene resuelto, evalúa la vigencia en SU zona horaria real.
    rules = active_variant_set_rules(db, now, tenant)
    # spec 066: los nombres del descriptor, **una vez por llamada** y fuera del
    # bucle de variantes — dentro sería un N+1 (research.md D-12).
    promo_names = variant_display_names(
        db, {v.product_variant_id for r in rules for v in r.variants}
    )

    categories = db.execute(
        select(Category).where(Category.active.is_(True))
        .order_by(Category.display_order.desc(), Category.name)
    ).scalars().all()

    products = db.execute(
        select(Product)
        # spec 093 (FR-019, research.md D6, A-104): un producto agotado
        # (`available=False`) ya NO se oculta -- sigue devolviéndose, marcado con
        # `sold_out=True` más abajo, para que el menú lo muestre atenuado en vez de
        # hacerlo desaparecer. Sigue excluyéndose por `active` sin cambios: eso es
        # alta/baja de catálogo, no disponibilidad temporal.
        .where(Product.active.is_(True))
        .options(
            selectinload(Product.variants)
            .selectinload(ProductVariant.option_groups)
            .selectinload(VariantOptionGroup.option_group)
            .selectinload(OptionGroup.options),
        )
        .order_by(Product.name)
    ).scalars().all()

    by_cat: dict[UUID, list[Product]] = {}
    for p in products:
        by_cat.setdefault(p.category_id, []).append(p)

    result: list[MenuCategoryResponse] = []
    for cat in categories:
        cat_products: list[MenuProductResponse] = []
        for p in by_cat.get(cat.id, []):
            variants: list[MenuVariantResponse] = []
            union: dict[UUID, MenuOptionGroupResponse] = {}
            pedible = False

            for v in p.variants:
                if not v.active:
                    continue
                groups = []
                v_pedible = True
                for link in v.option_groups:
                    g = link.option_group
                    if not g.active:
                        continue
                    options = [
                        MenuOptionResponse(
                            id=o.id, name=o.name, extra_price=o.extra_price,
                            available=avail.get(o.id, True),
                        )
                        for o in g.options if o.active
                    ]
                    # Un grupo obligatorio sin ninguna opción con stock deja esta
                    # presentación imposible de pedir: mejor decirlo aquí que dejar al
                    # comensal chocar con el 409 al añadir al carrito. spec 065: un
                    # grupo "cantidad" nunca es obligatorio (FR-003), sin importar
                    # `min_select` heredado del `VariantOptionGroup`.
                    if (
                        g.selection_mode != "cantidad"
                        and link.min_select > 0
                        and not any(o.available for o in options)
                    ):
                        v_pedible = False
                    grupo = MenuOptionGroupResponse(
                        id=g.id, name=g.name,
                        min_select=link.min_select, max_select=link.max_select,
                        # Descuenta el tamaño (`quantity_per_option`) o la propia
                        # opción (`item_quantity`): cualquiera de las dos obliga
                        # al comensal a completar el grupo. Ver
                        # `line_pricing.grupos_que_descuentan`.
                        consume=(
                            link.quantity_per_option > 0
                            or any(o.item_quantity > 0 for o in g.options if o.active)
                        ),
                        selection_mode=g.selection_mode,
                        max_quantity_per_option=g.max_quantity_per_option,
                        max_total_quantity=g.max_total_quantity,
                        options=options,
                    )
                    groups.append(grupo)
                    union.setdefault(g.id, grupo)

                # Al navegar el menú aún no hay carrito: solo las reglas con
                # `min_qty == 1` bajan el precio unitario (spec 063,
                # contracts/superficies-consumo.md §1). spec 066 (A-68): eso incluye
                # ahora `package_price`, y el tipo real viaja en `discount_kind`.
                discounted_price = None
                discount_kind = None
                promotion = None
                if rules:
                    disc = menu_unit_discount(rules, v.id, v.price)
                    if disc is not None:
                        discounted_price, discount_kind = disc
                    # spec 066 (FR-007): la condición y el equivalente por unidad,
                    # ya calculados y ya renderizados por el backend.
                    promotion = menu_variant_promotion(rules, v.id, v.price, promo_names)

                variants.append(MenuVariantResponse(
                    id=v.id, presentation_id=v.presentation_id,
                    presentation_name=v.presentation_name, display_name=v.display_name,
                    price=v.price, discounted_price=discounted_price,
                    discount_kind=discount_kind, option_groups=groups, available=v_pedible,
                    promotion=promotion,
                ))
                pedible = pedible or v_pedible

            if not variants:
                continue

            cat_products.append(MenuProductResponse(
                id=p.id, name=p.name, description=p.description, image_url=p.image_url,
                variants=variants, option_groups=list(union.values()), available=pedible,
                # spec 093 (FR-019/FR-020, research.md D6): flag manual e independiente
                # de `available` (arriba, "pedible por stock") -- `not p.available` es
                # el interruptor "Agotado" que marcan Cajero/Admin desde la Carta del
                # menú. Un producto puede ser `available=True` (pedible por stock) y
                # `sold_out=True` (marcado agotado a mano) a la vez; `sold_out` manda:
                # no se puede agregar al pedido sin importar `available`.
                sold_out=not p.available,
            ))
        if cat_products:
            result.append(MenuCategoryResponse(id=cat.id, name=cat.name, products=cat_products))
    return result


def _money(value: Decimal) -> str:
    """`$12.000` — formato de pesos colombianos con separador de miles."""
    return "$" + f"{int(value):,}".replace(",", ".")


def _build_menu_promotions(
    db: Session, now: datetime, tenant: Tenant | None = None
) -> list[MenuPromotionAnnouncement]:
    """Anuncios de promociones por conjunto de variantes **vigentes en este
    instante** (spec 063, FR-022 / SC-007): `status == "active"` **y**
    `_valid_now` verdadero (ventana de día/hora en la zona del tenant).
    `_build_menu` no se toca. `now` viene aware, no arrastra A-08. `tenant`
    (spec 127): si el caller lo tiene resuelto, la vigencia se evalúa en SU
    zona horaria real.

    spec 063 (revisión 2026-09-01): una promoción anuncia **una `rules[]` por
    cada `PromotionRule` vigente** que tenga (antes: siempre 1, una promoción
    = una combinación). El DTO `MenuPromotionAnnouncement.rules[]` ya tenía
    esta forma (research.md D-R3) — solo cambia la cardinalidad."""
    # `active_variant_set_rules` ya exige `status == "active"` + `_valid_now`
    # (vigencia en ese instante, FR-022 / SC-007), evaluado sobre la
    # promoción dueña de cada regla.
    by_promotion: dict = {}   # promotion_id -> MenuPromotionAnnouncement
    reglas = active_variant_set_rules(db, now, tenant)
    # spec 066: los nombres del descriptor se resuelven **una vez por llamada**,
    # sobre la unión de los conjuntos vigentes. Dentro del bucle sería un N+1
    # (research.md D-12).
    names = variant_display_names(
        db, {v.product_variant_id for r in reglas for v in r.variants}
    )
    for rule in reglas:
        text = variant_set_condition_text(rule, names)
        if text is None:
            continue
        promo = rule.promotion
        anuncio = by_promotion.get(promo.id)
        if anuncio is None:
            anuncio = MenuPromotionAnnouncement(
                promotion_id=promo.id, promotion_name=promo.name, rules=[],
            )
            by_promotion[promo.id] = anuncio
        # spec 084 (A-81): la configuración por presentación se guarda como una regla por
        # producto (una variante cada una), así que varias reglas de una misma promoción
        # producen el MISMO texto («Llevando 8 onzas x 2 pagas $12.000»). Se anuncia una sola
        # vez y `variant_count` suma las variantes de todas las reglas fusionadas.
        existente = next((r for r in anuncio.rules if r.text == text), None)
        if existente is not None:
            existente.variant_count += len(rule.variants)
            continue
        anuncio.rules.append(MenuPromotionRule(
            text=text,
            variant_count=len(rule.variants),
            min_qty=rule.min_qty,
            value=rule.value,
        ))
    anuncios = list(by_promotion.values())
    return anuncios


@router.get("", response_model=list[MenuCategoryResponse], summary="Menú público (catálogo activo)")
def public_menu(db: Session = Depends(get_db), tenant: Tenant = Depends(get_tenant)):
    return _build_menu(db, tenant)


@router.get("/promotions", response_model=list[MenuPromotionAnnouncement],
            summary="Anuncios de promociones de precio por presentación vigentes")
def public_menu_promotions(db: Session = Depends(get_db), tenant: Tenant = Depends(get_tenant)):
    return _build_menu_promotions(db, datetime.now(timezone.utc), tenant)


# NOTA: se eliminó `GET /menu/qr/{qr_token}` (UUID plano + header x-tenant-host).
# Resolvía el tenant desde una cabecera falsificable y exponía un identificador de
# mesa editable por el cliente; lo reemplaza `/menu/qr-token/{token}`, donde tenant
# y mesa viajan firmados dentro del token.


@router.get("/qr-token/{token}", summary="Resolver mesa por token QR firmado + menú")
async def menu_by_signed_qr(token: str, request: Request):
    """Flujo público del comensal: el token firmado lleva tenant + mesa, así que
    resuelve todo sin header x-tenant-host y sin exponer el table_id plano."""
    # Por IP antes de verificar la firma (un token basura no debe salir gratis)
    # y por mesa después, ya con el table_id de confianza.
    await rate_limit(request, "menu_qr")
    with open_qr_context(token) as ctx:
        await rate_limit(request, "menu_qr", table_id=ctx.table_id)
        table = ctx.db.execute(
            select(DiningTable).where(
                DiningTable.id == ctx.table_id, DiningTable.active.is_(True)
            )
        ).scalar_one_or_none()
        if table is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Mesa no encontrada o inactiva")
        return {
            "table": MenuTableResponse.model_validate(table),
            "business": MenuBusinessResponse.model_validate(ctx.tenant),
            "menu": _build_menu(ctx.db, ctx.tenant),
            # spec 040 (FR-021): clave aditiva; el resto del dict no cambia.
            "promotions": _build_menu_promotions(ctx.db, datetime.now(timezone.utc), ctx.tenant),
        }
