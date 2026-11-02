"""Spec 148 (disponibilidad en tiempo real) — motor único de "pedibilidad"
(`app/catalog_engine/availability.py`).

Este módulo arranca con una prueba de EQUIVALENCIA (Principio V): congela, con
valores literales, lo que hoy calcula `_option_availability`/`_build_menu`
(`app/api/v1/menu/router.py`) para opciones, presentaciones y productos, usando
un catálogo con insumos en stock suficiente/justo/insuficiente, una opción
ligada a un insumo inactivo, un grupo con `quantity_per_option` por tamaño, y
un grupo obligatorio `min_select>0` modo `conteo` sin ninguna opción
disponible. Esta prueba debe pasar contra el código SIN modificar (ninguna
receta en el catálogo de esta primera clase: ese caso es comportamiento nuevo,
cubierto más abajo) y sigue pasando después de mover el cálculo al motor
(T006/T009) — la receta es el único cambio observable intencional (anomalía
A-154, registro-de-anomalias.md).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_availability_engine -v
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from sqlalchemy import event

from app.characterization_tests import cart_fixtures as fx
from app.api.v1.menu.router import _build_menu
from app.catalog_engine.availability import compute_availability, diff_availability


class EquivalenceGoldenMenuTests(unittest.TestCase):
    """CONGELA comportamiento actual: `available` de opciones/presentaciones/
    productos tal como lo calcula `_build_menu` hoy, sin receta."""

    def _seed(self, db):
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=Decimal("10000"))
        group = fx.make_option_group(db, min_select=1, selection_mode="conteo")

        # insumo con stock suficiente
        item_ok = fx.make_inventory_item(db, current_stock=Decimal("10"))
        opt_ok = fx.make_option(
            db, group=group, inventory_item_id=item_ok.id, item_quantity=Decimal("1"),
        )
        # insumo con stock exacto al requerido
        item_exact = fx.make_inventory_item(db, current_stock=Decimal("2"))
        opt_exact = fx.make_option(
            db, group=group, inventory_item_id=item_exact.id, item_quantity=Decimal("2"),
        )
        # insumo con stock insuficiente
        item_short = fx.make_inventory_item(db, current_stock=Decimal("1"))
        opt_short = fx.make_option(
            db, group=group, inventory_item_id=item_short.id, item_quantity=Decimal("5"),
        )
        # opción ligada a insumo inactivo (aunque tenga stock)
        item_inactive = fx.make_inventory_item(db, current_stock=Decimal("99"), active=False)
        opt_inactive = fx.make_option(
            db, group=group, inventory_item_id=item_inactive.id, item_quantity=Decimal("1"),
        )
        fx.link_variant_group(
            db, variant, group, min_select=1, quantity_per_option=Decimal("0"),
        )
        db.commit()
        return product, variant, {
            "ok": opt_ok.id, "exact": opt_exact.id, "short": opt_short.id,
            "inactive": opt_inactive.id,
        }

    def test_opciones_disponibles_por_stock_e_inactividad(self):
        db = fx.new_session()
        product, variant, ids = self._seed(db)

        menu = _build_menu(db, only_visible_en_qr=False)
        pr = next(pr for c in menu for pr in c.products if pr.id == product.id)
        v = next(v for v in pr.variants if v.id == variant.id)
        options_by_id = {o.id: o for g in v.option_groups for o in g.options}

        self.assertTrue(options_by_id[ids["ok"]].available)
        self.assertTrue(options_by_id[ids["exact"]].available)
        self.assertFalse(options_by_id[ids["short"]].available)
        self.assertFalse(options_by_id[ids["inactive"]].available)

    def test_grupo_quantity_per_option_por_tamano_manda_sobre_item_quantity(self):
        """`quantity_per_option` del `VariantOptionGroup` (tamaño) manda sobre
        `item_quantity` de la opción cuando el tamaño sí lo define."""
        db = fx.new_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=Decimal("10000"))
        group = fx.make_option_group(db, min_select=1, selection_mode="conteo")
        item = fx.make_inventory_item(db, current_stock=Decimal("3"))
        # item_quantity=1 sugeriría disponible, pero el tamaño pide 5 por opción.
        opt = fx.make_option(
            db, group=group, inventory_item_id=item.id, item_quantity=Decimal("1"),
        )
        fx.link_variant_group(
            db, variant, group, min_select=1, quantity_per_option=Decimal("5"),
        )
        db.commit()

        menu = _build_menu(db, only_visible_en_qr=False)
        pr = next(pr for c in menu for pr in c.products if pr.id == product.id)
        v = next(v for v in pr.variants if v.id == variant.id)
        o = next(o for g in v.option_groups for o in g.options if o.id == opt.id)
        self.assertFalse(o.available)

    def test_grupo_obligatorio_sin_ninguna_opcion_disponible_deja_presentacion_no_pedible(self):
        db = fx.new_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=Decimal("10000"))
        group = fx.make_option_group(db, min_select=1, selection_mode="conteo")
        item = fx.make_inventory_item(db, current_stock=Decimal("0"))
        fx.make_option(
            db, group=group, inventory_item_id=item.id, item_quantity=Decimal("1"),
        )
        fx.link_variant_group(
            db, variant, group, min_select=1, quantity_per_option=Decimal("0"),
        )
        db.commit()

        menu = _build_menu(db, only_visible_en_qr=False)
        pr = next(pr for c in menu for pr in c.products if pr.id == product.id)
        v = next(v for v in pr.variants if v.id == variant.id)
        self.assertFalse(v.available)
        self.assertFalse(pr.available)  # única presentación, sin pedibles


class RecipeAvailabilityTests(unittest.TestCase):
    """Comportamiento NUEVO (anomalía A-154): el motor mira la receta, cosa que
    `_option_availability` nunca hacía."""

    def _variant_with_recipe(self, db, *, stock: Decimal, tracks_inventory: bool = True):
        category = fx.make_category(db)
        product = fx.make_product(db, category=category, tracks_inventory=tracks_inventory)
        variant = fx.make_variant(db, product=product, price=Decimal("10000"))
        item = fx.make_inventory_item(db, current_stock=stock)
        fx.make_recipe_item(db, variant, item, quantity=Decimal("2"))
        db.commit()
        return product, variant, item

    def test_receta_sin_stock_deja_presentacion_no_disponible(self):
        db = fx.new_session()
        product, variant, _item = self._variant_with_recipe(db, stock=Decimal("1"))

        avail = compute_availability(db)

        self.assertFalse(avail.variants[variant.id].available)
        self.assertFalse(avail.products[product.id].available)  # única presentación

    def test_producto_no_agotado_si_tiene_otra_presentacion_pedible(self):
        db = fx.new_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        sin_stock = fx.make_variant(db, product=product, price=Decimal("10000"))
        item = fx.make_inventory_item(db, current_stock=Decimal("0"))
        fx.make_recipe_item(db, sin_stock, item, quantity=Decimal("1"))
        con_stock = fx.make_variant(db, product=product, price=Decimal("12000"))
        db.commit()

        avail = compute_availability(db)

        self.assertFalse(avail.variants[sin_stock.id].available)
        self.assertTrue(avail.variants[con_stock.id].available)
        self.assertTrue(avail.products[product.id].available)

    def test_tracks_inventory_false_ignora_la_receta(self):
        db = fx.new_session()
        product, variant, _item = self._variant_with_recipe(
            db, stock=Decimal("0"), tracks_inventory=False,
        )

        avail = compute_availability(db)

        self.assertTrue(avail.variants[variant.id].available)
        self.assertTrue(avail.products[product.id].available)

    def test_insumo_compartido_por_varias_presentaciones(self):
        db = fx.new_session()
        category = fx.make_category(db)
        product_a = fx.make_product(db, category=category)
        product_b = fx.make_product(db, category=category)
        variant_a = fx.make_variant(db, product=product_a, price=Decimal("10000"))
        variant_b = fx.make_variant(db, product=product_b, price=Decimal("10000"))
        item = fx.make_inventory_item(db, current_stock=Decimal("1"))
        fx.make_recipe_item(db, variant_a, item, quantity=Decimal("5"))
        fx.make_recipe_item(db, variant_b, item, quantity=Decimal("5"))
        db.commit()

        avail = compute_availability(db)

        self.assertFalse(avail.variants[variant_a.id].available)
        self.assertFalse(avail.variants[variant_b.id].available)

    def test_stock_override_sustituye_current_stock(self):
        db = fx.new_session()
        product, variant, item = self._variant_with_recipe(db, stock=Decimal("0"))

        avail = compute_availability(db, stock={item.id: Decimal("10")})

        self.assertTrue(avail.variants[variant.id].available)
        self.assertTrue(avail.products[product.id].available)

    def test_scope_item_ids_da_el_mismo_estado_que_el_calculo_completo(self):
        db = fx.new_session()
        category = fx.make_category(db)
        product_a = fx.make_product(db, category=category)
        variant_a = fx.make_variant(db, product=product_a, price=Decimal("10000"))
        item_a = fx.make_inventory_item(db, current_stock=Decimal("0"))
        fx.make_recipe_item(db, variant_a, item_a, quantity=Decimal("1"))

        product_b = fx.make_product(db, category=category)
        variant_b = fx.make_variant(db, product=product_b, price=Decimal("10000"))
        item_b = fx.make_inventory_item(db, current_stock=Decimal("5"))
        fx.make_recipe_item(db, variant_b, item_b, quantity=Decimal("1"))
        db.commit()

        full = compute_availability(db)
        scoped = compute_availability(db, scope_item_ids=[item_a.id])

        self.assertIn(variant_a.id, scoped.variants)
        self.assertNotIn(variant_b.id, scoped.variants)
        self.assertEqual(
            scoped.variants[variant_a.id].available, full.variants[variant_a.id].available,
        )

    def test_scope_product_ids_da_el_mismo_estado_que_el_calculo_completo(self):
        db = fx.new_session()
        category = fx.make_category(db)
        product_a, variant_a, _item_a = self._variant_with_recipe(db, stock=Decimal("0"))
        product_b = fx.make_product(db, category=category)
        fx.make_variant(db, product=product_b, price=Decimal("10000"))
        db.commit()

        full = compute_availability(db)
        scoped = compute_availability(db, scope_product_ids=[product_a.id])

        self.assertIn(product_a.id, scoped.products)
        self.assertNotIn(product_b.id, scoped.products)
        self.assertEqual(
            scoped.products[product_a.id].available, full.products[product_a.id].available,
        )

    def test_sold_out_nunca_cambia_por_inventario(self):
        db = fx.new_session()
        product, _variant, _item = self._variant_with_recipe(db, stock=Decimal("0"))
        product.available = False  # marca manual "agotado"
        db.commit()

        avail = compute_availability(db)

        self.assertTrue(avail.products[product.id].sold_out)
        avail2 = compute_availability(db, stock={_item.id: Decimal("100")})
        self.assertTrue(avail2.products[product.id].sold_out)  # no cambia por stock


class DiffAvailabilityTests(unittest.TestCase):
    def test_sin_cruce_de_cero_no_hay_diferencia(self):
        db = fx.new_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=Decimal("10000"))
        item = fx.make_inventory_item(db, current_stock=Decimal("10"))
        fx.make_recipe_item(db, variant, item, quantity=Decimal("1"))
        db.commit()

        before = compute_availability(db, stock={item.id: Decimal("10")})
        after = compute_availability(db, stock={item.id: Decimal("9")})

        self.assertEqual(diff_availability(before, after), [])

    def test_cruzar_a_cero_produce_un_cambio(self):
        """Producto con una segunda presentación siempre pedible: cruzar el
        insumo a cero solo cambia la presentación afectada, no el producto."""
        db = fx.new_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=Decimal("10000"))
        fx.make_variant(db, product=product, price=Decimal("12000"))  # siempre pedible
        item = fx.make_inventory_item(db, current_stock=Decimal("1"))
        fx.make_recipe_item(db, variant, item, quantity=Decimal("1"))
        db.commit()

        before = compute_availability(db, stock={item.id: Decimal("1")})
        after = compute_availability(db, stock={item.id: Decimal("0")})

        changes = diff_availability(before, after)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["kind"], "variant")
        self.assertEqual(changes[0]["id"], variant.id)
        self.assertFalse(changes[0]["available"])

    def test_reponer_reaparece(self):
        db = fx.new_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=Decimal("10000"))
        fx.make_variant(db, product=product, price=Decimal("12000"))  # siempre pedible
        item = fx.make_inventory_item(db, current_stock=Decimal("0"))
        fx.make_recipe_item(db, variant, item, quantity=Decimal("1"))
        db.commit()

        before = compute_availability(db, stock={item.id: Decimal("0")})
        after = compute_availability(db, stock={item.id: Decimal("3")})

        changes = diff_availability(before, after)
        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0]["available"])


class QueryCountConstantTests(unittest.TestCase):
    """Número de consultas SQL constante, sin importar el tamaño del catálogo
    (research.md D3, plan.md «Presupuesto técnico»)."""

    def _catalog(self, db, n_products: int):
        category = fx.make_category(db)
        for _ in range(n_products):
            product = fx.make_product(db, category=category)
            variant = fx.make_variant(db, product=product, price=Decimal("10000"))
            item = fx.make_inventory_item(db, current_stock=Decimal("10"))
            fx.make_recipe_item(db, variant, item, quantity=Decimal("1"))
        db.commit()

    def _count_queries(self, db) -> int:
        count = {"n": 0}
        engine = db.get_bind()

        def _tick(*_args, **_kwargs):
            count["n"] += 1

        event.listen(engine, "before_cursor_execute", _tick)
        try:
            compute_availability(db)
        finally:
            event.remove(engine, "before_cursor_execute", _tick)
        return count["n"]

    def test_mismo_numero_de_consultas_con_5_y_50_productos(self):
        db_small = fx.new_session()
        self._catalog(db_small, 5)
        n_small = self._count_queries(db_small)

        db_big = fx.new_session()
        self._catalog(db_big, 50)
        n_big = self._count_queries(db_big)

        self.assertEqual(n_small, n_big)


if __name__ == "__main__":
    unittest.main()
