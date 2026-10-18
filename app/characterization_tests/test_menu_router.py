"""CONGELA comportamiento corregido: app/api/v1/menu/router.py:82
(_build_menu) — cierra la anomalía A-08 (specs/000-reconocimiento/
registro-de-anomalias.md), por contraste directo con A-07.

Antes de esta corrección (specs/022-correccion-zona-horaria-menu-carrito),
`_build_menu` evaluaba la vigencia de promociones con
`datetime.now(timezone.utc).replace(tzinfo=None)` — un naive que en realidad
es UTC, pero que `promotions.local_now()` interpreta como si ya estuviera en
hora local del tenant. Con `TENANT_TIMEZONE=America/Bogota` (UTC-5), esto
podía mostrar una promoción vigente/no vigente hasta 5 horas antes o después
de su ventana real.

Esta spec corrige el punto de invocación para pasar un `datetime` aware,
igual que ya hacen los cuatro caminos de cobro real (A-07).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_menu_router -v
"""
from datetime import datetime, time, timezone
from decimal import Decimal
import unittest

from app.characterization_tests import cart_fixtures as fx
from app.api.v1.menu.router import _build_menu, _build_menu_promotions


class TestBuildMenuA08(unittest.TestCase):
    """CONGELA comportamiento corregido — A-08, re-congelado para el modelo por
    conjunto de variantes de la spec 063 (A-58…A-65). La corrección de zona
    horaria (A-08) **se conserva**: `_build_menu` sigue devolviendo
    `list[MenuCategoryResponse]` y `now` viaja aware."""

    def _seed(self, db):
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=Decimal("10000"))
        promo = fx.make_promotion(
            db, status="active", start_time=time(20, 0), end_time=time(21, 0),
        )
        fx.add_rule_to_promotion(
            db, promo, type="percent", value=Decimal("20"), min_qty=1,
            variants=[variant],
        )
        db.commit()
        return variant

    def test_a08_fuera_de_ventana_en_hora_local_no_descuenta(self):
        """FR-001/CA1: a las 20:00 UTC (15:00 Bogotá, fuera de la ventana
        20:00-21:00 local) el menú NO debe mostrar la promoción vigente."""
        db = fx.new_session()
        self._seed(db)

        instant = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)
        with fx.frozen_now(instant, module="app.api.v1.menu.router"):
            menu = _build_menu(db)

        variant_resp = menu[0].products[0].variants[0]
        self.assertIsNone(variant_resp.discounted_price)

    def test_a08_dentro_de_ventana_en_hora_local_si_descuenta(self):
        """CA3 (sin regresión): a la 01:00 UTC del día siguiente (20:00
        Bogotá, dentro de la ventana 20:00-21:00 local) el menú SÍ debe
        mostrar la promoción vigente — mismo resultado que hoy en el caso
        correcto."""
        db = fx.new_session()
        self._seed(db)

        instant = datetime(2026, 1, 16, 1, 0, tzinfo=timezone.utc)
        with fx.frozen_now(instant, module="app.api.v1.menu.router"):
            menu = _build_menu(db)

        variant_resp = menu[0].products[0].variants[0]
        self.assertIsNotNone(variant_resp.discounted_price)
        self.assertLess(variant_resp.discounted_price, variant_resp.price)


class TestBuildMenuVisibleEnQr(unittest.TestCase):
    """spec 128 (FR-002/FR-004): con `only_visible_en_qr=True` (el único caller
    real es `menu_by_signed_qr`, el flujo del comensal), `_build_menu` excluye
    una categoría con `active=True, visible_en_qr=False` (y sus productos), y
    la incluye de nuevo cuando `visible_en_qr=True` — sin que `display_order`
    cambie en ningún momento.

    Corrección post-implementación: la primera versión filtraba por
    `visible_en_qr` siempre, sin distinguir caller -- eso también ocultaba la
    categoría del catálogo de la Terminal de mesas (`GET /menu` ->
    `MenuService` -> `pos-terminal.store.ts`), que según spec.md NUNCA debe
    perder productos por esta bandera (solo por `active`). Ver
    `TestBuildMenuDefaultIncludesHiddenInQr` para esa cobertura."""

    def _seed(self, db, *, visible_en_qr: bool):
        category = fx.make_category(db, display_order=7, visible_en_qr=visible_en_qr)
        product = fx.make_product(db, category=category)
        fx.make_variant(db, product=product, price=Decimal("8000"))
        db.commit()
        return category

    def test_categoria_oculta_en_qr_no_aparece_en_el_menu_qr(self):
        db = fx.new_session()
        self._seed(db, visible_en_qr=False)

        menu = _build_menu(db, only_visible_en_qr=True)

        self.assertEqual(menu, [])

    def test_categoria_visible_en_qr_si_aparece_en_el_menu_qr(self):
        db = fx.new_session()
        category = self._seed(db, visible_en_qr=True)

        menu = _build_menu(db, only_visible_en_qr=True)

        self.assertEqual(len(menu), 1)
        self.assertEqual(menu[0].id, category.id)

    def test_ocultar_y_mostrar_no_cambia_display_order(self):
        db = fx.new_session()
        category = self._seed(db, visible_en_qr=True)
        original_order = category.display_order

        category.visible_en_qr = False
        db.commit()
        self.assertEqual(_build_menu(db, only_visible_en_qr=True), [])
        self.assertEqual(category.display_order, original_order)

        category.visible_en_qr = True
        db.commit()
        menu = _build_menu(db, only_visible_en_qr=True)
        self.assertEqual(menu[0].id, category.id)
        self.assertEqual(category.display_order, original_order)


class TestBuildMenuDefaultIncludesHiddenInQr(unittest.TestCase):
    """spec 128 (notas técnicas): ocultar una categoría del menú QR NO la
    quita del catálogo de productos ni de la creación de pedidos manuales
    (cajero, mesero o admin). La Terminal de mesas llama a `_build_menu` sin
    `only_visible_en_qr` (`GET /menu` -> `public_menu`), así que el valor por
    defecto debe seguir devolviendo una categoría activa aunque esté oculta
    en QR."""

    def test_categoria_oculta_en_qr_sigue_en_el_catalogo_de_la_terminal(self):
        db = fx.new_session()
        category = fx.make_category(db, visible_en_qr=False)
        product = fx.make_product(db, category=category)
        fx.make_variant(db, product=product, price=Decimal("8000"))
        db.commit()

        menu = _build_menu(db)

        self.assertEqual(len(menu), 1)
        self.assertEqual(menu[0].id, category.id)


class TestMenuPromotionsAnnouncementUS5(unittest.TestCase):
    """spec 063 — US4 / FR-022 / SC-007: el menú QR anuncia las promociones por
    **conjunto de variantes** vigentes en ese instante. Reescrita de la clase de
    la spec 040 (A-58…A-65): el anuncio describe el conjunto, ya no una
    presentación. `_build_menu` no cambia de firma."""

    def _seed(self, db, *, days_of_week=None, start_time=None, end_time=None):
        prod = fx.make_product(db)
        variants = [
            fx.make_variant(db, product=prod, name=f"sabor-{i}", price=Decimal("7000"))
            for i in range(8)
        ]
        promo = fx.make_promotion(
            db, name="2 de 8 sabores por 12.000", status="active",
            days_of_week=days_of_week, start_time=start_time, end_time=end_time,
        )
        fx.add_rule_to_promotion(
            db, promo, type="package_price", value=Decimal("12000"), min_qty=2,
            variants=variants,
        )
        db.commit()
        return promo

    def test_vigente_se_anuncia_con_texto_legible(self):
        db = fx.new_session()
        self._seed(db)
        anuncios = _build_menu_promotions(db, datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(len(anuncios), 1)
        regla = anuncios[0].rules[0]
        # spec 066 (A-66): el conjunto se nombra en vez de contarse. Ocho nombres
        # distintos -> los tres primeros en orden alfabético y `y 5 más`.
        self.assertEqual(regla.text,
                         "Llevando 2 entre sabor-0, sabor-1, sabor-2 y 5 más pagas $12.000")
        # `variant_count` se conserva aunque el texto ya no lo mencione: es un
        # campo publicado de la spec 063 (spec 066, contracts/texto-condicion.md §7).
        self.assertEqual(regla.variant_count, 8)
        self.assertEqual(regla.min_qty, 2)

    def test_fuera_de_ventana_de_dia_no_se_anuncia(self):
        db = fx.new_session()
        self._seed(db, days_of_week="0")  # solo lunes
        # miércoles 5 de agosto de 2026
        anuncios = _build_menu_promotions(db, datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(anuncios, [])

    def test_fuera_de_ventana_de_hora_no_se_anuncia(self):
        db = fx.new_session()
        self._seed(db, start_time=time(8, 0), end_time=time(22, 0))
        # 07:00 local (12:00 UTC): fuera de la ventana
        anuncios = _build_menu_promotions(db, datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(anuncios, [])

    def test_vigencia_en_zona_del_tenant_no_arrastra_a08(self):
        """La ventana 20:00-22:00 local: a las 21:00 Bogotá (02:00 UTC del día
        siguiente) se anuncia — se evalúa en hora local, no en UTC."""
        db = fx.new_session()
        self._seed(db, start_time=time(20, 0), end_time=time(22, 0))
        anuncios = _build_menu_promotions(db, datetime(2026, 8, 6, 2, 0, tzinfo=timezone.utc))
        self.assertEqual(len(anuncios), 1)


class TestAnunciosPorPresentacionSinDuplicar(unittest.TestCase):
    """spec 084 (A-81): la regla «por presentación» se guarda como una regla de backend por
    producto; el menú QR debe anunciarla UNA sola vez, no una por producto."""

    NOW = datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc)

    def _seed(self, db):
        promo = fx.make_promotion(db, name="promo lunes a jueves", status="active")
        # Dos productos con las mismas dos presentaciones; una regla de backend por producto
        # y presentación (una variante cada una), como las genera el formulario.
        for producto in ("Ojo de diablo", "Perla negra"):
            prod = fx.make_product(db, name=producto)
            for presentacion, precio in (("8 onzas", "12000"), ("12 onzas", "17000")):
                v = fx.make_variant(db, product=prod, name=presentacion, price=Decimal("20000"))
                fx.add_rule_to_promotion(
                    db, promo, type="package_price", value=Decimal(precio), min_qty=2,
                    variants=[v],
                )
        db.commit()
        return promo

    def test_una_linea_por_presentacion_aunque_haya_una_regla_por_producto(self):
        db = fx.new_session()
        self._seed(db)

        (anuncio,) = _build_menu_promotions(db, self.NOW)

        self.assertEqual(
            sorted(r.text for r in anuncio.rules),
            ["Llevando 12 onzas x 2 pagas $17.000", "Llevando 8 onzas x 2 pagas $12.000"],
        )

    def test_variant_count_suma_las_variantes_de_las_reglas_fusionadas(self):
        db = fx.new_session()
        self._seed(db)

        (anuncio,) = _build_menu_promotions(db, self.NOW)

        self.assertEqual([r.variant_count for r in anuncio.rules], [2, 2])

    def test_reglas_con_texto_distinto_no_se_fusionan(self):
        db = fx.new_session()
        promo = fx.make_promotion(db, name="p", status="active")
        prod = fx.make_product(db)
        for nombre, valor in (("8 onzas", "12000"), ("12 onzas", "17000")):
            v = fx.make_variant(db, product=prod, name=nombre, price=Decimal("20000"))
            fx.add_rule_to_promotion(
                db, promo, type="package_price", value=Decimal(valor), min_qty=2, variants=[v],
            )
        db.commit()

        (anuncio,) = _build_menu_promotions(db, self.NOW)

        self.assertEqual(len(anuncio.rules), 2)


if __name__ == "__main__":
    unittest.main()
