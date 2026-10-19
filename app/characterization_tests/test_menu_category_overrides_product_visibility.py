"""Spec 129 (US4, FR-012): si la categoría de un producto está oculta en el
menú QR, el producto sigue sin aparecer sin importar su propio
`visible_en_qr` -- la visibilidad de categoría (spec 128) manda sobre la de
producto. Ya garantizado por la estructura existente de `_build_menu`: solo
se itera `by_cat.get(cat.id)` de categorías ya filtradas por
`Category.visible_en_qr`, así que un producto de una categoría oculta nunca
llega a evaluarse, sin importar su propio flag.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_menu_category_overrides_product_visibility -v
"""
import unittest
from decimal import Decimal

from app.characterization_tests import cart_fixtures as fx
from app.api.v1.menu.router import _build_menu


class MenuCategoryOverridesProductVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()

    def _producto_en_menu(self, nombre: str):
        categories = _build_menu(self.db, only_visible_en_qr=True)
        for cat in categories:
            for p in cat.products:
                if p.name == nombre:
                    return p
        return None

    def test_producto_visible_en_qr_sigue_oculto_si_su_categoria_esta_oculta(self):
        category = fx.make_category(self.db, visible_en_qr=False)
        product = fx.make_product(
            self.db, category=category, name="Granizado", visible_en_qr=True,
        )
        fx.make_variant(self.db, product=product, price=Decimal("8000"))
        self.db.commit()

        self.assertIsNone(self._producto_en_menu("Granizado"))

    def test_producto_visible_en_qr_aparece_si_su_categoria_tambien_es_visible(self):
        category = fx.make_category(self.db, visible_en_qr=True)
        product = fx.make_product(
            self.db, category=category, name="Granizado", visible_en_qr=True,
        )
        fx.make_variant(self.db, product=product, price=Decimal("8000"))
        self.db.commit()

        self.assertIsNotNone(self._producto_en_menu("Granizado"))

    def test_producto_oculto_no_aparece_aunque_su_categoria_sea_visible(self):
        category = fx.make_category(self.db, visible_en_qr=True)
        product = fx.make_product(
            self.db, category=category, name="Gomitas", visible_en_qr=False,
        )
        fx.make_variant(self.db, product=product, price=Decimal("2000"))
        self.db.commit()

        self.assertIsNone(self._producto_en_menu("Gomitas"))


if __name__ == "__main__":
    unittest.main()
