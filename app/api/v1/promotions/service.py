"""Promociones — spec 063: modelo por **conjunto explícito de variantes**.

Reemplaza el motor de las specs 012 (`evaluate` / `evaluate_detailed`) y 040
(`combined_discount_detailed` / `presentation_package_discount_for_lines`) por
**un único motor** `evaluate_variant_sets` (contracts/motor-y-persistencia.md §2):

- una promoción = `(type ∈ {percent, package_price}, value, min_qty)` + un
  conjunto de variantes (`promotion_variants`);
- por promoción vigente, se reúnen todas las unidades del pedido cuyas variantes
  pertenecen al conjunto, se arman `total // min_qty` **grupos completos** por
  **consumo codicioso descendente de precio**, se descuenta solo esos grupos y
  se reparte el descuento repartiendo el **importe cobrado** (residuo a la
  variante de id más alto);
- **no hay reconciliación**: el bloqueo de solape real de FR-014
  (`_guard_variant_overlap`) garantiza que una variante nunca está en dos
  promociones vigentes al mismo instante, así que `priority` desaparece.

`_tz`, `local_now`, `_in_time_window`, `_valid_now` se **conservan sin cambio de
cuerpo** (A-57 intacto).
"""
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, contains_eager, selectinload
from sqlalchemy.sql import Select

from app.core.config import settings
from app.core.models import Tenant
from app.core.timezone import resolve_timezone
from app.models.product import Product
from app.models.presentation import Presentation
from app.models.product_variant import ProductVariant
from app.models.promotion import (
    PROMOTION_TRANSITIONS, Promotion, PromotionRule, PromotionVariant,
)

# Tipos **vivos** que el motor evalúa. Las promociones que la migración `063a`
# dejó `finished` con un `type` viejo quedan fuera por `status != "active"`.
LIVE_TYPES = ("percent", "package_price")


# --------------------------- Hora local (sin cambio, A-57) ---------------------------

def _tz(tenant: Tenant | None = None) -> ZoneInfo:
    """Zona horaria de evaluación. Si el caller ya resolvió el `tenant`
    (spec 030, Historia 4/A-46), usa la suya; si no, conserva el respaldo
    global de instancia (`TENANT_TIMEZONE`)."""
    if tenant is not None:
        return resolve_timezone(tenant)
    return ZoneInfo(settings.TENANT_TIMEZONE)


def local_now(now: datetime | None = None, tenant: Tenant | None = None) -> datetime:
    """`now` como hora local **naive** del tenant. Acepta aware (convierte) o
    naive (asume ya local). `tenant` (spec 127): si el caller ya lo resolvió,
    se evalúa en SU zona horaria en vez del respaldo global de instancia."""
    if now is None:
        return datetime.now(_tz(tenant)).replace(tzinfo=None)
    if now.tzinfo is not None:
        return now.astimezone(_tz(tenant)).replace(tzinfo=None)
    return now


def _in_time_window(current: time, start: time | None, end: time | None) -> bool:
    """Ventana horaria, con soporte para cruce de medianoche."""
    if start is None and end is None:
        return True
    if start is None:
        return current <= end
    if end is None:
        return current >= start
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def _valid_now(promo: Promotion, now: datetime, tenant: Tenant | None = None) -> bool:
    """`now` puede venir en UTC o local; se normaliza a hora local del tenant.
    A-57 (atribución de día al cruzar medianoche) intacto. `tenant` (spec
    127): la zona horaria real del dueño de `promo`, si el caller la tiene
    resuelta."""
    now = local_now(now, tenant)
    if promo.status != "active":
        return False
    if promo.starts_at is not None and now < promo.starts_at:
        return False

    ref = now
    if (
        promo.start_time is not None
        and promo.end_time is not None
        and promo.start_time > promo.end_time
        and now.time() <= promo.end_time
    ):
        ref = now - timedelta(days=1)

    if promo.ends_at is not None and ref.date() > promo.ends_at.date():
        return False
    if promo.days_of_week:
        allowed = {d.strip() for d in promo.days_of_week.split(",") if d.strip()}
        if str(ref.weekday()) not in allowed:  # 0=lunes..6=domingo
            return False
    return _in_time_window(now.time(), promo.start_time, promo.end_time)


def _line_get(line, name: str, default=None):
    """Acceso uniforme a una línea, sea un `SaleLine` (atributos) o un dict
    enriquecido de `promo_lines_for` (claves)."""
    if isinstance(line, dict):
        return line.get(name, default)
    return getattr(line, name, default)


def _money(value: Decimal) -> str:
    """`$12.000` — formato de pesos colombianos con separador de miles."""
    return "$" + f"{int(value):,}".replace(",", ".")


# --------------------------- Motor por conjunto ---------------------------

@dataclass
class AppliedPromotion:
    promotion_id: UUID
    rule_id: UUID         # spec 063 (2026-09-01): qué regla generó este monto
    name: str            # snapshot: sobrevive al borrado de la promoción
    amount: Decimal      # descuento agregado de ESTA regla en ESTE cobro (>= 0)


@dataclass
class SetDiscountResult:
    total: Decimal = Decimal(0)                  # Σ by_line, redondeado una sola vez
    by_line: dict = field(default_factory=dict)  # line_index -> descuento (Decimal)
    applied: list = field(default_factory=list)  # list[AppliedPromotion]

    @property
    def single_promotion_id(self) -> UUID | None:
        return self.applied[0].promotion_id if len(self.applied) == 1 else None


def active_variant_set_rules(
    db: Session, now: datetime, tenant: Tenant | None = None
) -> list[PromotionRule]:
    """spec 063 (revisión 2026-09-01): reglas `percent` / `package_price` de
    promociones **activas y vigentes ahora** (`_valid_now`, hora local del
    tenant, evaluado sobre la `Promotion` dueña). Reemplaza a
    `active_variant_set_promotions` (motor-y-persistencia.md §2); usa el
    índice `ix_promotions_status_ends_at`. `tenant` (spec 127): el tenant
    propietario de las promociones que se están evaluando — si el caller lo
    tiene resuelto (siempre debería, ver `research.md` de spec 127), se usa
    su zona horaria real en vez del respaldo global de instancia."""
    today: date = local_now(now, tenant).date()
    stmt = (
        select(PromotionRule)
        .join(PromotionRule.promotion)
        .options(
            selectinload(PromotionRule.variants),
            contains_eager(PromotionRule.promotion),
        )
        .where(
            Promotion.status == "active",
            PromotionRule.type.in_(LIVE_TYPES),
            or_(Promotion.ends_at.is_(None), Promotion.ends_at >= today),
        )
    )
    return [
        r
        for r in db.execute(stmt).scalars().all()
        if _valid_now(r.promotion, now, tenant)
    ]


def _unit_sort_key(unit) -> tuple:
    """Orden de consumo codicioso (FR-008): precio unitario **descendente**,
    desempate `product_variant_id` ascendente y luego `line_id` ascendente. La
    comparación de `str(uuid)` coincide con la nativa (`.int`) por el ancho fijo
    del formato hex. Nunca usa la posición de la línea."""
    _idx, unit_price, pv_id, line_id = unit
    return (-unit_price, str(pv_id), str(line_id) if line_id is not None else "")


def _greedy_units(eligible_units: list, min_qty: int) -> list[list]:
    """Trocea las `grupos * min_qty` unidades más caras en bloques consecutivos
    de `min_qty` (FR-007, FR-008). El remanente (a precio normal) no se devuelve."""
    grupos = len(eligible_units) // min_qty
    if grupos == 0:
        return []
    ordenadas = sorted(eligible_units, key=_unit_sort_key)
    en_grupos = ordenadas[: grupos * min_qty]
    return [en_grupos[i * min_qty:(i + 1) * min_qty] for i in range(grupos)]


def _distribute_group_discount(group_units: list, discount: Decimal) -> dict:
    """Reparte `discount` (ya redondeado a peso, FR-006) entre las líneas que
    aportan unidades a **un** grupo completo, repartiendo el importe cobrado
    (FR-008a): `cobrado_L = floor(aporte_L - discount * aporte_L / aporte_total)`;
    los pesos que falten se suman al `cobrado_L` de la línea cuya variante tiene
    el `product_variant_id` más alto (desempate: `line_id` más alto).
    Devuelve `{line_index: descuento_de_L}` — la suma es `discount` exacta."""
    aporte: dict = {}
    info: dict = {}  # line_index -> (pv_id, line_id)
    for line_index, unit_price, pv_id, line_id in group_units:
        aporte[line_index] = aporte.get(line_index, Decimal(0)) + unit_price
        info[line_index] = (pv_id, line_id)

    aporte_total = sum(aporte.values(), Decimal(0))
    objetivo = aporte_total - discount

    cobrado: dict = {}
    for line_index, aporte_l in aporte.items():
        cobrado[line_index] = (
            aporte_l - discount * aporte_l / aporte_total
        ).to_integral_value(rounding=ROUND_FLOOR)

    falta = objetivo - sum(cobrado.values(), Decimal(0))
    if falta != 0:
        top = max(
            aporte,
            key=lambda li: (
                str(info[li][0]),
                str(info[li][1]) if info[li][1] is not None else "",
            ),
        )
        cobrado[top] += falta

    return {li: aporte[li] - cobrado[li] for li in aporte if aporte[li] != cobrado[li]}


def evaluate_variant_sets(
    db: Session, promo_lines: list, now: datetime, tenant: Tenant | None = None
) -> SetDiscountResult:
    """Algoritmo normativo de contracts/motor-y-persistencia.md §3. spec 063
    (revisión 2026-09-01): agrupa por **regla** (antes: por promoción) — la
    vigencia se resuelve una vez por promoción (`active_variant_set_rules`) y
    se aplica a todas sus reglas; el cálculo por bloque no cambia de fórmula.
    `tenant` (spec 127): reenviado a `active_variant_set_rules` para evaluar
    vigencia en la zona horaria real del tenant.

    FR-027 (spec 083, sesión 2026-09-17): para `type == "percent"`, el `%` se
    aplica solo sobre `base_unit_price` (precio de la variante sin toppings),
    nunca sobre `unit_price` (que incluye los adicionales elegidos, spec
    064/065) — el precio de cada topping se cobra íntegro.

    FR-011 (spec 087, A-84): `package_price` sigue el mismo criterio desde
    aquí — descuenta sobre `base_unit_price`, nunca sobre `unit_price`
    completo, para que los adicionales no queden absorbidos por el
    descuento de la promoción. Una línea sin `base_unit_price` (o sin
    toppings) trae `base_unit_price == unit_price`, así que esta rama no
    cambia nada para ese caso."""
    result = SetDiscountResult()
    rules = active_variant_set_rules(db, now, tenant)
    if not rules:
        return result

    by_line: dict = {}
    applied_amount: dict = {}   # rule_id -> [promotion_id, name, Decimal]

    for r in rules:
        conjunto = {v.product_variant_id for v in r.variants}
        if not conjunto:
            continue

        units: list = []
        base_price_by_line: dict = {}
        for idx, line in enumerate(promo_lines):
            pv_id = _line_get(line, "product_variant_id")
            if pv_id not in conjunto:
                continue
            if not _line_get(line, "_variant_active", True):  # FR-011
                continue
            if _line_get(line, "combo_id") is not None:       # defensivo
                continue
            unit_price = Decimal(_line_get(line, "unit_price", 0))
            base_price_by_line[idx] = Decimal(_line_get(line, "base_unit_price", unit_price))
            line_id = _line_get(line, "line_id")
            for _ in range(int(_line_get(line, "quantity", 0))):
                units.append((idx, unit_price, pv_id, line_id))

        rule_amount = Decimal(0)
        for block in _greedy_units(units, r.min_qty):
            if r.type == "package_price":
                base_g = sum((base_price_by_line[u[0]] for u in block), Decimal(0))
                descuento_g = max(Decimal(0), base_g - Decimal(r.value))
                dist_block = [
                    (idx, base_price_by_line[idx], pv_id, line_id)
                    for idx, _price, pv_id, line_id in block
                ]
            else:  # percent (FR-027: solo sobre el precio base de la variante)
                base_g = sum((base_price_by_line[u[0]] for u in block), Decimal(0))
                descuento_g = (
                    base_g * Decimal(r.value) / Decimal(100)
                ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                dist_block = [
                    (idx, base_price_by_line[idx], pv_id, line_id)
                    for idx, _price, pv_id, line_id in block
                ]
            if descuento_g <= 0:
                continue
            for line_index, d in _distribute_group_discount(dist_block, descuento_g).items():
                by_line[line_index] = by_line.get(line_index, Decimal(0)) + d
                rule_amount += d

        if rule_amount > 0:
            applied_amount[r.id] = [r.promotion_id, r.promotion.name, rule_amount]

    result.by_line = by_line
    result.total = sum(by_line.values(), Decimal(0)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    result.applied = [
        AppliedPromotion(
            promotion_id=pid, rule_id=rid, name=nm,
            amount=amt.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        )
        for rid, (pid, nm, amt) in sorted(
            applied_amount.items(), key=lambda kv: (str(kv[1][0]), str(kv[0]))
        )
    ]
    return result


def applied_to_dicts(applied: list) -> list[dict]:
    """`SetDiscountResult.applied` -> lista serializable para `applied_promotions`
    (JSONB): `[{promotion_id, rule_id, name, amount}]` (contract §6)."""
    return [
        {
            "promotion_id": str(a.promotion_id), "rule_id": str(a.rule_id),
            "name": a.name, "amount": str(a.amount),
        }
        for a in applied
    ]


def menu_unit_discount(rules: list, variant_id, unit_price) -> tuple[Decimal, str] | None:
    """Precio unitario vigente para el menú público, con el tipo de regla que lo
    produjo: `(discounted_price, discount_kind)`, o `None` si ninguna regla vigente
    baja el precio de esta variante.

    spec 063: solo `percent` con `min_qty == 1` bajaba el precio; `min_qty > 1`
    depende de cuántas unidades combinadas haya, que en el menú sin carrito no
    existen, y sigue devolviendo `None`.

    spec 066 (A-68, FR-010): se suma `package_price` con `min_qty == 1`, que es un
    **precio unitario especial**. Ahí el precio vigente es `rule.value` **tal cual**,
    incluso si resulta mayor o igual que `unit_price`: no se recorta, no se descarta
    y no se sustituye por el precio normal, porque es el importe que el cobro aplica.
    Devolver `None` en ese caso reintroduciría el defecto que A-68 corrige
    (mostrado ≠ cobrado), solo que en la dirección contraria. Lo que no se muestra
    ahí es la **señal de ahorro**, y esa decisión la toma el frontend comparando dos
    importes que ya le llegaron (FR-015, contracts/menu-info-promocion.md §4.2).

    Devuelve una tupla —y no solo el importe— porque `discount_kind` debe llevar el
    tipo **real** de la regla: el frontend acota la insignia de porcentaje a
    `percent` para no fabricar un `-25%` que un `package_price` nunca enuncia
    (research.md D-13)."""
    for r in rules:
        if r.min_qty != 1 or r.type not in LIVE_TYPES:
            continue
        if variant_id not in {v.product_variant_id for v in r.variants}:
            continue
        if r.type == "package_price":
            return Decimal(r.value), "package_price"
        descuento = Decimal(unit_price) * Decimal(r.value) / Decimal(100)
        if descuento <= 0:
            continue
        precio = (Decimal(unit_price) - descuento).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        return precio, "percent"
    return None


def menu_variant_promotion(
    rules: list,
    variant_id,
    unit_price: Decimal,
    names: Mapping[UUID, str],
) -> dict | None:
    """Bloque `MenuVariantPromotion` de FR-007 para una variante, o `None` si
    ninguna regla vigente la cubre.

    `rules` son las de `active_variant_set_rules`: la vigencia ya está resuelta.
    Se toma la **primera** regla cuyo conjunto contenga la variante, sin criterio de
    desempate y sin inventar uno: `_guard_variant_overlap` (spec 063 FR-014) impide
    que dos reglas vigentes compartan una variante en el mismo instante (FR-012)."""
    for r in rules:
        if variant_id not in {v.product_variant_id for v in r.variants}:
            continue
        # Una regla de tipo retirado no produce bloque. Hoy no llega aquí porque
        # `active_variant_set_rules` ya filtra por `LIVE_TYPES`; la guarda se
        # mantiene por si ese filtro cambia.
        condition_text = variant_set_condition_text(r, names)
        if condition_text is None:
            continue
        value = Decimal(r.value)
        if r.type == "package_price":
            # El precio del paquete es único: el mismo equivalente para todas las
            # variantes del conjunto, aunque mezclen precios normales distintos.
            exacto = value / Decimal(r.min_qty)
            short_condition = f"{r.min_qty} x {_money(value)}"
        else:
            # Sobre el precio normal **de esta** variante.
            exacto = Decimal(unit_price) * (Decimal(100) - value) / Decimal(100)
            pct = f"{value:f}"
            if "." in pct:
                pct = pct.rstrip("0").rstrip(".")
            short_condition = f"{r.min_qty} x -{pct}%"
        # `≈` siempre que el exacto no sea entero en pesos, en los dos tipos
        # (FR-009). Un precio de catálogo llega como `Decimal("8000.00")`, y
        # `Decimal("8000.00") % 1` es `Decimal("0.00")`: compara igual a 0 sin
        # normalizar antes.
        aprox = exacto % 1 != 0
        unit_equivalent = exacto.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        unit_equivalent_text = f"{'≈ ' if aprox else ''}{_money(unit_equivalent)} c/u"
        return {
            "condition_text": condition_text,
            "short_condition": short_condition,
            "unit_equivalent": unit_equivalent,
            "unit_equivalent_approx": aprox,
            "unit_equivalent_text": unit_equivalent_text,
            # El separador es " · " (espacio, U+00B7, espacio), tal como lo fija FR-008.
            "display_text": f"{short_condition} · {unit_equivalent_text}",
            "type": r.type,
            "min_qty": r.min_qty,
            "value": value,
        }
    return None


def _sort_key(name: str) -> str:
    """Clave de ordenación de FR-002 (spec 066): sin tildes y sin distinguir
    mayúsculas, para que el orden no dependa del punto de código ni de la
    configuración regional. La réplica en TypeScript
    (`promotion-condition.util.ts`) usa la misma definición: es lo que hace
    verificable SC-005 entre los dos lenguajes."""
    d = unicodedata.normalize("NFD", name)
    return "".join(c for c in d if not unicodedata.combining(c)).casefold()


def _set_descriptor(names: Iterable[str]) -> tuple[str, bool] | None:
    """Descriptor del conjunto (spec 066, FR-002 y FR-003) y si nombra a más de
    uno — lo segundo decide el `"entre "` de FR-004.

    Recorta espacios, descarta vacíos, **deduplica por el nombre mostrado** (ocho
    variantes llamadas `Pequeño 8oz` son un solo nombre) y ordena por `_sort_key`
    con desempate por el nombre original, que mantiene determinista el caso de dos
    nombres con la misma clave (`Pequeño` y `pequeño`).

    `None` cuando no queda ningún nombre utilizable: ahí el texto vuelve al
    respaldo por conteo (FR-006)."""
    distintos = {n.strip() for n in names if n and n.strip()}
    if not distintos:
        return None
    ordenados = sorted(distintos, key=lambda n: (_sort_key(n), n))
    d = len(ordenados)
    if d == 1:
        return ordenados[0], False
    if d == 2:
        return f"{ordenados[0]} y {ordenados[1]}", True
    if d == 3:
        return f"{ordenados[0]}, {ordenados[1]} y {ordenados[2]}", True
    # Los tres primeros del orden; `d - 3` cuenta **nombres distintos** restantes,
    # no variantes (contracts/texto-condicion.md §2.2).
    return f"{ordenados[0]}, {ordenados[1]}, {ordenados[2]} y {d - 3} más", True


def variant_display_names(db: Session, variant_ids: Iterable[UUID]) -> dict[UUID, str]:
    """`{product_variant_id: nombre utilizable}` en **una sola** consulta.

    El nombre utilizable es el `display_name` de la variante (spec 099, FR-004: su
    nombre propio si lo tiene, si no el de su presentación) y, si ese queda vacío, el
    del producto; una variante sin ninguno de los tres **no aparece en el mapa**,
    porque no aporta nombre al descriptor (spec 066 FR-006, research.md D-3; spec 099
    research.md D10 -- misma precedencia que `display_name`, antes de cualquier texto de
    promoción en el menú público, sin que un nombre propio recién puesto se vea pisado
    por el de la presentación).

    Nunca llamarla dentro de un bucle de variantes ni de reglas: el coste es
    constante por llamada y debe seguir siéndolo (research.md D-12)."""
    ids = set(variant_ids)
    if not ids:
        return {}
    rows = db.execute(
        select(ProductVariant.id, ProductVariant.name, Presentation.name, Product.name)
        .join(Product, Product.id == ProductVariant.product_id)
        .join(Presentation, Presentation.id == ProductVariant.presentation_id)
        .where(ProductVariant.id.in_(ids))
    ).all()
    names: dict[UUID, str] = {}
    for variant_id, own_name, presentation_name, product_name in rows:
        usable = (
            (own_name or "").strip()
            or (presentation_name or "").strip()
            or (product_name or "").strip()
        )
        if usable:
            names[variant_id] = usable
    return names


def variant_set_condition_text(rule: PromotionRule, names: Mapping[UUID, str]) -> str | None:
    """Condición en lenguaje llano, español de Colombia (contract §5). `None`
    para una regla de tipo viejo (histórica, migrada de una promoción
    `finished`).

    spec 066 (A-66): el conjunto se describe por **nombres** de variante en vez
    de por conteo. `names` (`{product_variant_id: nombre utilizable}`, de
    `variant_display_names`) es **posicional y obligatorio** a propósito: un call
    site que lo olvide debe romper en carga, no degradar en silencio al texto por
    conteo y separar las superficies (SC-005, research.md D-1)."""
    # El orden de las guardas importa: una regla histórica no se anuncia, ni
    # siquiera con nombres disponibles (research.md D-3).
    if rule.type not in LIVE_TYPES:
        return None
    n = len(rule.variants)
    value = Decimal(rule.value)
    descriptor = _set_descriptor(
        names[v.product_variant_id]
        for v in rule.variants
        if v.product_variant_id in names
    )
    # `d` nombra el conjunto; `e` solo aparece cuando nombra a más de uno.
    # Sin ningún nombre utilizable se conserva el respaldo por conteo (FR-006).
    d = descriptor[0] if descriptor else f"estas {n} variantes"
    e = "entre " if descriptor and descriptor[1] else ""
    # spec 084 (A-82): cuando el conjunto se nombra con UN solo nombre (una presentación),
    # la cantidad va después: «Llevando 8 onzas x 2 pagas $12.000». Los conjuntos con varios
    # nombres conservan «Llevando 2 entre A, B y C pagas $X» (aquí `x` no diría "cualquiera de").
    single = descriptor is not None and not descriptor[1]
    if rule.type == "package_price":
        if rule.min_qty > 1:
            if descriptor is None:
                return f"Llevando {rule.min_qty} de {d} pagas {_money(value)}"
            if single:
                return f"Llevando {d} x {rule.min_qty} pagas {_money(value)}"
            return f"Llevando {rule.min_qty} {e}{d} pagas {_money(value)}"
        if descriptor is None:
            return f"Cada una de {d} a {_money(value)}"
        return f"Cada {e}{d} a {_money(value)}"
    # `10.00` -> `10`, `12.50` -> `12.5` (`{value:g}` no despoja los ceros de un
    # Decimal; `.rstrip("0")` a secas convertiría `10` en `1`).
    pct = f"{value:f}"
    if "." in pct:
        pct = pct.rstrip("0").rstrip(".")
    if rule.min_qty == 1:
        # `percent` con `min_qty 1` es la única de las cuatro que no lleva `e`
        # (FR-004, contracts/texto-condicion.md §3).
        return f"{pct}% en {d}"
    if descriptor is None:
        return f"{pct}% llevando {rule.min_qty} de {d}"
    if single:
        return f"{pct}% llevando {d} x {rule.min_qty}"
    return f"{pct}% llevando {rule.min_qty} {e}{d}"


# --------------------------- Solape real (bloqueo, FR-014) ---------------------------

def _ranges_overlap(a: Promotion, b: Promotion) -> bool:
    if a.starts_at and b.ends_at and a.starts_at.date() > b.ends_at.date():
        return False
    if b.starts_at and a.ends_at and b.starts_at.date() > a.ends_at.date():
        return False
    return True


def _csv_overlap(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return True  # dimensión abierta = cubre todo su dominio
    return bool({x.strip() for x in a.split(",")} & {x.strip() for x in b.split(",")})


def _times_overlap(a: Promotion, b: Promotion) -> bool:
    if a.start_time is None or b.start_time is None:
        return True
    return _in_time_window(a.start_time, b.start_time, b.end_time) or \
        _in_time_window(b.start_time, a.start_time, a.end_time)


def _guard_no_shared_variants_within_payload(rules_in) -> None:
    """FR-001a (chequeo 1, intra-promoción): ninguna variante puede repetirse
    entre dos reglas del **mismo payload** (`PromotionCreate.rules` /
    `PromotionShapeUpdate.rules`, todavía sin persistir). No hace falta
    comparar vigencia: las reglas de una promoción comparten la misma por
    definición (FR-001), así que compartir variante es *siempre* un
    conflicto simultáneo.

    Se valida **antes** de tocar la base de datos, a propósito: la
    `UNIQUE(promotion_id, product_variant_id)` que `promotion_variants`
    todavía conserva (columna histórica hasta la migración destructiva
    `063d`) no distingue entre reglas de una misma promoción — insertar dos
    reglas con una variante compartida violaría ese `UNIQUE` con un error de
    integridad de base de datos en vez de este 409 legible."""
    for i in range(len(rules_in)):
        set_i = set(rules_in[i].variant_ids)
        for j in range(i + 1, len(rules_in)):
            shared = set_i & set(rules_in[j].variant_ids)
            if shared:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail={
                        "error": (
                            "La misma variante está en más de una regla de "
                            "esta promoción"
                        ),
                        "rule_index_a": i,
                        "rule_index_b": j,
                        "variant_ids": sorted(str(v) for v in shared),
                    },
                )


def _guard_variant_overlap(db: Session, promo: Promotion) -> None:
    """FR-014/FR-014a (inter-promoción, sin cambio de criterio respecto del
    modelo plano; contracts/administracion-promociones.md §2, "Chequeo 2").
    Opera sobre `promo.rules` ya persistidas (post-flush) — cada regla ya
    tiene sus `variants` y su `id`, necesarios para nombrar el conflicto.

    El conjunto de esta promoción (unión de conjuntos de todas sus reglas)
    comparte >= 1 variante con una regla de **otra** promoción en
    `draft`/`active`/`paused` **y** sus rangos de fecha ∧ días ∧ horas se
    intersectan simultáneamente. Dimensión no definida = cubre todo su
    dominio. El chequeo 1 (FR-001a, intra-promoción) corre antes, sobre el
    payload sin persistir — ver `_guard_no_shared_variants_within_payload`.
    """
    rules = list(promo.rules)
    vset = {v.product_variant_id for r in rules for v in r.variants}
    if not vset:
        return
    candidates = db.execute(
        select(Promotion)
        .options(selectinload(Promotion.rules).selectinload(PromotionRule.variants))
        .where(
            Promotion.id != promo.id,
            Promotion.status.in_(("draft", "active", "paused")),
        )
    ).scalars().all()

    conflicts: list[dict] = []
    for c in candidates:
        if not (_ranges_overlap(promo, c)
                and _csv_overlap(promo.days_of_week, c.days_of_week)
                and _times_overlap(promo, c)):
            continue
        for cr in c.rules:
            shared = vset & {v.product_variant_id for v in cr.variants}
            if not shared:
                continue
            conflicts.append({
                "promotion_id": str(c.id),
                "promotion_name": c.name,
                "rule_id": str(cr.id),
                "variant_ids": sorted(str(v) for v in shared),
            })

    if conflicts:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": (
                    "Otra promoción activa ya cubre esta(s) variante(s) en un "
                    "horario que se cruza"
                ),
                "conflicts": conflicts,
            },
        )


def _guard_product_overlap(db: Session, promo: Promotion) -> None:
    """spec 084 (FR-021 a FR-025, A-78): un producto no puede quedar cubierto
    por dos promociones `active` a la vez -- a nivel de PRODUCTO completo
    (cualquiera de sus variantes), sin importar si la variante nueva es la
    misma u otra distinta a la ya comprometida. A diferencia de
    `_guard_variant_overlap` (arriba, spec 063 FR-014, sin cambio): compara
    solo contra `status == "active"` (no `draft`/`paused`) y no evalúa cruce
    de vigencia por fecha/hora -- son dos guardas con criterios distintos,
    esta no reemplaza ni modifica la existente (research.md D3).

    Dentro de la misma promoción (`promo.id` excluido de la comparación)
    seleccionar varias variantes del mismo producto sigue permitido (FR-024).
    No retroactiva (FR-025): solo se evalúa al crear/guardar/activar hacia
    adelante, nunca contra el estado ya persistido de otras promociones."""
    rules = list(promo.rules)
    variant_ids = {v.product_variant_id for r in rules for v in r.variants}
    if not variant_ids:
        return

    own_products = {
        row[0] for row in db.execute(
            select(ProductVariant.product_id).where(ProductVariant.id.in_(variant_ids))
        ).all()
    }
    if not own_products:
        return

    candidates = db.execute(
        select(Promotion)
        .options(selectinload(Promotion.rules).selectinload(PromotionRule.variants))
        .where(Promotion.id != promo.id, Promotion.status == "active")
    ).scalars().all()

    for c in candidates:
        c_variant_ids = {v.product_variant_id for r in c.rules for v in r.variants}
        if not c_variant_ids:
            continue
        c_products = db.execute(
            select(ProductVariant.product_id).where(ProductVariant.id.in_(c_variant_ids))
        ).scalars().all()
        shared = own_products & set(c_products)
        if shared:
            product = db.get(Product, next(iter(shared)))
            nombre = product.name if product is not None else str(next(iter(shared)))
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=(
                    f"El producto «{nombre}» ya hace parte de la promoción activa «{c.name}»"
                ),
            )


def _guard_package_is_discount(db: Session, rule: PromotionRule) -> None:
    """FR-016 / SC-002 (research.md D16) + FR-026 (spec 083, sesión
    2026-09-17): `type == "package_price"` y `value >= min_qty × (menor
    precio regular entre las variantes del conjunto de ESTA regla, activas o
    no)` -> **409**. Cada regla que arma la pantalla de configuración de spec
    083 tiene una sola variante (FR-013), así que ese "peor caso del
    conjunto" coincide exactamente con `precio_regular_de_la_variante ×
    unidades` que exige FR-026 — no es un motor de cálculo nuevo, solo el
    mensaje que ahora expone explícitamente ambos montos."""
    if rule.type != "package_price":
        return
    variant_ids = [v.product_variant_id for v in rule.variants]
    if not variant_ids:
        return
    rows = db.execute(
        select(ProductVariant.id, ProductVariant.price)
        .where(ProductVariant.id.in_(variant_ids))
    ).all()
    if not rows:
        return
    cheapest_id, cheapest_price = min(rows, key=lambda r: Decimal(r[1]))
    regular_price_sum = rule.min_qty * Decimal(cheapest_price)
    if Decimal(rule.value) >= regular_price_sum:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": (
                    f"El precio promocional ({_money(Decimal(rule.value))}) debe ser "
                    f"menor a la suma del precio regular de los productos "
                    f"seleccionados ({_money(regular_price_sum)})."
                ),
                "rule_id": str(rule.id),
                "value": str(rule.value),
                "min_qty": rule.min_qty,
                "regular_price_sum": str(regular_price_sum),
                "variant_id": str(cheapest_id),
            },
        )


# --------------------------- CRUD ---------------------------

def list_query(
    search: str | None = None,
    status_filter: str | None = None,
    closed_by_refactor: bool | None = None,
) -> Select:
    """Select filtrado/ordenado para `GET /promotions`. Orden por `name`
    (ya no `priority`, A-58)."""
    stmt = (
        select(Promotion)
        .options(
            selectinload(Promotion.rules)
            .selectinload(PromotionRule.variants)
            .selectinload(PromotionVariant.product_variant)
        )
        .order_by(Promotion.name)
    )
    if status_filter:
        stmt = stmt.where(Promotion.status == status_filter)
    if search:
        stmt = stmt.where(Promotion.name.ilike(f"%{search.strip()}%"))
    if closed_by_refactor is True:
        stmt = stmt.where(Promotion.closed_by_refactor_at.is_not(None))
    elif closed_by_refactor is False:
        stmt = stmt.where(Promotion.closed_by_refactor_at.is_(None))
    return stmt


def _apply_variant_set(db: Session, rule: PromotionRule, variant_ids) -> None:
    """FR-001a: valida (cada uuid existe y es del tenant — la ausencia de
    repetidos y la no-vacuidad ya las valida `PromotionRuleIn` en Pydantic) y
    puebla el conjunto de **esta regla** (`promotion_variants`)."""
    seen = set(variant_ids)
    existentes = set(db.execute(
        select(ProductVariant.id).where(ProductVariant.id.in_(seen))
    ).scalars().all())
    faltan = seen - existentes
    if faltan:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Variante no encontrada en el catálogo del tenant",
        )

    for vid in variant_ids:
        db.add(PromotionVariant(
            promotion_rule_id=rule.id,
            product_variant_id=vid,
        ))
    db.flush()


def _add_rules(db: Session, promo: Promotion, rules_in: list) -> None:
    """spec 063 (revisión 2026-09-01): crea una `PromotionRule` por cada
    elemento de `rules_in` (creación por lote, FR-001) con su conjunto de
    variantes. Usada por `create` y por `update_shape` (que primero borra las
    reglas existentes)."""
    for rule_in in rules_in:
        rule = PromotionRule(
            promotion_id=promo.id, type=rule_in.type.value,
            value=rule_in.value, min_qty=rule_in.min_qty,
        )
        db.add(rule)
        db.flush()
        _apply_variant_set(db, rule, rule_in.variant_ids)


def create(db: Session, data) -> Promotion:
    _guard_no_shared_variants_within_payload(data.rules)
    promo = Promotion(
        name=data.name, description=data.description,
        status=data.status.value,
        starts_at=data.starts_at, ends_at=data.ends_at,
        days_of_week=data.days_of_week,
        start_time=data.start_time, end_time=data.end_time,
    )
    db.add(promo)
    db.flush()
    _add_rules(db, promo, data.rules)
    db.refresh(promo)
    for rule in promo.rules:
        _guard_package_is_discount(db, rule)
    _guard_variant_overlap(db, promo)
    _guard_product_overlap(db, promo)
    db.flush()
    return promo


def update(db: Session, promo: Promotion, data) -> Promotion:
    """Campos escalares de la **promoción** (FR-018): `type`/`value`/
    `min_qty`/conjunto ya no están en `PromotionUpdate` — viven en cada
    regla y solo se editan por `update_shape`, y solo en `draft`.

    spec 084 (FR-008/FR-011, A-76): una promoción `active` deja de poder editarse por
    esta vía -- antes no tenía ninguna guarda de estado, a diferencia de `update_shape`.
    Deliberadamente NO se reutiliza la condición de `update_shape`
    (`status not in ("draft", "paused")`): esa también bloquearía `finished`, y FR-009
    exige que estos campos sigan editables en `Borrador`, `Pausada` y `Finalizada` --
    solo `active` se bloquea aquí."""
    if promo.status == "active":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Pausa la promoción antes de modificarla",
        )
    provided = data.model_fields_set
    # spec 127 (FR-005): `starts_at` es más estricto que el resto -- solo se
    # puede corregir mientras la promoción sigue en `draft` (nunca llegó a
    # estar vigente), para no alterar retroactivamente la fecha de inicio de
    # una promoción que ya corrió alguna vez.
    if "starts_at" in provided and promo.status != "draft":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "La fecha de inicio solo se puede corregir mientras la promoción sigue en Borrador",
        )
    for field_name in ("name", "description", "starts_at", "ends_at",
                       "days_of_week", "start_time", "end_time"):
        if field_name in provided:
            setattr(promo, field_name, getattr(data, field_name))

    if (promo.start_time is None) != (promo.end_time is None):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "start_time y end_time deben configurarse juntos",
        )
    db.flush()
    return promo


def update_shape(db: Session, promo: Promotion, data) -> Promotion:
    """spec 063 (revisión 2026-09-01, FR-001a/FR-018) + spec 071 (A-69,
    FR-013 a FR-018): reemplaza la lista **completa** de reglas de la
    promoción. Permitido en `draft` y, desde la spec 071, también en
    `paused` — sigue bloqueado en `active` y `finished`."""
    if promo.status not in ("draft", "paused"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Solo una promoción en borrador o pausada puede cambiar sus reglas. "
            "Actívala y páusala para editarla, o duplícala.",
        )
    _guard_no_shared_variants_within_payload(data.rules)
    for rule in list(promo.rules):
        db.delete(rule)
    db.flush()
    _add_rules(db, promo, data.rules)
    db.flush()
    db.refresh(promo)

    for rule in promo.rules:
        _guard_package_is_discount(db, rule)
    _guard_variant_overlap(db, promo)
    _guard_product_overlap(db, promo)
    return promo


def change_status(db: Session, promo: Promotion, new_status: str) -> Promotion:
    if new_status == promo.status:
        return promo
    if new_status not in PROMOTION_TRANSITIONS[promo.status]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Transición no permitida: {promo.status} -> {new_status}",
        )
    if new_status == "active":
        # Una promo creada en `draft` sin conflicto puede chocar al activar.
        if not promo.rules:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Una promoción necesita al menos una regla",
            )
        for rule in promo.rules:
            if not rule.variants:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "Cada regla necesita al menos una variante",
                )
            _guard_package_is_discount(db, rule)
        _guard_variant_overlap(db, promo)
        # spec 084 (FR-023, A-78): cubre la condición de carrera -- dos
        # promociones que pasan `update_shape`/`create` sin conflicto (ninguna
        # de las dos estaba `active` todavía) y luego se activan casi al mismo
        # tiempo. La primera activación gana; esta rechaza la segunda.
        _guard_product_overlap(db, promo)
    promo.status = new_status
    db.flush()
    return promo


def duplicate(db: Session, promo: Promotion, new_name: str) -> Promotion:
    """Copia en `draft` con **todas** las reglas de la promoción (tipo/valor/
    `min_qty`/conjunto de cada una) y la misma vigencia, nombre distinto
    (FR-017). El solape de FR-014 se revalida al **activar** la copia, no al
    duplicar."""
    copy = Promotion(
        name=new_name, description=promo.description,
        status="draft",
        starts_at=promo.starts_at, ends_at=promo.ends_at,
        days_of_week=promo.days_of_week,
        start_time=promo.start_time, end_time=promo.end_time,
    )
    db.add(copy)
    db.flush()
    for rule in promo.rules:
        new_rule = PromotionRule(
            promotion_id=copy.id, type=rule.type,
            value=rule.value, min_qty=rule.min_qty,
        )
        db.add(new_rule)
        db.flush()
        for v in rule.variants:
            db.add(PromotionVariant(
                promotion_rule_id=new_rule.id,
                product_variant_id=v.product_variant_id,
            ))
    db.flush()
    db.refresh(copy)
    return copy


def duplicate_replacing(
    db: Session, promo: Promotion, new_name: str, existing: Promotion
) -> Promotion:
    """spec 084 (A-83): duplica `promo` con `new_name` **reemplazando** a `existing`, la
    promoción que ya usa ese nombre (que puede ser la propia `promo`).

    Todo en la misma transacción del llamador: la copia se crea primero con un nombre
    temporal (así también funciona cuando `existing` es la fuente y se lee antes de
    borrarla), luego se elimina `existing` con todas sus reglas (cascada) y por último la
    copia toma el nombre. Una promoción `active` nunca se reemplaza: hay que pausarla antes.
    """
    if existing.status == "active":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"La promoción «{existing.name}» está activa y no se puede reemplazar. "
            "Pausa la promoción antes de duplicarla con ese nombre.",
        )
    temp_name = f"{new_name[:200]} · copia {uuid4().hex[:8]}"
    copy = duplicate(db, promo, temp_name)
    db.delete(existing)
    db.flush()
    copy.name = new_name
    db.flush()
    return copy


# --------------------------- Serialización ---------------------------

def _serialize_rule(rule: PromotionRule, by_id: dict) -> dict:
    variants = []
    # spec 066: los nombres del descriptor salen del `by_id` que
    # `serialize_promotion` ya cargó — **cero consultas nuevas** aquí
    # (contracts/texto-condicion.md §1). Mismo criterio que
    # `variant_display_names`: el de la variante y, si está vacío, el del producto.
    names: dict[UUID, str] = {}
    for pv in rule.variants:
        v, p = by_id.get(pv.product_variant_id, (None, None))
        if v is None:
            continue
        usable = (v.presentation_name or "").strip()
        if not usable and p:
            usable = (p.name or "").strip()
        if usable:
            names[pv.product_variant_id] = usable
        variants.append({
            "product_variant_id": pv.product_variant_id,
            "description": f"{p.name} - {v.presentation_name}" if p else v.presentation_name,
            "unit_price": Decimal(v.price),
        })
    return {
        "id": rule.id,
        "type": rule.type,
        "value": rule.value,
        "min_qty": rule.min_qty,
        "condition_text": variant_set_condition_text(rule, names),
        "variants": variants,
    }


def serialize_promotion(db: Session, promo: Promotion) -> dict:
    """`PromotionResponse` con `rules` (cada una con su `variants` —
    descripción + precio normal vigente, FR-005— y su `condition_text`)."""
    all_variant_ids = {
        v.product_variant_id for r in promo.rules for v in r.variants
    }
    rows = db.execute(
        select(ProductVariant, Product)
        .join(Product, Product.id == ProductVariant.product_id)
        .where(ProductVariant.id.in_(all_variant_ids))
    ).all() if all_variant_ids else []
    by_id = {v.id: (v, p) for v, p in rows}

    return {
        "id": promo.id,
        "name": promo.name,
        "description": promo.description,
        "status": promo.status,
        "starts_at": promo.starts_at,
        "ends_at": promo.ends_at,
        "days_of_week": promo.days_of_week,
        "start_time": promo.start_time,
        "end_time": promo.end_time,
        "closed_by_refactor_at": promo.closed_by_refactor_at,
        "rules": [_serialize_rule(r, by_id) for r in promo.rules],
    }
