"""Spec 129 (US3, FR-008/FR-009/FR-010): una adición nunca aparece como tarjeta/
resultado propio en el menú QR firmado del comensal (`only_visible_en_qr=True`),
pero sí aparece en `toppings` del producto al que está asociada.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_menu_topping_visibility -v
"""
import unittest
from decimal import Decimal

from app.characterization_tests import cart_fixtures as fx
from app.api.v1.menu.router import _build_menu
from app.models.product_topping import ProductTopping


class MenuToppingVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.category = fx.make_category(self.db)

    def _producto_en_menu(self, nombre: str, *, only_visible_en_qr: bool):
        categories = _build_menu(self.db, only_visible_en_qr=only_visible_en_qr)
        for cat in categories:
            for p in cat.products:
                if p.name == nombre:
                    return p
        return None

    def test_adicion_no_aparece_como_tarjeta_propia_en_el_menu_qr_firmado(self):
        topping = fx.make_product(
            self.db, category=self.category, name="Gomitas", is_topping=True,
            visible_en_qr=False,
        )
        fx.make_variant(self.db, product=topping, price=Decimal("2000"))
        self.db.commit()

        self.assertIsNone(self._producto_en_menu("Gomitas", only_visible_en_qr=True))

    def test_adicion_sigue_disponible_en_el_catalogo_sin_firmar(self):
        """El mismo producto, por el caller sin firmar (pedido manual) -- spec 129 US2."""
        topping = fx.make_product(
            self.db, category=self.category, name="Gomitas", is_topping=True,
            visible_en_qr=False,
        )
        fx.make_variant(self.db, product=topping, price=Decimal("2000"))
        self.db.commit()

        self.assertIsNotNone(self._producto_en_menu("Gomitas", only_visible_en_qr=False))

    def test_adicion_aparece_en_toppings_del_producto_que_la_ofrece(self):
        granizado = fx.make_product(self.db, category=self.category, name="Granizado")
        fx.make_variant(self.db, product=granizado, price=Decimal("8000"))
        gomitas = fx.make_product(
            self.db, category=self.category, name="Gomitas", is_topping=True,
        )
        fx.make_variant(self.db, product=gomitas, price=Decimal("2000"))
        self.db.add(ProductTopping(product_id=granizado.id, topping_product_id=gomitas.id))
        self.db.commit()

        menu_product = self._producto_en_menu("Granizado", only_visible_en_qr=True)

        self.assertIsNotNone(menu_product)
        self.assertEqual(len(menu_product.toppings), 1)
        topping_resp = menu_product.toppings[0]
        self.assertEqual(topping_resp.id, gomitas.id)
        self.assertEqual(topping_resp.name, "Gomitas")
        self.assertEqual(topping_resp.unit_price, Decimal("2000"))
        self.assertTrue(topping_resp.available)

    def test_producto_sin_adiciones_asociadas_trae_toppings_vacio(self):
        granizado = fx.make_product(self.db, category=self.category, name="Granizado")
        fx.make_variant(self.db, product=granizado, price=Decimal("8000"))
        self.db.commit()

        menu_product = self._producto_en_menu("Granizado", only_visible_en_qr=True)

        self.assertIsNotNone(menu_product)
        self.assertEqual(menu_product.toppings, [])

    def test_adicion_inactiva_sale_marcada_no_disponible_en_toppings(self):
        granizado = fx.make_product(self.db, category=self.category, name="Granizado")
        fx.make_variant(self.db, product=granizado, price=Decimal("8000"))
        gomitas = fx.make_product(
            self.db, category=self.category, name="Gomitas", is_topping=True,
            available=False,
        )
        fx.make_variant(self.db, product=gomitas, price=Decimal("2000"))
        self.db.add(ProductTopping(product_id=granizado.id, topping_product_id=gomitas.id))
        self.db.commit()

        menu_product = self._producto_en_menu("Granizado", only_visible_en_qr=True)

        self.assertFalse(menu_product.toppings[0].available)


if __name__ == "__main__":
    unittest.main()
