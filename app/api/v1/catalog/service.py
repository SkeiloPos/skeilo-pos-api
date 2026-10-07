"""Servicio de catálogo (heladería): variantes vendibles y su receta (BOM).

El precio vive en la variante; la receta liga la variante a insumos de inventario.
Los grupos de opciones (sabores) se gestionan aparte y se asignan al producto.
"""
import re
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.recipe_item import RecipeItem
from app.models.inventory_item import InventoryItem
from app.models.option_group import OptionGroup
from app.models.variant_option_group import VariantOptionGroup
from app.models.presentation import Presentation
from app.api.v1.catalog.schemas import VariantSaveIn, RecipeItemIn, VariantOptionGroupIn
# Reexportado (no definido aquí): `catalog_engine.core` no puede depender del
# ORM (SC-006), así que la constante vive allá y este módulo solo la reusa —
# ver `format_item_description` (spec 087, FR-010).
from app.catalog_engine.core import DEFAULT_PRESENTATION_NAME  # noqa: F401


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^A-Z0-9]+", "", (text or "").upper())
    return cleaned[:4] or "X"


def _unique_sku(db: Session, base: str) -> str:
    sku = base
    i = 2
    while db.execute(select(ProductVariant.id).where(ProductVariant.sku == sku)).first() is not None:
        sku = f"{base}-{i}"
        i += 1
    return sku


def _next_display_order(db: Session, product_id: UUID) -> int:
    """Siguiente posición al final para una presentación nueva (spec 042, FR-005).

    Cuenta activas e inactivas del mismo producto -- una presentación desactivada
    sigue ocupando su posición en la secuencia (research.md Decisión 4), así que el
    siguiente hueco libre es siempre el máximo actual + 1, nunca el conteo de filas.
    """
    current_max = db.execute(
        select(func.max(ProductVariant.display_order)).where(
            ProductVariant.product_id == product_id
        )
    ).scalar()
    return (current_max or 0) + 1


def default_presentation(db: Session) -> Presentation:
    """Presentación del catálogo que nombra la variante de un producto sin tamaños
    (spec 084, A-79/A-74). Get-or-create por nombre exacto, sin filtrar `active`: una
    presentación desactivada por el administrador solo deja de ofrecerse en el selector,
    no deja de nombrar las variantes que ya la usan. Sin marca de "presentación del
    sistema": si el administrador la renombró, se crea otra con el literal (research.md D11).
    """
    existing = db.execute(
        select(Presentation).where(Presentation.name == DEFAULT_PRESENTATION_NAME).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    presentation = Presentation(name=DEFAULT_PRESENTATION_NAME, active=True)
    try:
        # SAVEPOINT: si otra petición la creó a la vez (`presentations.name` es UNIQUE),
        # se descarta solo este INSERT y se reutiliza la ganadora, sin perder el resto de
        # la transacción del guardado consolidado.
        with db.begin_nested():
            db.add(presentation)
    except IntegrityError:
        return db.execute(
            select(Presentation).where(Presentation.name == DEFAULT_PRESENTATION_NAME).limit(1)
        ).scalar_one()
    return presentation


def variante_duplicada(
    db: Session,
    product_id: UUID,
    presentation_id: UUID,
    *,
    exclude_id: UUID | None = None,
) -> ProductVariant | None:
    """Variante del mismo producto que ya ocupa esa presentación, esté activa o no.

    Existe porque `DELETE /variants/{id}` es un soft-delete: la fila desactivada sigue
    ocupando la presentación en `uq__product_variants__product_id__presentation_id`, así que
    volver a crear un tamaño borrado choca con la constraint. Sin esta comprobación el
    conflicto llegaba al `commit()` y salía como 500 en vez de un 409 accionable.
    Ordena activas primero para que el 409 hable de la que el usuario tiene a la vista.
    """
    stmt = (
        select(ProductVariant)
        .where(
            ProductVariant.product_id == product_id,
            ProductVariant.presentation_id == presentation_id,
        )
        .order_by(ProductVariant.active.desc())
    )
    if exclude_id is not None:
        stmt = stmt.where(ProductVariant.id != exclude_id)
    return db.execute(stmt).scalars().first()


def ensure_default_variant(db: Session, product: Product, *, price=0) -> ProductVariant:
    """Garantiza que un producto tenga al menos una variante vendible. Los
    productos sin tamaños obtienen una variante asociada a la presentación
    'Presentación única' (spec 083, A-74; spec 084, A-79)."""
    existing = db.execute(
        select(ProductVariant).where(ProductVariant.product_id == product.id).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    presentation = default_presentation(db)
    variant = ProductVariant(
        product_id=product.id,
        presentation_id=presentation.id,
        sku=_unique_sku(db, f"{_slug(product.name)}-DEF"),
        price=price,
        active=True,
        display_order=_next_display_order(db, product.id),
    )
    db.add(variant)
    db.flush()
    return variant


# ===================== Guardado consolidado de producto (spec 043) =====================
#
# `reorder_variants`/`VariantReorderError` (el endpoint dedicado de reordenamiento, spec 042)
# se retiraron aquí (A-55, registro-de-anomalias.md): `_assign_display_orders` abajo reemplaza
# su función, dentro del guardado consolidado, con el mismo patrón de dos pasadas.
# `_save_variant_entry`/`_replace_recipe`/`_replace_option_groups` reproducen, a propósito, la
# misma lógica de validación que ya usan `create_variant`/`update_variant`/`set_recipe`/
# `set_variant_option_groups` en `catalog/router.py` -- no se importa de ahí (la capa de servicio
# no depende de la de router) y esas funciones del router siguen intactas mientras existan sus
# endpoints (retiro condicionado a FR-007). La única diferencia real es que estas versiones no
# hacen `commit()` por su cuenta (el llamador -- `ProductService.create_product`/`update_product`
# -- controla una única transacción para todo el árbol, FR-004) y que cada error trae
# `variant_index` para identificar qué entrada del payload falló (research.md Decisión 5).


def _raise_presentation_conflict(dup: ProductVariant, index: int | None) -> None:
    if dup.active:
        mensaje = "Esta presentación ya está en uso por otra variante de este producto"
    else:
        mensaje = (
            f"Ya existe una variante «{dup.presentation_name}» desactivada en este "
            "producto. Reactívala en vez de crear otra."
        )
    detail = {"error": mensaje, "variant_id": str(dup.id), "active": dup.active}
    if index is not None:
        detail["variant_index"] = index
    raise HTTPException(status.HTTP_409_CONFLICT, detail=detail)


def _ensure_sku_unique(
    db: Session, sku: str, index: int, *, exclude_id: UUID | None = None
) -> None:
    stmt = select(ProductVariant.id).where(ProductVariant.sku == sku)
    if exclude_id is not None:
        stmt = stmt.where(ProductVariant.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"error": "SKU already exists", "variant_index": index},
        )


def display_name_duplicado(
    db: Session,
    product_id: UUID,
    display_name: str,
    *,
    exclude_id: UUID | None = None,
) -> ProductVariant | None:
    """Otra variante **activa** del mismo producto que ya resuelve a `display_name`
    (spec 099, FR-008). Solo activas (Clarificación, sesión 2026-10-06, pregunta 1): una
    variante desactivada no bloquea su etiqueta. Comparación insensible a mayúsculas
    (pregunta 2) -- se hace en Python, no en SQL, porque pocas variantes por producto no
    justifica un índice funcional para esto (research.md D5)."""
    stmt = select(ProductVariant).where(
        ProductVariant.product_id == product_id,
        ProductVariant.active.is_(True),
    )
    if exclude_id is not None:
        stmt = stmt.where(ProductVariant.id != exclude_id)
    objetivo = display_name.lower()
    for otra in db.execute(stmt).scalars().all():
        if otra.display_name.lower() == objetivo:
            return otra
    return None


def _raise_display_name_conflict(index: int | None) -> None:
    detail = {"error": "Ya existe un tamaño con ese nombre en este producto"}
    if index is not None:
        detail["variant_index"] = index
    raise HTTPException(status.HTTP_409_CONFLICT, detail=detail)


def _replace_recipe(
    db: Session, variant_id: UUID, items: list[RecipeItemIn], index: int
) -> None:
    """Reemplazo total de la receta de una presentación -- mismo patrón que `set_recipe`
    (`catalog/router.py`), con `variant_index` en cualquier error."""
    db.execute(RecipeItem.__table__.delete().where(RecipeItem.product_variant_id == variant_id))
    seen: set[UUID] = set()
    for it in items:
        if it.inventory_item_id in seen:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "Insumo repetido en la receta", "variant_index": index},
            )
        if db.get(InventoryItem, it.inventory_item_id) is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={"error": "Inventory item not found", "variant_index": index},
            )
        seen.add(it.inventory_item_id)
        db.add(RecipeItem(
            product_variant_id=variant_id,
            inventory_item_id=it.inventory_item_id,
            quantity=it.quantity,
        ))


def _replace_option_groups(
    db: Session, variant_id: UUID, groups: list[VariantOptionGroupIn], index: int
) -> None:
    """Reemplazo total de los grupos de opciones de una presentación -- mismo patrón que
    `set_variant_option_groups` (`catalog/router.py`), con `variant_index` en cualquier error."""
    db.execute(
        VariantOptionGroup.__table__.delete().where(
            VariantOptionGroup.product_variant_id == variant_id
        )
    )
    seen: set[UUID] = set()
    for g in groups:
        if g.option_group_id in seen:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "Grupo de opciones repetido en esta presentación",
                    "variant_index": index,
                },
            )
        group = db.get(OptionGroup, g.option_group_id)
        if group is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={"error": "Option group not found", "variant_index": index},
            )
        if not group.active:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": f"El grupo «{group.name}» está inactivo", "variant_index": index},
            )
        seen.add(g.option_group_id)
        db.add(VariantOptionGroup(
            product_variant_id=variant_id,
            option_group_id=g.option_group_id,
            min_select=g.min_select,
            max_select=g.max_select,
            quantity_per_option=g.quantity_per_option,
        ))


def _resolve_presentation(
    db: Session,
    product_id: UUID,
    presentation_id: UUID | None,
    index: int | None = None,
    *,
    current: ProductVariant | None = None,
) -> Presentation:
    """Presentación del catálogo (spec 083) que una entrada de variante pide asociar
    (spec 084, A-79). `None` significa "Presentación única" (`default_presentation`).

    `current` es la variante que se está actualizando (si la hay): puede seguir usando
    una presentación que se desactivó después, y no se detecta a sí misma como el "otro"
    uso en conflicto (FR-006).
    """
    if presentation_id is None:
        presentation = default_presentation(db)
    else:
        presentation = db.get(Presentation, presentation_id)
        keeps_current = current is not None and current.presentation_id == presentation_id
        if presentation is None or not (presentation.active or keeps_current):
            detail = {"error": "La presentación seleccionada no existe o está inactiva"}
            if index is not None:
                detail["variant_index"] = index
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)
    dup = variante_duplicada(
        db, product_id, presentation.id,
        exclude_id=current.id if current is not None else None,
    )
    if dup is not None:
        _raise_presentation_conflict(dup, index)
    return presentation


def _save_variant_entry(
    db: Session,
    product: Product,
    entry: VariantSaveIn,
    index: int,
    existing_by_id: dict[UUID, ProductVariant],
) -> ProductVariant:
    """Crea o actualiza una presentación dentro de un guardado consolidado (spec 043, FR-001/
    FR-002). `existing_by_id` son las presentaciones que ya pertenecen a `product` (para poder
    rechazar un `id` que no le pertenece). `index` es la posición de `entry` en la lista `variants`
    del body -- se agrega a cualquier error para identificar qué presentación falló (FR-004).

    No asigna el `display_order` final (lo hace `_assign_display_orders` una vez resueltas todas
    las entradas) ni hace `commit()` -- el llamador controla la transacción completa.

    spec 084 (A-79): la variante no tiene nombre propio; su nombre es el de su presentación
    (`entry.presentation_id`, `None` = "Presentación única"). Cambiar el nombre de una variante
    es cambiarle la presentación.
    """
    current = None
    if entry.id is not None:
        current = existing_by_id.get(entry.id)
        if current is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "Variant not found",
                    "variant_index": index,
                    "variant_id": str(entry.id),
                },
            )
    presentation = _resolve_presentation(
        db, product.id, entry.presentation_id, index, current=current
    )

    # spec 099 (FR-008): la etiqueta que resultaría para esta entrada no puede coincidir
    # con la de otra variante activa del mismo producto -- solo si esta misma entrada
    # queda activa (una entrada que se guarda desactivada no le quita el nombre a nadie).
    if entry.active:
        entry_display_name = entry.name or presentation.name
        dup_nombre = display_name_duplicado(
            db, product.id, entry_display_name,
            exclude_id=current.id if current is not None else None,
        )
        if dup_nombre is not None:
            _raise_display_name_conflict(index)

    if current is None:
        if entry.sku is not None:
            _ensure_sku_unique(db, entry.sku, index)
        sku = entry.sku or _unique_sku(db, f"{_slug(product.name)}-{_slug(presentation.name)}")
        variant = ProductVariant(
            product_id=product.id,
            presentation_id=presentation.id,
            price=entry.price,
            sku=sku,
            active=entry.active,
            name=entry.name,
            display_order=_next_display_order(db, product.id),
        )
        db.add(variant)
        db.flush()
    else:
        variant = current
        if entry.sku is not None and entry.sku != variant.sku:
            _ensure_sku_unique(db, entry.sku, index, exclude_id=variant.id)
            variant.sku = entry.sku
        variant.presentation_id = presentation.id
        variant.price = entry.price
        variant.active = entry.active
        variant.name = entry.name
        db.flush()

    _replace_recipe(db, variant.id, entry.recipe, index)
    _replace_option_groups(db, variant.id, entry.option_groups, index)
    return variant


def _assign_display_orders(
    db: Session, product_id: UUID, resolved: list[ProductVariant]
) -> None:
    """Asigna `display_order = 1..N` a `resolved` según su posición en la lista, y reacomoda
    cualquier otra fila del mismo producto (p. ej. presentaciones inactivas que este guardado no
    tocó) por encima de ese rango.

    `UNIQUE(product_id, display_order)` aplica a **todas** las filas del producto, activas o no
    -- una presentación desactivada puede seguir ocupando un valor bajo (p. ej. 2) que el nuevo
    conjunto activo también necesita, así que no basta con reasignar solo `resolved` (a diferencia
    de `reorder_variants` arriba, que asume que el conjunto activo ya no choca con nada porque
    nunca lo reordena junto con altas/bajas en la misma llamada). Mismo patrón de dos pasadas
    (negativos primero, definitivos después), extendido a todas las filas del producto para no
    violar la constraint en ningún estado intermedio (spec 043, research.md Decisión 2). No hace
    `commit()` -- el llamador controla la transacción completa.
    """
    resolved_ids = {v.id for v in resolved}
    others = db.execute(
        select(ProductVariant).where(
            ProductVariant.product_id == product_id, ProductVariant.id.notin_(resolved_ids)
        )
    ).scalars().all()
    others.sort(key=lambda v: v.display_order)

    for i, variant in enumerate([*resolved, *others], start=1):
        variant.display_order = -i
    db.flush()

    for i, variant in enumerate(resolved, start=1):
        variant.display_order = i
    offset = len(resolved)
    for i, variant in enumerate(others, start=1):
        variant.display_order = offset + i
    db.flush()
