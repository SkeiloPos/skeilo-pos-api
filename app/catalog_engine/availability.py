"""Motor único de "pedibilidad" (spec 148, research.md D3).

Calcula, a tres niveles —opción, presentación (variante activa) y producto—
si algo se puede pedir *hoy*, con receta e inventario incluidos. Antes de esta
spec, `_build_menu` (`app/api/v1/menu/router.py`) solo miraba las opciones y
los grupos obligatorios (`_option_availability`); no miraba la receta. Ese es
el único cambio de comportamiento intencional (anomalía A-154,
`specs/000-reconocimiento/registro-de-anomalias.md`) — el resto de la lógica
de opciones se mueve **sin cambios** (prueba de equivalencia en
`test_availability_engine.py`).

Lo consumen el menú (`_build_menu`), el snapshot de reconciliación
(`GET /menu/availability`), el evento `availability.changed` (gancho de
sesión y publicación manual) y, más adelante, el 409 unificado
(`ensure_orderable`).

Número de consultas SQL **constante**, sin importar el tamaño del catálogo:
seis consultas agregadas, nunca una por producto/variante/opción.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Sequence

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from uuid import UUID

from app.models.inventory_item import InventoryItem
from app.models.option import Option
from app.models.option_group import OptionGroup
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.recipe_item import RecipeItem
from app.models.variant_option_group import VariantOptionGroup

from app.catalog_engine.core import ChosenOption
from app.catalog_engine.consumption import plan_line_consumption, required_consumption


@dataclass(frozen=True)
class ProductAvailability:
    sold_out: bool
    available: bool


@dataclass(frozen=True)
class VariantAvailability:
    product_id: UUID
    available: bool


@dataclass
class CatalogAvailability:
    products: dict[UUID, ProductAvailability] = field(default_factory=dict)
    variants: dict[UUID, VariantAvailability] = field(default_factory=dict)
    options: dict[UUID, bool] = field(default_factory=dict)


def _option_availability_by_stock(
    db: Session, *, stock_override: dict[UUID, Decimal],
) -> dict[UUID, bool]:
    """`{option_id: hay stock}` — lógica de `_option_availability` (spec 093),
    movida sin cambios (research.md D3). Dos consultas constantes, sin N+1
    por producto; `stock_override` sustituye `InventoryItem.current_stock`
    por id (lo usa el gancho de inventario, research.md D4)."""
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
            InventoryItem.id, InventoryItem.current_stock, InventoryItem.active,
        ).join(InventoryItem, InventoryItem.id == Option.inventory_item_id)
    ).all()

    out: dict[UUID, bool] = {}
    for option_id, group_id, item_qty, item_id, stock, item_active in rows:
        effective_stock = stock_override.get(item_id, Decimal(stock))
        mayor, alguno_sin_definir = por_grupo.get(group_id, (Decimal(0), True))
        need = max(mayor, Decimal(item_qty)) if alguno_sin_definir else mayor
        out[option_id] = bool(item_active) and (need <= 0 or effective_stock >= need)
    return out


def compute_availability(
    db: Session,
    *,
    stock: dict[UUID, Decimal] | None = None,
    scope_item_ids: Iterable[UUID] | None = None,
    scope_product_ids: Iterable[UUID] | None = None,
) -> CatalogAvailability:
    """Calcula la disponibilidad del catálogo activo completo (o de un
    subconjunto con `scope_item_ids`/`scope_product_ids`).

    - **Opción**: `_option_availability_by_stock` (sin cambios).
    - **Presentación (variante activa) "pedible"**: NOT(algún `RecipeItem`
      con stock < cantidad, solo si `Product.tracks_inventory`) AND
      NOT(algún grupo obligatorio `min_select>0` modo `conteo` sin ninguna
      opción disponible — regla actual, research.md D3, no corrige
      `min_select>1`).
    - **Producto `available`**: alguna presentación activa pedible.
    - **Producto `sold_out`**: `not Product.available` — independiente del
      inventario (FR-011); nunca cambia por stock.

    `scope_item_ids`/`scope_product_ids` restringen el resultado devuelto a
    los elementos dependientes de esos insumos/productos (mismo estado que el
    cálculo completo para esos elementos) — pensado para el gancho de sesión
    y la publicación manual, que solo necesitan lo que cambió.
    """
    stock_override = dict(stock or {})
    scope_items = set(scope_item_ids) if scope_item_ids is not None else None
    scope_products = set(scope_product_ids) if scope_product_ids is not None else None

    options = _option_availability_by_stock(db, stock_override=stock_override)

    product_filters = [Product.active.is_(True)]
    if scope_products is not None:
        product_filters.append(Product.id.in_(scope_products))
    products_rows = db.execute(
        select(Product.id, Product.available, Product.tracks_inventory)
        .where(*product_filters)
    ).all()
    tracks_inventory_by_product = {pid: bool(ti) for pid, _, ti in products_rows}

    variant_filters = [ProductVariant.active.is_(True)]
    if scope_products is not None:
        variant_filters.append(ProductVariant.product_id.in_(scope_products))
    variant_rows = db.execute(
        select(ProductVariant.id, ProductVariant.product_id).where(*variant_filters)
    ).all()
    variant_ids = [vid for vid, _ in variant_rows]
    variant_product = {vid: pid for vid, pid in variant_rows}

    recipe_by_variant: dict[UUID, list[tuple[UUID, Decimal]]] = {}
    if variant_ids:
        for variant_id, item_id, qty in db.execute(
            select(RecipeItem.product_variant_id, RecipeItem.inventory_item_id, RecipeItem.quantity)
            .where(RecipeItem.product_variant_id.in_(variant_ids))
        ).all():
            recipe_by_variant.setdefault(variant_id, []).append((item_id, Decimal(qty)))

    item_ids_in_recipes = {item_id for lines in recipe_by_variant.values() for item_id, _ in lines}
    stock_by_item: dict[UUID, Decimal] = {}
    if item_ids_in_recipes:
        for item_id, current_stock in db.execute(
            select(InventoryItem.id, InventoryItem.current_stock)
            .where(InventoryItem.id.in_(item_ids_in_recipes))
        ).all():
            stock_by_item[item_id] = Decimal(current_stock)

    group_link_rows = db.execute(
        select(
            VariantOptionGroup.product_variant_id, VariantOptionGroup.option_group_id,
            VariantOptionGroup.min_select, OptionGroup.selection_mode,
        )
        .join(OptionGroup, OptionGroup.id == VariantOptionGroup.option_group_id)
        .where(OptionGroup.active.is_(True))
    ).all() if variant_ids else []

    option_rows = db.execute(
        select(Option.id, Option.option_group_id).where(Option.active.is_(True))
    ).all()
    options_by_group: dict[UUID, list[UUID]] = {}
    for option_id, group_id in option_rows:
        options_by_group.setdefault(group_id, []).append(option_id)

    def _recipe_ok(variant_id: UUID, product_id: UUID) -> bool:
        if not tracks_inventory_by_product.get(product_id, True):
            return True
        for item_id, qty in recipe_by_variant.get(variant_id, []):
            effective = stock_override.get(item_id, stock_by_item.get(item_id, Decimal(0)))
            if effective < qty:
                return False
        return True

    def _groups_ok(variant_id: UUID) -> bool:
        for v_id, group_id, min_select, selection_mode in group_link_rows:
            if v_id != variant_id:
                continue
            if selection_mode == "cantidad" or min_select <= 0:
                continue
            group_options = options_by_group.get(group_id, [])
            if not any(options.get(oid, True) for oid in group_options):
                return False
        return True

    variants_out: dict[UUID, VariantAvailability] = {}
    pedible_by_product: dict[UUID, bool] = {}
    for variant_id, product_id in variant_rows:
        v_available = _recipe_ok(variant_id, product_id) and _groups_ok(variant_id)
        variants_out[variant_id] = VariantAvailability(product_id=product_id, available=v_available)
        pedible_by_product[product_id] = pedible_by_product.get(product_id, False) or v_available

    products_out: dict[UUID, ProductAvailability] = {}
    for product_id, manual_available, _ in products_rows:
        products_out[product_id] = ProductAvailability(
            sold_out=not manual_available,
            available=pedible_by_product.get(product_id, False),
        )

    if scope_items is not None:
        affected_variants = {
            vid for vid, lines in recipe_by_variant.items()
            if any(item_id in scope_items for item_id, _ in lines)
        }
        # Opciones cuyo insumo está en el scope.
        option_item_rows = db.execute(
            select(Option.id, Option.inventory_item_id).where(Option.active.is_(True))
        ).all()
        affected_options = {
            oid for oid, item_id in option_item_rows if item_id in scope_items
        }
        affected_variants |= {
            vid for vid, group_id, *_ in group_link_rows
            if any(oid in affected_options for oid in options_by_group.get(group_id, []))
        }
        affected_products = {variant_product[vid] for vid in affected_variants if vid in variant_product}

        variants_out = {vid: v for vid, v in variants_out.items() if vid in affected_variants}
        options = {oid: a for oid, a in options.items() if oid in affected_options}
        products_out = {pid: p for pid, p in products_out.items() if pid in affected_products}

    return CatalogAvailability(products=products_out, variants=variants_out, options=options)


def _label_for(kind: str, elem_id: UUID, after: CatalogAvailability) -> dict:
    if kind == "product":
        state = after.products[elem_id]
        return {"kind": "product", "id": elem_id, "sold_out": state.sold_out, "available": state.available}
    if kind == "variant":
        state = after.variants[elem_id]
        return {"kind": "variant", "id": elem_id, "product_id": state.product_id, "available": state.available}
    state = after.options[elem_id]
    return {"kind": "option", "id": elem_id, "available": state}


def diff_availability(before: CatalogAvailability, after: CatalogAvailability) -> list[dict]:
    """Solo los elementos cuyo estado "pedible" cambió entre `before` y
    `after` (FR-009), con la forma de `contracts/availability-event.md`. Un
    elemento presente en un solo lado se ignora (no hay "antes" o "después"
    con el que comparar).

    En `product` compara el `available` **derivado** (la marca manual no
    cambia por inventario, FR-011) y toma `sold_out` del `after` (por si
    cambió en la misma operación, aunque hoy nunca ocurre a la vez)."""
    changes: list[dict] = []

    for pid, after_state in after.products.items():
        before_state = before.products.get(pid)
        if before_state is None:
            continue
        if before_state.available != after_state.available:
            changes.append(_label_for("product", pid, after))

    for vid, after_state in after.variants.items():
        before_state = before.variants.get(vid)
        if before_state is None:
            continue
        if before_state.available != after_state.available:
            changes.append(_label_for("variant", vid, after))

    for oid, after_available in after.options.items():
        if oid not in before.options:
            continue
        if before.options[oid] != after_available:
            changes.append(_label_for("option", oid, after))

    return changes


# ---------------------------------------------------------------------------
# 409 unificado (spec 148, research.md D6/D7; A-153; contracts/sold-out-conflict.md)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SoldOutItem:
    """Un elemento no pedible, listo para nombrarse en el 409 unificado."""

    id: UUID
    tipo: str  # "producto" | "presentacion" | "opcion"
    nombre: str
    producto_id: UUID
    motivo: str  # "manual" | "inventario" | "cantidad"


@dataclass
class OrderableLine:
    """Una línea a validar antes de crear/mutar un pedido o carrito."""

    variant_id: UUID
    quantity: int
    options: Sequence[ChosenOption] = ()
    topping_product_ids: Sequence[UUID] = ()


_TIPO_ORDEN = {"producto": 0, "presentacion": 1, "opcion": 2}


def sold_out_conflict(items: Sequence[SoldOutItem], *, legacy: dict | None = None) -> HTTPException:
    """Arma el `HTTPException(409)` unificado (contracts/sold-out-conflict.md):
    lista completa de `items`, sin duplicados, orden estable (producto →
    presentación → opción, luego por nombre), texto de `error` en el formato
    de FR-024 y las claves heredadas de `legacy` (FR-023, compatibilidad)."""
    seen: set[tuple[str, UUID]] = set()
    unique: list[SoldOutItem] = []
    for it in items:
        key = (it.tipo, it.id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)
    unique.sort(key=lambda it: (_TIPO_ORDEN.get(it.tipo, 9), it.nombre))

    nombres = [it.nombre for it in unique]
    if len(nombres) == 1:
        error = f"Se agotó: {nombres[0]}. Quítalo para continuar"
    else:
        error = f"Se agotaron: {', '.join(nombres)}. Quítalos para continuar"

    detail: dict = {
        "error": error,
        "productos_agotados": nombres,
        "agotados": [
            {
                "id": it.id, "tipo": it.tipo, "nombre": it.nombre,
                "producto_id": it.producto_id, "motivo": it.motivo,
            }
            for it in unique
        ],
    }
    if legacy:
        detail.update(legacy)
    return HTTPException(status.HTTP_409_CONFLICT, detail=detail)


def _bottleneck_insumo(
    db: Session, variant_id: UUID, quantity: int, options: Sequence[ChosenOption],
) -> tuple[str, Decimal, Decimal] | None:
    """Para UNA línea, el primer insumo (receta u opción) que no alcanza:
    `(nombre, disponible, requerido)`, o `None` si todos alcanzan. Usado solo
    para enriquecer el 409 de un único elemento (FR-023: `insumo`,
    `disponible`, `requerido` del carrito QR)."""
    needed = required_consumption(db, variant_id, quantity, options)
    if not needed:
        return None
    rows = db.execute(
        select(InventoryItem.id, InventoryItem.name, InventoryItem.current_stock)
        .where(InventoryItem.id.in_(needed.keys()))
    ).all()
    for item_id, name, stock in rows:
        disponible = Decimal(stock)
        requerido = needed[item_id]
        if disponible < requerido:
            return name, disponible, requerido
    return None


def stock_error_to_conflict(db: Session, exc, variant_id: UUID) -> HTTPException:
    """Convierte el `InsufficientStockError` (400) del guardia final de
    descuento (`record_movement`/`apply_adjustment`) en el 409 unificado --
    carrera entre `ensure_orderable` y el descuento real. Conserva
    `producto`/`insumo` del antiguo `sold_out_detail` (research.md D6)."""
    from app.catalog_engine.consumption import variant_label

    nombre = variant_label(db, variant_id)
    product_id = db.execute(
        select(ProductVariant.product_id).where(ProductVariant.id == variant_id)
    ).scalar_one_or_none()
    insumo = getattr(exc, "item_name", None)
    item = SoldOutItem(
        id=variant_id, tipo="presentacion", nombre=nombre,
        producto_id=product_id if product_id is not None else variant_id, motivo="inventario",
    )
    legacy = {"producto": nombre}
    if insumo:
        legacy["insumo"] = insumo
    return sold_out_conflict([item], legacy=legacy)


def stock_conflict_for_required(
    db: Session, required: dict[UUID, Decimal], item: SoldOutItem, *, extra_context: str = "",
) -> HTTPException | None:
    """Mismo chequeo que `check_availability` (pricing.py) pero devuelve el 409
    unificado con `agotados[]` nombrando `item` (la adición/opción que se
    valida) -- sin tocar `check_availability` en sí (research.md D6: la
    comparten los pagos complementarios, fuera del contrato de esta spec).
    Para `toppings.py`/`additions.py` (T072), que validan post-hoc el stock de
    UNA adición/opción. `None` si alcanza."""
    for item_id, need in required.items():
        if need <= 0:
            continue
        inv_item = db.get(InventoryItem, item_id)
        if inv_item is None:
            continue
        if Decimal(inv_item.current_stock) < need:
            legacy = {
                "producto": item.nombre, "insumo": inv_item.name,
                "disponible": str(inv_item.current_stock), "requerido": str(need),
                "contexto": extra_context,
            }
            return sold_out_conflict([item], legacy=legacy)
    return None


def ensure_orderable(
    db: Session,
    lines: Sequence[OrderableLine],
    *,
    toppings_inventory: bool = True,
    extra_context: str | None = None,
) -> None:
    """Chequeo preventivo unificado (research.md D6/D7): antes de mutar nada,
    rechaza con un único 409 (`sold_out_conflict`) si alguna línea tiene un
    producto marcado agotado a mano, una presentación/opción/adición no
    pedible por inventario, o pide más de lo que alcanza el insumo agregado
    entre TODAS las líneas.

    `toppings_inventory` (D19): si `False`, se omite SOLO el chequeo de stock
    de adiciones/toppings -- la marca manual de una adición sigue aplicando.
    El motor (`compute_availability`) nunca consulta el módulo `inventario`;
    cada llamador decide este flag igual que decide hoy si valida ese stock.
    """
    if not lines:
        return

    variant_ids = {ln.variant_id for ln in lines}
    variant_rows = db.execute(
        select(ProductVariant.id, ProductVariant.product_id).where(ProductVariant.id.in_(variant_ids))
    ).all()
    product_by_variant: dict[UUID, UUID] = dict(variant_rows)
    product_ids = set(product_by_variant.values())

    variant_label_rows = db.execute(
        select(ProductVariant.id, ProductVariant.name, Product.name, Product.id)
        .join(Product, Product.id == ProductVariant.product_id)
        .where(ProductVariant.id.in_(variant_ids))
    ).all()
    producto_nombre_by_variant: dict[UUID, tuple[str, UUID]] = {}
    for vid, v_name, p_name, p_id in variant_label_rows:
        label = v_name or p_name
        producto_nombre_by_variant[vid] = (f"{p_name} · {label}", p_id)

    avail = compute_availability(db, scope_product_ids=product_ids) if product_ids else CatalogAvailability()

    items: list[SoldOutItem] = []
    seen: set[tuple[str, UUID]] = set()

    def _add(it: SoldOutItem) -> None:
        key = (it.tipo, it.id)
        if key in seen:
            return
        seen.add(key)
        items.append(it)

    option_ids: set[UUID] = set()
    for ln in lines:
        for chosen in ln.options:
            option_ids.add(chosen.option.id)
    option_label_rows = db.execute(
        select(Option.id, Option.name).where(Option.id.in_(option_ids))
    ).all() if option_ids else []
    option_name_by_id = dict(option_label_rows)

    topping_ids: set[UUID] = set()
    for ln in lines:
        topping_ids |= set(ln.topping_product_ids)
    # La marca manual de una adición SIEMPRE se consulta (D19: `toppings_inventory`
    # solo gobierna el chequeo de STOCK); se calcula siempre que haya adiciones.
    topping_avail = compute_availability(db, scope_product_ids=topping_ids) if topping_ids else None
    topping_name_by_id: dict[UUID, str] = {}
    if topping_avail is not None:
        topping_rows = db.execute(
            select(Product.id, Product.name).where(Product.id.in_(topping_ids))
        ).all()
        topping_name_by_id = dict(topping_rows)

    # Índices de línea donde el motor (manual/inventario, no "cantidad") ya
    # marcó algo -- para no duplicar con el chequeo de "cantidad" agregada de
    # abajo, que es sobre una causa distinta (alcanza para 1, no para lo
    # pedido).
    line_has_engine_issue: set[int] = set()

    for idx, ln in enumerate(lines):
        product_id = product_by_variant.get(ln.variant_id)
        if product_id is None:
            continue
        prod_state = avail.products.get(product_id)
        if prod_state is not None and prod_state.sold_out:
            line_has_engine_issue.add(idx)
            _add(SoldOutItem(
                id=product_id, tipo="producto",
                nombre=db.get(Product, product_id).name, producto_id=product_id, motivo="manual",
            ))

        var_state = avail.variants.get(ln.variant_id)
        if var_state is not None and not var_state.available:
            line_has_engine_issue.add(idx)
            nombre, p_id = producto_nombre_by_variant.get(ln.variant_id, (str(ln.variant_id), product_id))
            _add(SoldOutItem(id=ln.variant_id, tipo="presentacion", nombre=nombre, producto_id=p_id, motivo="inventario"))

        for chosen in ln.options:
            option_id = chosen.option.id
            if not avail.options.get(option_id, True):
                line_has_engine_issue.add(idx)
                _, p_id = producto_nombre_by_variant.get(ln.variant_id, (None, product_id))
                p_name = db.get(Product, p_id).name if p_id else ""
                opt_name = option_name_by_id.get(option_id, str(option_id))
                _add(SoldOutItem(
                    id=option_id, tipo="opcion", nombre=f"{p_name} · {opt_name}",
                    producto_id=p_id, motivo="inventario",
                ))

        if topping_avail is not None:
            for tp_id in ln.topping_product_ids:
                tstate = topping_avail.products.get(tp_id)
                if tstate is None:
                    continue
                if tstate.sold_out:
                    motivo = "manual"
                elif toppings_inventory and not tstate.available:
                    motivo = "inventario"
                else:
                    continue
                line_has_engine_issue.add(idx)
                _add(SoldOutItem(
                    id=tp_id, tipo="producto",
                    nombre=topping_name_by_id.get(tp_id, str(tp_id)),
                    producto_id=tp_id, motivo=motivo,
                ))

    # Cantidad agregada: por cada insumo que no alcanza para TODAS las líneas
    # juntas, se marcan todas las líneas que lo consumen (D7) -- conservador,
    # no se intenta adivinar "cuál sobra". Se excluyen las líneas que ya
    # tienen un motivo manual/inventario (esa causa ya está nombrada arriba;
    # "cantidad" es específicamente "alcanza para 1, no para lo pedido").
    required: dict[UUID, Decimal] = defaultdict(Decimal)
    for idx, ln in enumerate(lines):
        if idx in line_has_engine_issue:
            continue
        for item_id, qty in required_consumption(db, ln.variant_id, ln.quantity, ln.options).items():
            required[item_id] += qty

    short_items: set[UUID] = set()
    if required:
        stock_rows = db.execute(
            select(InventoryItem.id, InventoryItem.current_stock).where(InventoryItem.id.in_(required.keys()))
        ).all()
        stock_by_item = {iid: Decimal(s) for iid, s in stock_rows}
        for item_id, need in required.items():
            if stock_by_item.get(item_id, Decimal(0)) < need:
                short_items.add(item_id)

    if short_items:
        for idx, ln in enumerate(lines):
            if idx in line_has_engine_issue:
                continue
            consumed = {
                cl.inventory_item_id
                for cl in plan_line_consumption(db, ln.variant_id, ln.quantity, ln.options)
            }
            if consumed & short_items:
                nombre, p_id = producto_nombre_by_variant.get(
                    ln.variant_id, (str(ln.variant_id), product_by_variant.get(ln.variant_id)),
                )
                _add(SoldOutItem(id=ln.variant_id, tipo="presentacion", nombre=nombre, producto_id=p_id, motivo="cantidad"))

    if not items:
        return

    legacy: dict | None = None
    if len(items) == 1:
        only = items[0]
        if only.motivo == "manual":
            legacy = {"producto": only.nombre.split(" · ")[0]}
        elif only.motivo in ("inventario", "cantidad"):
            matching_line = next((ln for ln in lines if ln.variant_id == only.id), lines[0])
            bottleneck = _bottleneck_insumo(db, matching_line.variant_id, matching_line.quantity, matching_line.options)
            # Mismo criterio que el antiguo `sold_out_detail` (consumption.py):
            # `producto` es la etiqueta COMBINADA "Producto · Presentación", no
            # solo el nombre del producto -- `only.nombre` ya trae esa forma
            # para `presentacion`/`opcion`.
            legacy = {"producto": only.nombre}
            if bottleneck is not None:
                insumo_name, disponible, requerido = bottleneck
                legacy["insumo"] = insumo_name
                if extra_context is not None:
                    legacy["disponible"] = str(disponible)
                    legacy["requerido"] = str(requerido)
                    legacy["contexto"] = extra_context

    raise sold_out_conflict(items, legacy=legacy)
