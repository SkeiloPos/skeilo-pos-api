from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.db import get_db, get_tenant
from app.core.crud import get_or_404, ensure_unique
from app.core.dependencies import get_current_user, require_tenant_admin, forbid_cashier
from app.core.models import User, Tenant
from app.core.plan_limits import ensure_module_access
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.recipe_item import RecipeItem
from app.models.inventory_item import InventoryItem
from app.models.option_group import OptionGroup
from app.models.option import Option
from app.models.presentation import Presentation
from app.models.variant_option_group import VariantOptionGroup
from app.api.v1.catalog.service import (
    ensure_default_variant,
    _resolve_presentation,
    _unique_sku,
    _slug,
    _next_display_order,
    display_name_duplicado,
    _raise_display_name_conflict,
)
from app.api.v1.catalog.schemas import (
    VariantCreate,
    VariantUpdate,
    VariantResponse,
    RecipeItemResponse,
    OptionGroupCreate,
    OptionGroupUpdate,
    OptionGroupResponse,
    OptionCreate,
    OptionUpdate,
    OptionResponse,
    VariantOptionGroupResponse,
)

router = APIRouter(tags=["catalog"])


# ============================ Variantes ============================
def _commit_variante(db: Session, variant: ProductVariant) -> ProductVariant:
    """Commit + refresh traduciendo el choque de la constraint única a 409.

    La comprobación previa no puede cerrar la carrera entre dos admins guardando a la
    vez; sin esto, esa carrera sale como 500."""
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Ya existe una variante con esa presentación o SKU",
        )
    db.refresh(variant)
    return variant


@router.get(
    "/products/{product_id}/variants",
    response_model=list[VariantResponse],
    summary="Listar las variantes de un producto",
    responses={404: {"description": "El producto no existe."}},
)
def list_variants(
    product_id: UUID,
    active: bool | None = Query(None, description="Filtra por estado activo/inactivo."),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    get_or_404(db, Product, product_id, "Product not found")
    stmt = select(ProductVariant).where(ProductVariant.product_id == product_id)
    if active is not None:
        stmt = stmt.where(ProductVariant.active.is_(active))
    # spec 042: antes ordenaba por nombre (alfabético) -- ahora refleja el orden que el
    # administrador definió (o el de creación, por backfill, si nunca lo cambió).
    return db.execute(stmt.order_by(ProductVariant.display_order)).scalars().all()


@router.post(
    "/products/{product_id}/variants",
    response_model=VariantResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear una variante para un producto",
    responses={
        404: {"description": "El producto no existe."},
        409: {"description": "Ya existe una variante con esa presentación (activa o desactivada) o con ese SKU."},
        422: {"description": "La presentación no existe o está inactiva."},
    },
)
def create_variant(
    product_id: UUID,
    body: VariantCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_tenant_admin),
):
    product = get_or_404(db, Product, product_id, "Product not found")
    # spec 084 (A-79): `presentation_id` nulo = "Presentación única".
    presentation = _resolve_presentation(db, product_id, body.presentation_id)
    # spec 099 (FR-008): la etiqueta resultante no puede chocar con otra variante activa.
    display_name = body.name or presentation.name
    dup_nombre = display_name_duplicado(db, product_id, display_name)
    if dup_nombre is not None:
        _raise_display_name_conflict(None)
    if body.sku is not None:
        ensure_unique(db, ProductVariant, ProductVariant.sku, body.sku, "SKU already exists")
    sku = body.sku or _unique_sku(db, f"{_slug(product.name)}-{_slug(presentation.name)}")
    variant = ProductVariant(
        product_id=product_id,
        presentation_id=presentation.id,
        price=body.price,
        sku=sku,
        active=True,
        name=body.name,
        display_order=_next_display_order(db, product_id),
    )
    db.add(variant)
    return _commit_variante(db, variant)


@router.patch(
    "/variants/{variant_id}",
    response_model=VariantResponse,
    summary="Actualizar una variante (presentación, precio, sku, activa)",
    responses={
        404: {"description": "La variante no existe."},
        409: {"description": "Otra variante del producto ya usa esa presentación, o el SKU está tomado."},
        422: {"description": "La presentación no existe o está inactiva."},
    },
)
def update_variant(
    variant_id: UUID,
    body: VariantUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_tenant_admin),
):
    variant = get_or_404(db, ProductVariant, variant_id, "Variant not found")
    if body.sku is not None and body.sku != variant.sku:
        ensure_unique(db, ProductVariant, ProductVariant.sku, body.sku, "SKU already exists")
        variant.sku = body.sku
    if body.presentation_id is not None and body.presentation_id != variant.presentation_id:
        presentation = _resolve_presentation(
            db, variant.product_id, body.presentation_id, current=variant
        )
        variant.presentation_id = presentation.id
    if body.price is not None:
        variant.price = body.price
    if body.active is not None:
        variant.active = body.active
    # spec 099: `None` = sin cambio de nombre propio (ver VariantUpdate.name); un valor
    # (incluida `""` explícita) sí se aplica. Se valida contra el estado ya actualizado
    # arriba (presentación/activa), antes de persistir.
    if body.name is not None:
        variant.name = body.name
    if variant.active:
        dup_nombre = display_name_duplicado(
            db, variant.product_id, variant.display_name, exclude_id=variant.id
        )
        if dup_nombre is not None:
            _raise_display_name_conflict(None)
    return _commit_variante(db, variant)


@router.delete(
    "/variants/{variant_id}",
    response_model=VariantResponse,
    summary="Desactivar una variante (soft-delete)",
    responses={404: {"description": "La variante no existe."}},
)
def delete_variant(
    variant_id: UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_tenant_admin),
):
    variant = get_or_404(db, ProductVariant, variant_id, "Variant not found")
    variant.active = False
    db.commit()
    db.refresh(variant)
    return variant


# ============================ Receta (BOM) ============================
@router.get(
    "/variants/{variant_id}/recipe",
    response_model=list[RecipeItemResponse],
    summary="Ver la receta de una variante",
    responses={403: {"description": "El Cajero no puede consultar la receta (spec 093)."}},
)
def get_recipe(
    variant_id: UUID,
    db: Session = Depends(get_db),
    _: User = Depends(forbid_cashier),
):
    get_or_404(db, ProductVariant, variant_id, "Variant not found")
    return db.execute(
        select(RecipeItem).where(RecipeItem.product_variant_id == variant_id)
    ).scalars().all()


# ================= Grupos de opciones de una variante =================
@router.get(
    "/variants/{variant_id}/option-groups",
    response_model=list[VariantOptionGroupResponse],
    summary="Grupos de opciones que ofrece una variante",
)
def get_variant_option_groups(
    variant_id: UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    get_or_404(db, ProductVariant, variant_id, "Variant not found")
    return db.execute(
        select(VariantOptionGroup).where(
            VariantOptionGroup.product_variant_id == variant_id
        )
    ).scalars().all()


# ============================ Grupos de opciones ============================
def _variantes_que_lo_usan(db: Session, group_id: UUID) -> list[str]:
    """Variantes que ofrecen este grupo, como 'Producto · Variante'.

    Retirarlo las dejaría ofreciendo algo que el cliente ya no puede elegir, o —peor—
    vendiendo sin descontar. Se bloquea en vez de borrar en cascada, que es exactamente
    el tipo de fallo silencioso que costó caro antes."""
    stmt = (
        select(Product.name, Presentation.name)
        .join(ProductVariant, ProductVariant.product_id == Product.id)
        .join(Presentation, Presentation.id == ProductVariant.presentation_id)
        .join(VariantOptionGroup, VariantOptionGroup.product_variant_id == ProductVariant.id)
        .where(VariantOptionGroup.option_group_id == group_id)
    )
    return [f"{p} · {v}" for p, v in db.execute(stmt).all()]


def _bloquear_si_esta_en_uso(db: Session, group_id: UUID, accion: str) -> None:
    variantes = _variantes_que_lo_usan(db, group_id)
    if not variantes:
        return
    nombres = ", ".join(f"«{v}»" for v in dict.fromkeys(variantes))
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "error": (
                f"No se puede {accion}: {nombres} lo ofrece a sus clientes. "
                "Quítalo de esas presentaciones primero."
            ),
            "variantes_en_uso": list(dict.fromkeys(variantes)),
        },
    )


@router.get(
    "/option-groups",
    response_model=list[OptionGroupResponse],
    summary="Listar grupos de opciones con sus opciones",
)
def list_option_groups(
    active: bool | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    stmt = select(OptionGroup).options(selectinload(OptionGroup.options))
    if active is not None:
        stmt = stmt.where(OptionGroup.active.is_(active))
    return db.execute(stmt.order_by(OptionGroup.name)).scalars().all()


@router.post(
    "/option-groups",
    response_model=OptionGroupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear un grupo de opciones",
)
def create_option_group(
    body: OptionGroupCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_tenant_admin),
):
    if body.max_select < body.min_select:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "max_select < min_select")
    ensure_unique(db, OptionGroup, OptionGroup.name, body.name, "Option group name already exists")
    group = OptionGroup(
        name=body.name,
        min_select=body.min_select,
        max_select=body.max_select,
        pricing_type=body.pricing_type,
        selection_mode=body.selection_mode,
        max_quantity_per_option=body.max_quantity_per_option,
        max_total_quantity=body.max_total_quantity,
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    return group


@router.patch(
    "/option-groups/{group_id}",
    response_model=OptionGroupResponse,
    summary="Actualizar un grupo de opciones (nombre, min/max, activo)",
)
def update_option_group(
    group_id: UUID,
    body: OptionGroupUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_tenant_admin),
):
    group = get_or_404(db, OptionGroup, group_id, "Option group not found")
    if body.name is not None and body.name != group.name:
        ensure_unique(
            db, OptionGroup, OptionGroup.name, body.name,
            "Option group name already exists", exclude_id=group_id,
        )
        group.name = body.name
    min_select = body.min_select if body.min_select is not None else group.min_select
    max_select = body.max_select if body.max_select is not None else group.max_select
    if max_select < min_select:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "max_select < min_select")
    group.min_select = min_select
    group.max_select = max_select
    if body.active is not None:
        if not body.active and group.active:
            _bloquear_si_esta_en_uso(db, group_id, "desactivar este grupo")
        group.active = body.active
    if body.pricing_type is not None:
        # spec 064, FR-004: pasar de "con_recargo" a "incluido" fuerza $0 en todas las
        # opciones del grupo -- mismo criterio no destructivo ya usado por RN-CAT-38
        # (desvincular insumo resetea item_quantity). La confirmación previa al usuario
        # es responsabilidad del frontend (research.md Decisión 2); el backend aplica
        # el cambio directamente.
        if body.pricing_type == "incluido" and group.pricing_type != "incluido":
            db.execute(
                update(Option).where(Option.option_group_id == group_id).values(extra_price=0)
            )
        group.pricing_type = body.pricing_type
    if body.selection_mode is not None:
        group.selection_mode = body.selection_mode
    if body.max_quantity_per_option is not None:
        group.max_quantity_per_option = body.max_quantity_per_option
    if body.max_total_quantity is not None:
        group.max_total_quantity = body.max_total_quantity
    db.commit()
    db.refresh(group)
    return group


@router.delete(
    "/option-groups/{group_id}",
    response_model=OptionGroupResponse,
    summary="Desactivar un grupo de opciones (soft-delete)",
)
def delete_option_group(
    group_id: UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_tenant_admin),
):
    group = get_or_404(db, OptionGroup, group_id, "Option group not found")
    _bloquear_si_esta_en_uso(db, group_id, "desactivar este grupo")
    group.active = False
    db.commit()
    db.refresh(group)
    return group


@router.post(
    "/option-groups/{group_id}/options",
    response_model=OptionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Agregar una opción (p.ej. un sabor) a un grupo",
)
def add_option(
    group_id: UUID,
    body: OptionCreate,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_tenant),
    _: User = Depends(require_tenant_admin),
):
    group = get_or_404(db, OptionGroup, group_id, "Option group not found")
    if group.pricing_type == "incluido" and body.extra_price != 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Los grupos «Incluido» no permiten precio distinto de $0.",
        )
    # spec 064, FR-011/FR-012: guardar insumo o cantidad de consumo exige el módulo
    # Inventario en el plan del tenant -- gating a nivel de campo, no de ruta completa
    # (research.md Decisión 4): un topping sin insumo (precio puro) sigue funcionando
    # sin importar el plan.
    if body.inventory_item_id is not None or body.item_quantity > 0:
        ensure_module_access(db, tenant, "inventario")
    if body.inventory_item_id is not None:
        get_or_404(db, InventoryItem, body.inventory_item_id, "Inventory item not found")
    dup = db.execute(
        select(Option).where(Option.option_group_id == group_id, Option.name == body.name)
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Option name already exists in group")
    option = Option(
        option_group_id=group_id,
        name=body.name,
        extra_price=body.extra_price,
        inventory_item_id=body.inventory_item_id,
        item_quantity=body.item_quantity,
    )
    db.add(option)
    db.commit()
    db.refresh(option)
    return option


@router.patch(
    "/options/{option_id}",
    response_model=OptionResponse,
    summary="Actualizar una opción (nombre, precio extra, insumo, activa)",
)
def update_option(
    option_id: UUID,
    body: OptionUpdate,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_tenant),
    _: User = Depends(require_tenant_admin),
):
    option = get_or_404(db, Option, option_id, "Option not found")
    if body.name is not None and body.name != option.name:
        dup = db.execute(
            select(Option).where(
                Option.option_group_id == option.option_group_id,
                Option.name == body.name,
                Option.id != option_id,
            )
        ).scalar_one_or_none()
        if dup is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Option name already exists in group")
        option.name = body.name
    if body.extra_price is not None:
        if option.option_group.pricing_type == "incluido" and body.extra_price != 0:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Los grupos «Incluido» no permiten precio distinto de $0.",
            )
        option.extra_price = body.extra_price
    # `None` explícito desliga el insumo; ausente = no tocar.
    if "inventory_item_id" in body.model_fields_set:
        if body.inventory_item_id is not None:
            get_or_404(db, InventoryItem, body.inventory_item_id, "Inventory item not found")
            option.inventory_item_id = body.inventory_item_id
        else:
            option.inventory_item_id = None
            option.item_quantity = 0
    if body.item_quantity is not None and option.inventory_item_id is not None:
        option.item_quantity = body.item_quantity
    if body.active is not None:
        option.active = body.active
    # spec 064, FR-011/FR-012: solo exige el módulo cuando ESTE request intenta agregar o
    # aumentar consumo de inventario -- enlazar un insumo nuevo, o subir item_quantity en
    # una opción que ya tiene (o pasa a tener) insumo. NO se evalúa sobre el estado final
    # completo de la opción: una opción que ya tenía insumo configurado de antes (dato
    # preservado, FR-013) puede seguir editándose en cualquier otro campo (ej. `name`) sin
    # que ese insumo heredado dispare un 403 por una edición que no lo toca. Desvincular
    # (que ya fuerza item_quantity=0 por RN-CAT-38) nunca exige el módulo tampoco.
    intenta_enlazar_insumo = (
        "inventory_item_id" in body.model_fields_set and body.inventory_item_id is not None
    )
    intenta_subir_cantidad = (
        body.item_quantity is not None
        and body.item_quantity > 0
        and option.inventory_item_id is not None
    )
    if intenta_enlazar_insumo or intenta_subir_cantidad:
        ensure_module_access(db, tenant, "inventario")
    db.commit()
    db.refresh(option)
    return option


@router.delete(
    "/options/{option_id}",
    response_model=OptionResponse,
    summary="Desactivar una opción (soft-delete: conserva el histórico de ventas)",
)
def delete_option(
    option_id: UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_tenant_admin),
):
    option = get_or_404(db, Option, option_id, "Option not found")
    option.active = False
    db.commit()
    db.refresh(option)
    return option


# La asignación grupo<->producto se retiró: los grupos cuelgan de la VARIANTE, vía
# `GET /variants/{id}/option-groups` (más arriba). El `PUT` que definía/reemplazaba esos
# grupos se retiró en spec 043 (A-55): ahora se define dentro del guardado consolidado de
# `POST`/`PATCH /products` (`ProductService._save_variant_tree`/`_reconcile_variants`).
