"""Helpers de línea compartidos entre carrito (Fase 3) y consolidación (Fase 4):
validación de la selección de opciones y chequeo preventivo de disponibilidad
contra stock único.

La disponibilidad NO reserva ni bloquea (a diferencia de `record_movement` en
inventory/stock.py, que sí bloquea y descuenta en la venta/consolidación); es
un chequeo best-effort de UX que puede quedar obsoleto para cuando se
consolide.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal
from typing import Sequence
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crud import get_or_404
from app.models.inventory_item import InventoryItem
from app.models.option import Option
from app.models.option_group import OptionGroup
from app.models.product_variant import ProductVariant
from app.models.variant_option_group import VariantOptionGroup

from app.catalog_engine.core import ChosenOption, _exige_maximo
from app.catalog_engine.consumption import load_variant_groups
from app.api.v1.catalog.schemas import OptionSelectionIn

logger = logging.getLogger(__name__)


def load_valid_options(
    db: Session,
    selections: list[OptionSelectionIn],
    *,
    variant: ProductVariant | None = None,
    quantity: int = 1,
) -> list[ChosenOption]:
    """Carga las opciones elegidas validando existencia y que estén activas, junto con
    la cantidad que trajo cada una (spec 065).

    Pasar `variant` valida además la selección contra los grupos del producto (ver
    `validate_option_selection`). Es opcional solo por compatibilidad con los pocos
    llamadores que aún no tienen la variante a mano; **pasarla siempre que se pueda**.

    `quantity` es la cantidad de producto de la línea (spec 117, D2): escala los topes
    `max_quantity_per_option`/`max_total_quantity` de los grupos "cantidad". Se reenvía a
    `validate_option_selection` solo cuando hay `variant` (igual que antes).
    """
    seen: set[UUID] = set()
    chosen: list[ChosenOption] = []
    for sel in selections:
        if sel.option_id in seen:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Opción repetida en la selección: {sel.option_id}",
            )
        seen.add(sel.option_id)
        option = get_or_404(db, Option, sel.option_id, f"Option {sel.option_id} not found")
        if not option.active:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"Opción inactiva: {sel.option_id}"
            )
        chosen.append(ChosenOption(option, sel.quantity))
    if variant is not None:
        validate_option_selection(db, variant, chosen, quantity=quantity)
    return chosen


def grupos_que_descuentan(db: Session, links: Sequence[VariantOptionGroup]) -> set[UUID]:
    """De estos grupos, cuáles mueven inventario al elegir una opción.

    Hay **dos** vías y contar solo una deja fuera la mitad del catálogo:

    - `variant_option_groups.quantity_per_option`: el tamaño reparte una cantidad
      por cada opción elegida (la copa grande, 120 g de cada sabor);
    - `options.item_quantity`: la opción descuenta lo suyo en todo el catálogo.

    Basta con que la opción descuente por su cuenta para que el grupo importe:
    elegir menos opciones descuenta menos, con independencia de dónde esté
    escrita la cantidad.
    """
    gids = {l.option_group_id for l in links}
    if not gids:
        return set()
    por_grupo = {l.option_group_id for l in links if l.quantity_per_option > 0}
    # spec 064, FR-009 [corrige A-32/RN-CAT-39]: mismo criterio de tres condiciones que
    # `group_discounts` (app/catalog_engine/consumption.py) -- una opción con cantidad
    # puesta pero sin insumo enlazado o desactivada NO cuenta como "descuenta inventario"
    # aquí tampoco. Antes de esta spec, esta función exigía solo `item_quantity > 0`,
    # discrepando de `group_discounts` (que ya exigía además `active` e
    # `inventory_item_id` no nulo) -- esa discrepancia hoy podía forzar "elige
    # exactamente el máximo" sobre un grupo a medio configurar que, al confirmar la
    # venta, `ensure_lines_consume_inventory` ya trataba como si no descontara nada.
    por_opcion = set(db.execute(
        select(Option.option_group_id)
        .where(
            Option.option_group_id.in_(gids),
            Option.active.is_(True),
            Option.inventory_item_id.is_not(None),
            Option.item_quantity > 0,
        )
        .distinct()
    ).scalars().all())
    return por_grupo | por_opcion


def validate_option_selection(
    db: Session, variant: ProductVariant, options: Sequence[ChosenOption], *, quantity: int = 1
) -> None:
    """Comprueba que la selección respeta los grupos que ofrece **esta variante** y sus
    `min_select`/`max_select` (grupos "conteo") o sus topes de cantidad (grupos
    "cantidad", spec 065).

    `quantity` es la cantidad de producto de la línea (spec 117, D2): los topes
    `max_quantity_per_option`/`max_total_quantity` de un grupo "cantidad" se escalan por
    ella (`tope × quantity`). El default `1` reproduce el tope fijo de hoy para quien no
    lo pase (solo tests aislados de esta función -- ningún camino de producción llega
    aquí sin pasar la cantidad real de la línea).

    Hasta hace poco esto no se validaba en ningún camino de pedido (solo el frontend), y
    era un fallo de UX tolerable. **Con consumo por opción deja de serlo**: lo que se
    descuenta es `nº de opciones elegidas × quantity_per_option`, así que aceptar cinco
    opciones en un grupo `max_select=1` descuenta cinco veces el helado.

    Por eso el rodaje es asimétrico: `STRICT_OPTION_SELECTION` gobierna el resto del
    catálogo, pero **los grupos que descuentan se validan siempre**. Ahí no es
    cosmético, es inventario.

    Y por lo mismo un grupo que descuenta y es obligatorio exige el **máximo**, no el
    mínimo (ver `_exige_maximo`): el error simétrico al de arriba es elegir un sabor
    para un helado de tres bolas, que sirve tres y descuenta una.

    Todo sale de la misma tabla y del mismo nivel (la variante). Antes los bounds venían
    del producto y el consumo de la variante, así que un tamaño podía exigir un número
    de sabores pensado para otro.
    """
    links = load_variant_groups(db, variant.id)
    bounds = {l.option_group_id: (l.min_select, l.max_select) for l in links}
    consumen = grupos_que_descuentan(db, links)
    # spec 065: un grupo "cantidad" nunca es obligatorio, sin importar min_select del
    # VariantOptionGroup (research.md Decisión 3) -- se necesita el modo de TODOS los
    # grupos ofrecidos por la variante, no solo los que trajeron alguna ChosenOption,
    # para que un grupo "cantidad" sin ninguna unidad elegida no caiga en la rama de
    # "obligatorio sin elegir nada" de abajo.
    selection_modes = dict(db.execute(
        select(OptionGroup.id, OptionGroup.selection_mode).where(
            OptionGroup.id.in_(bounds.keys())
        )
    ).all())

    # spec 065: separar por selection_mode del grupo de cada ChosenOption *antes* de la
    # lógica de conteo -- un grupo "cantidad" nunca entra en ella (no tiene mínimo
    # posible, research.md Decisión 3), y una opción de un grupo "conteo" con
    # quantity != 1 se rechaza aquí mismo en vez de perderse en silencio.
    conteo_options: list[Option] = []
    cantidad_por_grupo: dict[UUID, dict[UUID, int]] = defaultdict(dict)
    group_cache: dict[UUID, OptionGroup] = {}
    problems: list[tuple[UUID | None, str]] = []
    # spec 065: `quantity>1` en "conteo" y los topes de "cantidad" son "422
    # inmediato" (research.md Decisión 3) -- nunca sujetos a la tolerancia de
    # `STRICT_OPTION_SELECTION`/"grupo no descuenta" que sí aplica a `bounds`
    # (comportamiento heredado, sin cambios). Se acumulan aparte para no
    # mezclarse con esa tolerancia.
    hard_problems: list[tuple[UUID | None, str]] = []

    for chosen_option in options:
        option = chosen_option.option
        group = option.option_group
        group_cache[group.id] = group
        if group.selection_mode == "cantidad":
            cantidad_por_grupo[group.id][option.id] = (
                cantidad_por_grupo[group.id].get(option.id, 0) + chosen_option.quantity
            )
        else:
            if chosen_option.quantity != 1:
                hard_problems.append(
                    (group.id, "esta opción no admite más de una unidad")
                )
                continue
            conteo_options.append(option)

    chosen: dict[UUID, int] = defaultdict(int)
    for option in conteo_options:
        chosen[option.option_group_id] += 1

    for gid, count in chosen.items():
        if gid not in bounds:
            problems.append((gid, "no está disponible para esta presentación"))
            continue
        lo, hi = bounds[gid]
        if _exige_maximo(gid, lo, consumen):
            if count != hi:
                problems.append(
                    (gid, f"exige exactamente {hi} opción(es), se enviaron {count}")
                )
        elif count > hi:
            problems.append((gid, f"admite como máximo {hi} opción(es), se enviaron {count}"))
        elif count < lo:
            problems.append((gid, f"exige al menos {lo} opción(es), se enviaron {count}"))

    for gid, (lo, hi) in bounds.items():
        if lo > 0 and gid not in chosen and selection_modes.get(gid) != "cantidad":
            faltan = hi if _exige_maximo(gid, lo, consumen) else lo
            problems.append((gid, f"es obligatorio: elige {faltan} opción(es)"))

    # spec 065, FR-008/FR-009: topes opcionales por opción y por total del grupo. Nunca
    # se exige un mínimo -- un grupo "cantidad" jamás aparece en la lista de "obligatorio
    # sin elegir nada" de arriba.
    for gid, por_opcion in cantidad_por_grupo.items():
        group = group_cache[gid]
        total = sum(por_opcion.values())
        if group.max_quantity_per_option is not None:
            tope_por_opcion = group.max_quantity_per_option * quantity
            for option_id, qty in por_opcion.items():
                if qty > tope_por_opcion:
                    hard_problems.append(
                        (gid, f"admite como máximo {tope_por_opcion} "
                              f"unidad(es) por opción, se pidieron {qty}")
                    )
        if group.max_total_quantity is not None:
            tope_total = group.max_total_quantity * quantity
            if total > tope_total:
                hard_problems.append(
                    (gid, f"admite como máximo {tope_total} unidad(es) en total, "
                          f"se pidieron {total}")
                )

    if not problems and not hard_problems:
        return

    # Un problema en un grupo que descuenta inventario es siempre bloqueante.
    blocking = [p for p in problems if p[0] in consumen]
    if not blocking and not hard_problems and not settings.STRICT_OPTION_SELECTION:
        logger.warning(
            "Selección de opciones inválida (variante %s), tolerada por "
            "STRICT_OPTION_SELECTION=False: %s",
            variant.id, "; ".join(msg for _gid, msg in problems),
        )
        return

    relevant = hard_problems + blocking if (hard_problems or blocking) else problems
    names = {
        gid: name for gid, name in db.execute(
            select(OptionGroup.id, OptionGroup.name).where(
                OptionGroup.id.in_([gid for gid, _ in relevant if gid is not None])
            )
        ).all()
    }
    detalle = "; ".join(
        f"«{names.get(gid, 'grupo')}» {msg}" for gid, msg in relevant
    )
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"error": f"Selección de opciones inválida: {detalle}", "grupos": detalle},
    )


def mark_per_line(
    db: Session, options: list[ChosenOption], *, qr_addons_per_line: bool = False
) -> list[ChosenOption]:
    """Marca `per_line=True` en las opciones que se cobran/consumen una vez por línea,
    en vez de multiplicadas por la cantidad de producto (spec 117, D1).

    Unión de dos ejes independientes, ninguno apaga al otro:

    - `qr_addons_per_line AND group.pricing_type == 'con_recargo'`: la regla de la spec
      089 (A-94), que solo aplica en el Menú QR y se reversa con
      `settings.QR_ADDONS_PER_LINE=False`.
    - `group.selection_mode == 'cantidad'`: la regla nueva de esta spec (A-126) -- un
      grupo "Por cantidad" (spec 065) siempre se cobra/consume por línea, en **todo**
      canal, sin bandera de reversa. El modo "Conteo" nunca entra por este eje
      (`FR-013`).
    """
    if not options:
        return options
    group_ids = {c.option.option_group_id for c in options}
    rows = db.execute(
        select(OptionGroup.id, OptionGroup.pricing_type, OptionGroup.selection_mode).where(
            OptionGroup.id.in_(group_ids)
        )
    ).all()
    por_grupo = {gid: (pricing_type, selection_mode) for gid, pricing_type, selection_mode in rows}
    return [
        c._replace(per_line=(
            (qr_addons_per_line and por_grupo.get(c.option.option_group_id, (None, None))[0] == "con_recargo")
            or por_grupo.get(c.option.option_group_id, (None, None))[1] == "cantidad"
        ))
        for c in options
    ]


def check_availability(
    db: Session, required: dict[UUID, Decimal], *, extra_context: str = ""
) -> None:
    """Chequeo preventivo (sin lock ni reserva): rechaza con 409 si algún insumo
    no tiene `current_stock` suficiente para el consumo `required` agregado."""
    for item_id, need in required.items():
        if need <= 0:
            continue
        item = db.get(InventoryItem, item_id)
        if item is None:
            continue
        if Decimal(item.current_stock) < need:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": "Stock insuficiente",
                    "insumo": item.name,
                    "disponible": str(item.current_stock),
                    "requerido": str(need),
                    "contexto": extra_context,
                },
            )


def ensure_product_available(variant: ProductVariant) -> None:
    """Spec 093 (FR-020, research.md D7): rechaza agregar/editar un ítem de un solo
    producto marcado "Agotado" (`Product.available=False`, independiente de
    `active`). Mismo shape que ya usa `sold_out_detail()` para el agotado por
    inventario (`app/catalog_engine/consumption.py`) -- un único producto nombrado,
    no una lista (ver `ensure_products_available` para el caso de varios a la vez)."""
    if not variant.product.available:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": f"«{variant.product.name}» está agotado y no se puede agregar al pedido",
                "producto": variant.product.name,
            },
        )


def ensure_products_available(variants: Sequence[ProductVariant]) -> None:
    """Spec 093 (FR-021/FR-022, research.md D7): barrido de **todos** los productos
    (uno por variante) antes de confirmar un carrito/pedido con varios ítems a la
    vez -- a diferencia de `ensure_product_available`, nombra todos los agotados
    encontrados en un solo 409 en vez de rechazar por el primero y obligar a
    reintentar uno por uno."""
    agotados = sorted({v.product.name for v in variants if not v.product.available})
    if agotados:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": f"Los siguientes productos están agotados: {', '.join(agotados)}",
                "productos_agotados": agotados,
            },
        )
