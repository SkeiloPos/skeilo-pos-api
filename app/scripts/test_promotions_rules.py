"""Reglas del motor de promociones que cuestan dinero si se rompen.

    python -m app.scripts.test_promotions_rules

**No toca la base de datos.** A diferencia del resto de `app/scripts/test_*`,
que trabajan sobre un tenant real, esto ejercita **funciones puras** del motor
por conjunto de variantes de la spec 063 (`_valid_now`, `_in_time_window`,
`_greedy_units`, `_distribute_group_discount`, `variant_set_condition_text`) con
objetos sin sesión. Por eso puede correr en CI, antes de cada deploy.

spec 063-promociones-por-variante (decisión de negocio A-58…A-65,
registro-de-anomalias.md), reescrito para la partición `Promoción`/`Regla`
(revisión 2026-09-01, contracts/migracion.md §2.1): `_valid_now` sigue
operando sobre una `Promotion` (vigencia + estado, sin cambio de cuerpo);
`_greedy_units`/`_distribute_group_discount`/`variant_set_condition_text`
operan sobre el conjunto y la combinación (tipo/valor/cantidad mínima) de
una **regla**, no de la promoción directa. Se fueron `priority`,
`_matching_target` y `_line_discount` de `qty_price`/`fixed`
(contracts/migracion.md §2.5). Cubre:

  1. Vigencia en **hora local**, no en UTC (A-08) + cruce de medianoche +
     atribución de día al cruzar medianoche (**A-57 conservado**).
  2. **Consumo codicioso descendente** de precio: el grupo toma las unidades más
     caras (FR-008); grupos completos + remanente a precio normal (FR-007).
  3. `package_price`: descuento del grupo = `Σ normal − value`, topado en 0
     (FR-006, FR-009). `percent`: `round(value% × Σ normal)` a peso (FR-006).
  4. `_distribute_group_discount` con **división no exacta**: residuo a la
     variante de id más alto; `Σ descuentos por línea == descuento del grupo`,
     al peso, en cualquier orden (FR-008a, SC-005).
  5. Un grupo **nunca encarece** (FR-009).
  6. El `type` **de entrada** admite exactamente `{percent, package_price}`.
"""
import os
from datetime import datetime, time, timezone
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from uuid import UUID

# Sin .env real: `Settings` exige credenciales que este script no usa.
for _k, _v in {
    "DATABASE_URL": "postgresql+psycopg://x:x@localhost/x",
    "REDIS_URL": "redis://localhost:6379/0",
    "JWT_SECRET": "test",
    "EMAIL_API_URL": "https://example.invalid",
    "MAIL_FROM_NAME": "t", "MAIL_FROM": "t@example.invalid",
    "R2_ACCOUNT_ID": "x", "R2_ACCESS_KEY_ID": "x", "R2_SECRET_ACCESS_KEY": "x",
    "R2_BUCKET_NAME": "x", "R2_ENDPOINT_URL": "https://example.invalid",
    "R2_PUBLIC_BASE_URL": "https://example.invalid",
    "ASSETS_BASE_URL": "https://assets.example.invalid",
    "CONSENT_IP_HASH_SALT": "test",
    "SUPER_ADMIN_NAME": "t", "SUPER_ADMIN_EMAIL": "t@example.invalid",
    "SUPER_ADMIN_PASSWORD": "t",
}.items():
    os.environ.setdefault(_k, _v)

from app.api.v1.promotions.service import (  # noqa: E402
    _distribute_group_discount, _greedy_units, _in_time_window, _set_descriptor,
    _valid_now, variant_set_condition_text,
)
from app.api.v1.promotions.schemas import PromotionType  # noqa: E402
from app.models.promotion import PROMOTION_TYPES, Promotion, PromotionRule  # noqa: E402

fallos: list[str] = []


def check(nombre: str, ok: bool) -> None:
    print(f"  {'OK  ' if ok else 'FALLA'} {nombre}")
    if not ok:
        fallos.append(nombre)


def promo(**kw) -> Promotion:
    """spec 063 (revisión 2026-09-01): `_valid_now` solo lee vigencia/estado
    de la promoción — ya no necesita `type`/`value`/`min_qty` (viven en cada
    `PromotionRule`, sin relación con la vigencia)."""
    base = dict(name=kw.pop("name", "p"), status="active")
    base.update(kw)
    p = Promotion(**base)
    p.created_at = kw.get("created_at", datetime(2026, 1, 1))
    return p


def unit(line_index, price, pv_id, line_id=None):
    return (line_index, Decimal(str(price)), pv_id, line_id or pv_id)


def descuento_grupo(tipo, value, block):
    """Descuento de un grupo completo (research.md D5.d)."""
    normal_g = sum((u[1] for u in block), Decimal(0))
    if tipo == "package_price":
        return max(Decimal(0), normal_g - Decimal(str(value)))
    return (normal_g * Decimal(str(value)) / Decimal(100)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )


# --- 1. Hora local + cruce de medianoche + A-57 ---------------------------
print("\n1. Vigencia en hora local del tenant (A-08) y cruce de medianoche (A-57)")

utc_miercoles = datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc)  # martes 20:00 Bogotá
check("martes 20:00 local sigue siendo martes",
      _valid_now(promo(days_of_week="1"), utc_miercoles))
check("no se adelanta al miércoles",
      not _valid_now(promo(days_of_week="2"), utc_miercoles))

hh = promo(start_time=time(15, 0), end_time=time(17, 0))
check("happy hour 15-17 aplica a las 16:00 locales",
      _valid_now(hh, datetime(2026, 8, 4, 21, 0, tzinfo=timezone.utc)))
check("happy hour 15-17 no aplica a las 10:00 locales",
      not _valid_now(hh, datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc)))

check("23:00 cae dentro de 22:00-02:00", _in_time_window(time(23, 0), time(22, 0), time(2, 0)))
check("01:00 cae dentro de 22:00-02:00", _in_time_window(time(1, 0), time(22, 0), time(2, 0)))
check("15:00 queda fuera de 22:00-02:00", not _in_time_window(time(15, 0), time(22, 0), time(2, 0)))
check("sin ventana, siempre dentro", _in_time_window(time(3, 0), None, None))

# A-57: con la ventana cruzando la medianoche, las horas posteriores a las 00:00
# pertenecen al DÍA DE INICIO para evaluar `days_of_week`.
lunes_noche = promo(days_of_week="0", start_time=time(22, 0), end_time=time(2, 0))
check("lunes 23:00 local: vigente",
      _valid_now(lunes_noche, datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc)))
check("martes 01:00 local: sigue siendo el lunes de inicio, vigente",
      _valid_now(lunes_noche, datetime(2026, 8, 4, 6, 0, tzinfo=timezone.utc)))
check("martes 03:00 local: fuera de ventana, no vigente",
      not _valid_now(lunes_noche, datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc)))
check("miércoles 01:00 local: el día de inicio sería el martes, no vigente",
      not _valid_now(lunes_noche, datetime(2026, 8, 5, 6, 0, tzinfo=timezone.utc)))


# --- 2. Consumo codicioso descendente + grupos completos -----------------
print("\n2. Consumo codicioso: el grupo toma las unidades más caras (FR-008)")

V1, V2, V3 = UUID(int=1), UUID(int=2), UUID(int=3)
unidades = [unit(0, 11000, V1), unit(0, 11000, V1), unit(1, 8000, V2), unit(1, 8000, V2)]
grupos = _greedy_units(unidades, 3)
check("4 unidades, min_qty 3 -> 1 grupo", len(grupos) == 1)
precios_grupo = sorted((u[1] for u in grupos[0]), reverse=True)
check("el grupo toma 11.000 + 11.000 + 8.000 (las 3 más caras)",
      precios_grupo == [Decimal("11000"), Decimal("11000"), Decimal("8000")])

check("2 unidades, min_qty 3 -> 0 grupos (remanente a precio normal, FR-007)",
      _greedy_units([unit(0, 8000, V1), unit(0, 8000, V1)], 3) == [])

seis = [unit(i, 8000, UUID(int=i + 1)) for i in range(6)]
check("6 unidades, min_qty 2 -> 3 grupos de 2", len(_greedy_units(seis, 2)) == 3)


# --- 3. Descuento por grupo: package_price y percent ---------------------
print("\n3. Descuento del grupo (FR-006, FR-009)")

pkg_block = [unit(0, 8000, V1), unit(1, 8000, V2)]
check("package_price: Σ16.000 - 12.000 = 4.000",
      descuento_grupo("package_price", 12000, pkg_block) == Decimal("4000"))
check("package_price que iguala o supera el normal -> 0 (nunca encarece)",
      descuento_grupo("package_price", 20000, pkg_block) == Decimal("0"))

pct_block = [unit(0, 11000, V1), unit(0, 11000, V1), unit(1, 8000, V2)]
check("percent 15% de Σ30.000 = 4.500 (redondeado a peso)",
      descuento_grupo("percent", 15, pct_block) == Decimal("4500"))
check("percent 10% de 23.000 = 2.300",
      descuento_grupo("percent", 10, [unit(0, 15000, V1), unit(1, 8000, V2)]) == Decimal("2300"))


# --- 4. Reparto por importe cobrado + división no exacta (FR-008a, SC-005) -
print("\n4. _distribute_group_discount: cuadra al peso, residuo a la variante de id más alto")

# "3 Pequeños sin licor por 16.000": 3 x 6.000, descuento del grupo 2.000.
alta = UUID(int=99)
block = [unit(0, 6000, UUID(int=1)), unit(1, 6000, UUID(int=2)), unit(2, 6000, alta)]
rep = _distribute_group_discount(block, Decimal("2000"))
check("Σ descuentos por línea == 2.000 (al peso)", sum(rep.values()) == Decimal("2000"))
check("la variante de id más alto descuenta menos (666), las otras 667",
      sorted(rep.values()) == [Decimal("666"), Decimal("667"), Decimal("667")]
      and rep[2] == Decimal("666"))

# Mismo grupo, unidades pasadas en otro orden -> el reparto no cambia: la
# variante de id más alto (`alta`, aquí en line_index 2) sigue descontando 666.
block_rev = [unit(2, 6000, alta), unit(1, 6000, UUID(int=2)), unit(0, 6000, UUID(int=1))]
rep_rev = _distribute_group_discount(block_rev, Decimal("2000"))
check("otro orden de las unidades -> mismo total", sum(rep_rev.values()) == Decimal("2000"))
check("otro orden -> la variante de id más alto sigue en 666", rep_rev[2] == Decimal("666"))

# "15% llevando 3 medianos": grupo 2x11.000 + 1x8.000, descuento 4.500.
grp = [unit(0, 11000, V1), unit(0, 11000, V1), unit(1, 8000, V2)]
rep2 = _distribute_group_discount(grp, Decimal("4500"))
check("reparto -3.300 / -1.200", sorted(rep2.values()) == [Decimal("1200"), Decimal("3300")])
check("Σ == 4.500", sum(rep2.values()) == Decimal("4500"))

# 2X8.000 -> 12.000: descuento 4.000, dos líneas de una unidad -> -2.000 c/u.
rep3 = _distribute_group_discount([unit(0, 8000, V1), unit(1, 8000, V2)], Decimal("4000"))
check("2X: -2.000 por línea", sorted(rep3.values()) == [Decimal("2000"), Decimal("2000")])


# --- 5. Descriptor del conjunto (spec 066, FR-002/FR-003) ----------------
print("\n5. _set_descriptor")

def _descriptor_casos() -> None:
    """spec 066 (A-66): el conjunto se nombra en vez de contarse. Deduplica por el
    nombre mostrado, ordena sin tildes ni mayúsculas y resume a tres.

    `variant_display_names` (T005) no se ejercita aquí: necesita una `Session` y
    este script corre sin base de datos a propósito. La cubren los
    characterization tests sobre SQLite."""
    check("0 nombres -> None (respaldo por conteo, FR-006)",
          _set_descriptor([]) is None)
    check("solo vacíos y espacios -> None",
          _set_descriptor(["", "   "]) is None)
    check("1 nombre -> sin 'entre'",
          _set_descriptor(["Pequeño 8oz"]) == ("Pequeño 8oz", False))
    check("8 nombres iguales -> uno solo, sin 'entre' (FR-003)",
          _set_descriptor(["Pequeño 8oz"] * 8) == ("Pequeño 8oz", False))
    check("se recorta y se deduplica por el nombre mostrado",
          _set_descriptor(["Pequeño 8oz", "  Pequeño 8oz  "]) == ("Pequeño 8oz", False))
    check("los vacíos no cuentan, el nombre bueno sobrevive",
          _set_descriptor(["  ", "Pequeño 8oz", ""]) == ("Pequeño 8oz", False))
    check("2 nombres -> 'A y B'",
          _set_descriptor(["Mediano 12oz", "Grande 16oz"])
          == ("Grande 16oz y Mediano 12oz", True))
    check("3 nombres -> orden alfabético, no el de selección",
          _set_descriptor(["Pequeño 8oz", "Mediano 12oz", "Grande 16oz"])
          == ("Grande 16oz, Mediano 12oz y Pequeño 8oz", True))
    check("5 nombres -> tres primeros y 'y 2 más' (nombres, no variantes)",
          _set_descriptor(["Durazno", "Ácai", "Cereza", "Almendra", "Banano"])
          == ("Ácai, Almendra, Banano y 2 más", True))
    check("la tilde no altera el orden: Ácai antes que Almendra (D-2)",
          _set_descriptor(["Almendra", "Ácai"]) == ("Ácai y Almendra", True))
    check("mayúsculas y minúsculas se ordenan juntas",
          _set_descriptor(["banano", "Ácai"]) == ("Ácai y banano", True))

_descriptor_casos()


# --- 6. Textos de condición (español de Colombia) ------------------------
print("\n6. variant_set_condition_text")

def _regla_texto(tipo, value, min_qty, nombres):
    # spec 063 (revisión 2026-09-01): `variant_set_condition_text` recibe una
    # REGLA, no una promoción.
    # spec 066: y además el mapa `{product_variant_id: nombre utilizable}`,
    # obligatorio. `nombres` lleva un elemento por variante del conjunto; un
    # `None` es una variante sin nombre utilizable, que no entra al mapa (FR-006).
    ids = [UUID(int=i) for i in range(len(nombres))]
    fake = SimpleNamespace(
        type=tipo, value=Decimal(str(value)), min_qty=min_qty,
        variants=[SimpleNamespace(product_variant_id=i) for i in ids],
    )
    return variant_set_condition_text(fake, {i: n for i, n in zip(ids, nombres) if n})

# Tabla normativa de contracts/texto-condicion.md §5, los 10 casos. La misma que
# ejercita `promotion-condition.util.spec.ts`: si un caso da distinto en los dos
# lenguajes, las superficies se separaron (SC-005).
check("1. paquete, 8 variantes con el mismo nombre -> un solo nombre, sin 'entre'",
      _regla_texto("package_price", 12000, 2, ["Pequeño 8oz"] * 8)
      == "Llevando Pequeño 8oz x 2 pagas $12.000")  # spec 084, A-82
check("2. paquete, conjunto de UNA variante -> nunca 'de estas 1 variantes'",
      _regla_texto("package_price", 12000, 2, ["Pequeño 8oz"])
      == "Llevando Pequeño 8oz x 2 pagas $12.000")  # spec 084, A-82
check("3. paquete, 3 nombres -> orden alfabético (Grande primero), con 'entre'",
      _regla_texto("package_price", 15000, 2, ["Pequeño 8oz", "Mediano 12oz", "Grande 16oz"])
      == "Llevando 2 entre Grande 16oz, Mediano 12oz y Pequeño 8oz pagas $15.000")
check("4. paquete, 5 nombres -> tres primeros y 'y 2 más'",
      _regla_texto("package_price", 15000, 2,
                   ["Durazno", "Ácai", "Cereza", "Almendra", "Banano"])
      == "Llevando 2 entre Ácai, Almendra, Banano y 2 más pagas $15.000")
check("5. percent min_qty 1 -> sin 'entre' aunque el conjunto sea grande",
      _regla_texto("percent", 10, 1, ["Pequeño 8oz"] * 8) == "10% en Pequeño 8oz")
check("6. percent min_qty>1, 2 nombres -> 'entre A y B'",
      _regla_texto("percent", 15, 3, ["Mediano 12oz", "Grande 16oz"])
      == "15% llevando 3 entre Grande 16oz y Mediano 12oz")
check("7. paquete min_qty 1 -> 'Cada {nombre} a {valor}'",
      _regla_texto("package_price", 6000, 1, ["Pequeño 8oz"])
      == "Cada Pequeño 8oz a $6.000")
check("8. ningún nombre utilizable -> respaldo por conteo intacto (FR-006)",
      _regla_texto("percent", 10, 1, [None, None, None]) == "10% en estas 3 variantes")
check("9. regla histórica de tipo retirado -> None, antes de mirar nombres",
      _regla_texto("combo", 0, 1, ["Pequeño 8oz"]) is None)
check("10. porcentaje con decimal -> punto, no coma (FR-005 sin cambio)",
      _regla_texto("percent", "12.5", 1, ["Pequeño 8oz"]) == "12.5% en Pequeño 8oz")


# --- 7. El enum de ENTRADA admite exactamente {percent, package_price} ---
print("\n7. Tipos vivos")
check("PromotionType de entrada = {percent, package_price}",
      {t.value for t in PromotionType} == {"percent", "package_price"})
check("PROMOTION_TYPES conserva los viejos + package_price (lee las finished)",
      set(PROMOTION_TYPES) == {"percent", "fixed", "combo", "qty_price",
                               "qty_price_presentation", "package_price"})
# spec 063 (revisión 2026-09-01): `type` vive en `PromotionRule`, no en
# `Promotion` (retirada por la migración destructiva `063d`).
_type_len = PromotionRule.__table__.c.type.type.length
check(f"varchar({_type_len}) admite el más largo ({max(PROMOTION_TYPES, key=len)!r})",
      _type_len >= max(len(t) for t in PROMOTION_TYPES))


print("\n" + "=" * 60)
if fallos:
    print(f"FALLARON {len(fallos)} comprobación(es):")
    for f in fallos:
        print(f"  - {f}")
    raise SystemExit(1)
print("Todas las comprobaciones pasaron.")
