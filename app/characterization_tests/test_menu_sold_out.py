"""Spec 093-cajero-carta-agotado (User Story 2, escenarios 5/6): `_build_menu` deja de
ocultar un producto marcado "Agotado" (`Product.available=False`) y expone el flag
manual en un campo nuevo, `sold_out`, sin tocar el significado de `available` (que ya
existía y sigue siendo "pedible por stock de sus opciones obligatorias" --
characterization-protegido por `test_menu_router.py`, que este módulo no toca).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_menu_sold_out -v
"""
import unittest
from decimal import Decimal

from app.characterization_tests import cart_fixtures as fx
from app.api.v1.menu.router import _build_menu


class MenuSoldOutTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()

    def _producto_en_menu(self, nombre: str):
        categories = _build_menu(self.db)
        for cat in categories:
            for p in cat.products:
                if p.name == nombre:
                    return p
        return None

    def test_producto_agotado_sigue_en_el_menu_marcado_sold_out(self):
        category = fx.make_category(self.db)
        product = fx.make_product(self.db, category=category, name="Fresa boom", available=False)
        fx.make_variant(self.db, product=product, price=Decimal("8000"))
        self.db.commit()

        menu_product = self._producto_en_menu("Fresa boom")

        self.assertIsNotNone(menu_product, "un producto agotado ya NO debe ocultarse del menú")
        self.assertTrue(menu_product.sold_out)

    def test_producto_disponible_no_sale_marcado_sold_out(self):
        category = fx.make_category(self.db)
        product = fx.make_product(self.db, category=category, name="Oreo split", available=True)
        fx.make_variant(self.db, product=product, price=Decimal("8000"))
        self.db.commit()

        menu_product = self._producto_en_menu("Oreo split")

        self.assertIsNotNone(menu_product)
        self.assertFalse(menu_product.sold_out)

    def test_producto_inactivo_sigue_oculto_sin_cambios(self):
        """`active` no se toca por esta spec -- solo `available` deja de filtrarse."""
        category = fx.make_category(self.db)
        product = fx.make_product(self.db, category=category, name="Retirado", active=False)
        fx.make_variant(self.db, product=product, price=Decimal("8000"))
        self.db.commit()

        self.assertIsNone(self._producto_en_menu("Retirado"))

    def test_sold_out_es_independiente_de_available_pedible_por_stock(self):
        """Un producto puede ser `available=True` (pedible por stock) y
        `sold_out=True` (agotado manual) a la vez -- son dos conceptos distintos."""
        category = fx.make_category(self.db)
        product = fx.make_product(self.db, category=category, name="Mixto", available=False)
        fx.make_variant(self.db, product=product, price=Decimal("8000"))
        self.db.commit()

        menu_product = self._producto_en_menu("Mixto")

        self.assertTrue(menu_product.sold_out)
        self.assertTrue(menu_product.available)  # pedible por stock: sin grupos obligatorios sin stock


class MenuSoldOutByInventoryHookTests(unittest.TestCase):
    """T102 (spec 148, US6): el agotado por receta en `GET /menu` y su
    interacción con el gancho de inventario (`core/availability_hooks.py`) y
    la marca manual (FR-011: nunca se tocan entre sí)."""

    def setUp(self):
        self.db = fx.new_session()

    def _producto_en_menu(self, nombre: str):
        categories = _build_menu(self.db)
        for cat in categories:
            for p in cat.products:
                if p.name == nombre:
                    return p
        return None

    def test_variante_sin_insumo_de_receta_sale_available_false(self):
        category = fx.make_category(self.db)
        product = fx.make_product(self.db, category=category, name="Única presentación", available=True)
        variant = fx.make_variant(self.db, product=product, price=Decimal("8000"))
        item = fx.make_inventory_item(self.db, current_stock=Decimal("0"))
        fx.make_recipe_item(self.db, variant, item, quantity=Decimal("1"))
        self.db.commit()

        menu_product = self._producto_en_menu("Única presentación")

        self.assertFalse(menu_product.variants[0].available)
        # era su única presentación: el producto también queda no pedible.
        self.assertFalse(menu_product.available)
        self.assertFalse(menu_product.sold_out)  # la marca manual no cambió

    def test_marca_manual_con_insumo_suficiente_sigue_sold_out_tras_movimiento(self):
        from app.inventory_engine.stock import record_movement
        from app.core.availability_hooks import install_availability_hooks
        from unittest import mock

        install_availability_hooks()
        self.db.info["tenant_id"] = 7

        category = fx.make_category(self.db)
        product = fx.make_product(self.db, category=category, name="Agotado a mano", available=False)
        variant = fx.make_variant(self.db, product=product, price=Decimal("8000"))
        item = fx.make_inventory_item(self.db, current_stock=Decimal("10"))
        fx.make_recipe_item(self.db, variant, item, quantity=Decimal("1"))
        self.db.commit()

        with mock.patch("app.core.availability_hooks.events.availability_changed"):
            record_movement(self.db, item.id, type="out", quantity=Decimal("1"))
            self.db.commit()

        menu_product = self._producto_en_menu("Agotado a mano")
        self.assertTrue(menu_product.sold_out)  # la marca manual no se tocó
        self.assertTrue(menu_product.available)  # insumo sigue alcanzando (9 >= 1)

    def test_producto_sin_marca_y_sin_insumo_conserva_available_true_en_bd(self):
        """FR-011: `Product.available` (la marca manual) nunca cambia por
        inventario, ni en la respuesta del menú ni en la fila de la base de
        datos, antes o después de los movimientos y de los commits del gancho."""
        from app.inventory_engine.stock import record_movement
        from app.core.availability_hooks import install_availability_hooks
        from unittest import mock

        install_availability_hooks()
        self.db.info["tenant_id"] = 7

        category = fx.make_category(self.db)
        product = fx.make_product(self.db, category=category, name="Sin marca", available=True)
        variant = fx.make_variant(self.db, product=product, price=Decimal("8000"))
        item = fx.make_inventory_item(self.db, current_stock=Decimal("1"))
        fx.make_recipe_item(self.db, variant, item, quantity=Decimal("1"))
        self.db.commit()

        self.assertTrue(product.available)

        with mock.patch("app.core.availability_hooks.events.availability_changed"):
            record_movement(self.db, item.id, type="out", quantity=Decimal("1"))
            self.db.commit()

        self.db.refresh(product)
        self.assertTrue(product.available)  # sigue "disponible" a mano aunque no haya insumo

        menu_product = self._producto_en_menu("Sin marca")
        self.assertFalse(menu_product.sold_out)
        self.assertFalse(menu_product.available)  # no pedible por stock


if __name__ == "__main__":
    unittest.main()
