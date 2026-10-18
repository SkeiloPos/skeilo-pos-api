"""Tests de la nueva funcionalidad — spec 063-promociones-por-variante, US2:
el motor `evaluate_variant_sets` (contracts/motor-y-persistencia.md §2).

Decisión de negocio: A-58…A-65 (registro-de-anomalias.md, 2026-08-31,
propietario del repositorio). Precios del catálogo real (spec.md §Assumptions).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_promotions_service -v
"""
from datetime import datetime, time, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from app.characterization_tests import cart_fixtures as fx
from app.api.v1.promotions import service as promotions
from app.core.models import Tenant

NOW = datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc)  # miércoles, 13:00 Bogotá


def _line(variant, qty, *, active=True, combo_id=None, base_unit_price=None):
    unit_price = Decimal(variant.price)
    return {
        "product_variant_id": variant.id,
        "unit_price": unit_price,
        # FR-027 (spec 083): la base del descuento `percent`, sin toppings.
        # Por defecto igual a `unit_price` -- sin diferencia con el
        # comportamiento anterior cuando la línea no lleva adicionales.
        "base_unit_price": unit_price if base_unit_price is None else Decimal(base_unit_price),
        "quantity": qty,
        "line_id": variant.id,   # una fila por variante en estos escenarios
        "combo_id": combo_id,
        "_variant_active": active,
        "description": variant.presentation_name,
    }


class TestEvaluateVariantSets(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.prod = fx.make_product(self.db)

    def _variant(self, price, name=None):
        return fx.make_variant(
            self.db, product=self.prod, price=Decimal(str(price)),
            name=name or f"v-{uuid4()}",
        )

    def _promo(self, type_, value, min_qty, variants, **kw):
        """spec 063 (revisión 2026-09-01): la promoción solo lleva vigencia y
        estado; tipo/valor/cantidad mínima/conjunto viven en su única regla
        (`add_rule_to_promotion`, contracts/migracion.md §2.1). Devuelve la
        `Promotion` (no la regla) porque `AppliedPromotion.promotion_id` es
        el id de la promoción, no el de la regla."""
        p = fx.make_promotion(
            self.db, status="active",
            start_time=kw.get("start_time"), end_time=kw.get("end_time"),
            days_of_week=kw.get("days_of_week"),
        )
        fx.add_rule_to_promotion(
            self.db, p, type=type_, value=Decimal(str(value)), min_qty=min_qty,
            variants=variants,
        )
        self.db.commit()
        return p

    # ---- CA1 / SC-008: 2 variantes distintas del conjunto -> $12.000 ----
    def test_1_paquete_combina_dos_variantes_distintas(self):
        a = self._variant(8000, "Ojo de Diablo Pequeño")
        b = self._variant(8000, "Perla Negra Pequeño")
        self._promo("package_price", 12000, 2, [a, b])

        r = promotions.evaluate_variant_sets(self.db, [_line(a, 1), _line(b, 1)], NOW)
        self.assertEqual(r.total, Decimal("4000.00"))  # 16000 - 12000

    # ---- CA2: 3 unidades -> 1 grupo + 1 suelta a precio normal ----
    def test_2_tres_unidades_una_suelta_determinista(self):
        a, b, c = self._variant(8000, "a"), self._variant(8000, "b"), self._variant(8000, "c")
        self._promo("package_price", 12000, 2, [a, b, c])

        r = promotions.evaluate_variant_sets(
            self.db, [_line(a, 1), _line(b, 1), _line(c, 1)], NOW,
        )
        # 1 grupo (2 unidades más caras) -> descuenta 4000; la 3ª a precio normal.
        self.assertEqual(r.total, Decimal("4000.00"))

    # ---- CA3 / SC-005: otro orden de captura -> total y reparto idénticos ----
    def test_3_mismo_pedido_otro_orden_identico(self):
        a, b, c = self._variant(8000, "a"), self._variant(8000, "b"), self._variant(8000, "c")
        self._promo("package_price", 12000, 2, [a, b, c])

        r1 = promotions.evaluate_variant_sets(
            self.db, [_line(a, 1), _line(b, 1), _line(c, 1)], NOW,
        )
        r2 = promotions.evaluate_variant_sets(
            self.db, [_line(c, 1), _line(a, 1), _line(b, 1)], NOW,
        )
        self.assertEqual(r1.total, r2.total)

    # ---- CA4: percent min_qty 1 sobre conjunto mixto ----
    def test_4_percent_min_qty_1_mixto(self):
        grande = self._variant(15000, "Grande con licor")
        mediano = self._variant(8000, "Mediano sin licor")
        self._promo("percent", 10, 1, [grande, mediano])

        r = promotions.evaluate_variant_sets(
            self.db, [_line(grande, 1), _line(mediano, 1)], NOW,
        )
        # 10% de (15000+8000) = 2300 -> total cobrado 20700
        self.assertEqual(r.total, Decimal("2300.00"))

    # ---- CA5 / SC-005: "15% llevando 3 medianos" -> $4.500, reparto -3300/-1200 ----
    def test_5_percent_min_qty_3_reparto(self):
        con = self._variant(11000, "Mediano con licor")
        sin = self._variant(8000, "Mediano sin licor")
        self._promo("percent", 15, 3, [con, sin])

        lines = [_line(con, 2), _line(sin, 2)]
        r = promotions.evaluate_variant_sets(self.db, lines, NOW)
        # grupo = 3 más caras: 11000 + 11000 + 8000 = 30000; 15% = 4500.
        self.assertEqual(r.total, Decimal("4500.00"))
        # reparto por importe cobrado: línea "con" -3300, línea "sin" -1200.
        by_line = {i: v for i, v in r.by_line.items()}
        self.assertEqual(sorted(by_line.values()), [Decimal("1200"), Decimal("3300")])

    # ---- CA6: no alcanza min_qty -> sin descuento ----
    def test_6_no_alcanza_el_minimo(self):
        a, b = self._variant(8000, "a"), self._variant(8000, "b")
        self._promo("package_price", 12000, 3, [a, b])
        r = promotions.evaluate_variant_sets(self.db, [_line(a, 1), _line(b, 1)], NOW)
        self.assertEqual(r.total, Decimal("0"))
        self.assertEqual(r.applied, [])

    # ---- CA7: día no incluido ----
    def test_7_dia_no_incluido_sin_descuento(self):
        a, b = self._variant(8000, "a"), self._variant(8000, "b")
        self._promo("package_price", 12000, 2, [a, b], days_of_week="0")  # solo lunes
        r = promotions.evaluate_variant_sets(self.db, [_line(a, 1), _line(b, 1)], NOW)
        self.assertEqual(r.total, Decimal("0"))

    # ---- CA8: ventana horaria 14:59 vs 15:01 ----
    def test_8_ventana_horaria(self):
        a, b = self._variant(8000, "a"), self._variant(8000, "b")
        self._promo("package_price", 12000, 2, [a, b],
                    start_time=time(15, 0), end_time=time(17, 0))
        antes = datetime(2026, 8, 5, 19, 59, tzinfo=timezone.utc)   # 14:59 Bogotá
        despues = datetime(2026, 8, 5, 20, 1, tzinfo=timezone.utc)  # 15:01 Bogotá
        self.assertEqual(
            promotions.evaluate_variant_sets(self.db, [_line(a, 1), _line(b, 1)], antes).total,
            Decimal("0"),
        )
        self.assertEqual(
            promotions.evaluate_variant_sets(self.db, [_line(a, 1), _line(b, 1)], despues).total,
            Decimal("4000.00"),
        )

    # ---- CA9 / FR-011: variante desactivada no cuenta ----
    def test_10_variante_desactivada_no_es_unidad_elegible(self):
        a, b = self._variant(8000, "a"), self._variant(8000, "b")
        self._promo("package_price", 12000, 2, [a, b])
        r = promotions.evaluate_variant_sets(
            self.db, [_line(a, 1), _line(b, 1, active=False)], NOW,
        )
        self.assertEqual(r.total, Decimal("0"))  # solo 1 unidad elegible -> 0 grupos

    # ---- SC-005: división no exacta cuadra al peso en cualquier orden ----
    def test_9_division_no_exacta_residuo_al_peso(self):
        a = self._variant(6000, "a")
        b = self._variant(6000, "b")
        c = self._variant(6000, "c")
        self._promo("package_price", 16000, 3, [a, b, c])
        top = max((a, b, c), key=lambda v: str(v.id))

        resultados = []
        for orden in ([a, b, c], [c, b, a], [b, c, a]):
            promo_lines = [_line(v, 1) for v in orden]
            r = promotions.evaluate_variant_sets(self.db, promo_lines, NOW)
            self.assertEqual(r.total, Decimal("2000.00"))              # 18000 - 16000
            self.assertEqual(sum(r.by_line.values()), Decimal("2000"))  # cuadra al peso
            por_variante = {orden[i].id: d for i, d in r.by_line.items()}
            resultados.append(por_variante)
            # la variante de id más alto cobra el residuo ($5.334) -> descuenta
            # menos ($666); las otras dos descuentan $667 (research.md D6).
            self.assertEqual(por_variante[top.id], Decimal("666"))
            self.assertEqual(
                sorted(por_variante.values()),
                [Decimal("666"), Decimal("667"), Decimal("667")],
            )
        # el reparto por variante NO depende del orden de las líneas.
        self.assertEqual(resultados[0], resultados[1])
        self.assertEqual(resultados[1], resultados[2])

    # ---- Assumptions: conjunto mixto con precios distintos -> $18.000 ----
    def test_11_conjunto_mixto_precios_distintos(self):
        p8a = self._variant(8000, "Ojo de Diablo")
        p6 = self._variant(6000, "Manzana Verde sin licor")
        p8b = self._variant(8000, "Perla Negra")
        self._promo("package_price", 12000, 2, [p8a, p6, p8b])

        r = promotions.evaluate_variant_sets(
            self.db, [_line(p8a, 1), _line(p6, 1), _line(p8b, 1)], NOW,
        )
        # el grupo toma las 2 de 8000 -> 16000 - 12000 = 4000; la de 6000 suelta.
        self.assertEqual(r.total, Decimal("4000.00"))

    # ---- FR-009: un grupo nunca encarece (descuento topado en 0) ----
    def test_12_package_price_no_encarece(self):
        a, b = self._variant(5000, "a"), self._variant(5000, "b")
        self._promo("package_price", 12000, 2, [a, b])  # 12000 > 10000 normal
        r = promotions.evaluate_variant_sets(self.db, [_line(a, 1), _line(b, 1)], NOW)
        self.assertEqual(r.total, Decimal("0"))

    # ---- applied lista toda promoción con amount > 0, ordenada por promotion_id ----
    def test_13_applied_promotions_agregado_por_promocion(self):
        a = self._variant(8000, "a")
        b = self._variant(6000, "b")
        p1 = self._promo("percent", 10, 1, [a])
        p2 = self._promo("percent", 20, 1, [b])
        r = promotions.evaluate_variant_sets(self.db, [_line(a, 1), _line(b, 1)], NOW)
        montos = {ap.promotion_id: ap.amount for ap in r.applied}
        self.assertEqual(montos[p1.id], Decimal("800.00"))
        self.assertEqual(montos[p2.id], Decimal("1200.00"))
        self.assertEqual([ap.promotion_id for ap in r.applied],
                         sorted([p1.id, p2.id], key=str))

    # ---- T027 (spec 063, revisión 2026-09-01): dos reglas de la MISMA
    # promoción, ambas con descuento en el mismo cobro -> dos entradas en
    # `applied` con igual `promotion_id` y distinto `rule_id` ----
    def test_14_dos_reglas_de_la_misma_promocion_descuentan_en_el_mismo_cobro(self):
        pequenos_a = self._variant(8000, "pequeño-a")
        pequenos_b = self._variant(8000, "pequeño-b")
        medianos_a = self._variant(11000, "mediano-a")
        medianos_b = self._variant(11000, "mediano-b")
        promo = fx.make_promotion(self.db, name="2X entre semana", status="active")
        regla_pequenos = fx.add_rule_to_promotion(
            self.db, promo, type="package_price", value=Decimal("12000"), min_qty=2,
            variants=[pequenos_a, pequenos_b],
        )
        regla_medianos = fx.add_rule_to_promotion(
            self.db, promo, type="package_price", value=Decimal("17000"), min_qty=2,
            variants=[medianos_a, medianos_b],
        )
        self.db.commit()

        r = promotions.evaluate_variant_sets(self.db, [
            _line(pequenos_a, 1), _line(pequenos_b, 1),
            _line(medianos_a, 1), _line(medianos_b, 1),
        ], NOW)

        # $16.000 -> $12.000 (regla Pequeños) + $22.000 -> $17.000 (regla Medianos)
        self.assertEqual(r.total, Decimal("9000.00"))
        self.assertEqual(len(r.applied), 2)
        self.assertTrue(all(ap.promotion_id == promo.id for ap in r.applied))
        rule_ids = {ap.rule_id for ap in r.applied}
        self.assertEqual(rule_ids, {regla_pequenos.id, regla_medianos.id})
        montos = {ap.rule_id: ap.amount for ap in r.applied}
        self.assertEqual(montos[regla_pequenos.id], Decimal("4000.00"))
        self.assertEqual(montos[regla_medianos.id], Decimal("5000.00"))

    # ---- FR-027 (spec 083, sesión 2026-09-17): el % de una regla `percent`
    # se aplica solo sobre el precio base de la variante, nunca sobre el
    # precio de los toppings/adicionales elegidos (spec 064/065) ----
    def test_15_percent_excluye_toppings_de_la_base_min_qty_1(self):
        # $10.000 de línea = $8.000 de variante + $2.000 de toppings.
        v = self._variant(8000, "malteada con toppings")
        self._promo("percent", 10, 1, [v])

        linea = {
            "product_variant_id": v.id, "unit_price": Decimal("10000"),
            "base_unit_price": Decimal("8000"), "quantity": 1,
            "line_id": v.id, "combo_id": None, "_variant_active": True,
            "description": v.presentation_name,
        }
        r = promotions.evaluate_variant_sets(self.db, [linea], NOW)
        # 10% de $8.000 (la base) = $800 -- no 10% de $10.000 ($1.000).
        self.assertEqual(r.total, Decimal("800.00"))

    def test_16_percent_min_qty_2_reparte_por_precio_base_no_por_precio_total(self):
        # Dos líneas de la misma variante, cantidades separadas para variar
        # cuánto topping lleva cada unidad: ambas con la misma base ($1.000),
        # una sin toppings y otra con $500 de toppings.
        v = self._variant(1000, "cono")
        self._promo("percent", 10, 2, [v])

        sin_topping = {
            "product_variant_id": v.id, "unit_price": Decimal("1000"),
            "base_unit_price": Decimal("1000"), "quantity": 1,
            "line_id": uuid4(), "combo_id": None, "_variant_active": True,
            "description": "cono sin topping",
        }
        con_topping = {
            "product_variant_id": v.id, "unit_price": Decimal("1500"),
            "base_unit_price": Decimal("1000"), "quantity": 1,
            "line_id": uuid4(), "combo_id": None, "_variant_active": True,
            "description": "cono con topping",
        }
        r = promotions.evaluate_variant_sets(self.db, [sin_topping, con_topping], NOW)
        # Base del grupo: 1000 + 1000 = 2000; 10% = 200, repartido por partes
        # iguales (misma base) -- $100 cada línea, no proporcional a $1000/$1500.
        self.assertEqual(r.total, Decimal("200.00"))
        by_line = dict(r.by_line)
        self.assertEqual(sorted(by_line.values()), [Decimal("100"), Decimal("100")])

    def test_17_package_price_excluye_toppings_de_la_base_min_qty_1(self):
        """spec 087, FR-011: el fix corrige `package_price` para que también
        descuente sobre `base_unit_price` (sin adicionales), igual que `percent`
        desde FR-027 (spec 083) -- reemplaza el comportamiento congelado en la
        versión anterior de este test, que documentaba el bug ahora corregido
        (A-84, registro-de-anomalias.md)."""
        v = self._variant(6000, "Granizado de Mora")
        self._promo("package_price", 5000, 1, [v])
        linea = _line(v, 1, base_unit_price=Decimal("6000"))
        linea["unit_price"] = Decimal("9500")  # 6000 base + 2000 leche cond. + 1500 choco-chips
        r = promotions.evaluate_variant_sets(self.db, [linea], NOW)
        # Descuento = base(6000) - promo(5000) = 1000 -- nunca sobre 9500 (total final 8500).
        self.assertEqual(r.total, Decimal("1000.00"))


class TestEvaluateVariantSetsTenantTimezone(unittest.TestCase):
    """spec 127 (FR-002): la vigencia se evalúa en la zona horaria REAL del
    tenant propietario de la promoción, no en el respaldo global de
    instancia (`settings.TENANT_TIMEZONE` = America/Bogota, UTC-5) — antes
    de esta spec, ningún llamador pasaba nunca un `tenant`, así que todos
    coincidían por casualidad con ese respaldo global."""

    def setUp(self):
        self.db = fx.new_session()
        self.prod = fx.make_product(self.db)
        self.variant = fx.make_variant(
            self.db, product=self.prod, price=Decimal("8000"), name=f"v-{uuid4()}",
        )
        # Ventana angosta (22:00-23:00, todos los días) elegida para que un
        # único instante UTC caiga DENTRO de la ventana en una zona horaria y
        # FUERA en otra.
        self.promo = fx.make_promotion(
            self.db, status="active", start_time=time(22, 0), end_time=time(23, 0),
        )
        fx.add_rule_to_promotion(
            self.db, self.promo, type="package_price", value=Decimal("5000"),
            min_qty=1, variants=[self.variant],
        )
        self.db.commit()

    def _lines(self):
        return [_line(self.variant, 1)]

    def test_mismo_instante_vigente_en_bogota_no_vigente_en_mexico(self):
        # 2026-08-06T03:30:00Z: 22:30 en Bogotá (UTC-5, DENTRO de 22:00-23:00)
        # pero 21:30 en Ciudad de México (UTC-6, FUERA de 22:00-23:00).
        instant = datetime(2026, 8, 6, 3, 30, tzinfo=timezone.utc)
        bogota = Tenant(timezone="America/Bogota")
        mexico = Tenant(timezone="America/Mexico_City")

        r_bogota = promotions.evaluate_variant_sets(self.db, self._lines(), instant, bogota)
        r_mexico = promotions.evaluate_variant_sets(self.db, self._lines(), instant, mexico)

        self.assertEqual(r_bogota.total, Decimal("3000.00"))  # 8000 - 5000, vigente
        self.assertEqual(r_mexico.total, Decimal("0"))        # fuera de ventana, sin descuento

    def test_sin_tenant_cae_al_respaldo_global_equivalente_a_bogota(self):
        instant = datetime(2026, 8, 6, 3, 30, tzinfo=timezone.utc)

        r_sin_tenant = promotions.evaluate_variant_sets(self.db, self._lines(), instant)
        r_bogota = promotions.evaluate_variant_sets(
            self.db, self._lines(), instant, Tenant(timezone="America/Bogota"),
        )

        self.assertEqual(r_sin_tenant.total, r_bogota.total)
        self.assertEqual(r_sin_tenant.total, Decimal("3000.00"))

    def test_active_variant_set_rules_tambien_distingue_por_tenant(self):
        """Mismo caso, a nivel de `active_variant_set_rules` (lo que consume
        el menú público) -- no solo `evaluate_variant_sets` (carrito/cobro)."""
        instant = datetime(2026, 8, 6, 3, 30, tzinfo=timezone.utc)
        bogota = Tenant(timezone="America/Bogota")
        mexico = Tenant(timezone="America/Mexico_City")

        reglas_bogota = promotions.active_variant_set_rules(self.db, instant, bogota)
        reglas_mexico = promotions.active_variant_set_rules(self.db, instant, mexico)

        self.assertEqual(len(reglas_bogota), 1)
        self.assertEqual(len(reglas_mexico), 0)


if __name__ == "__main__":
    unittest.main()
