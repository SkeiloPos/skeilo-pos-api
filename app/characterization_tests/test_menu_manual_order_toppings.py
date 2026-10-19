"""Spec 129 (US2, FR-006): el catálogo del pedido manual es, en realidad, `GET /menu`
sin firmar (`_build_menu(..., only_visible_en_qr=False)`, único caller:
`MenuService`/`pos-terminal.store.ts`), no `GET /products` -- `manual-order-
page.component.ts` lee de ahí. Un producto marcado `is_topping=true` (que nace con
`visible_en_qr=False`, spec 129 FR-010) sigue apareciendo ahí sin filtrarse, porque
esa ruta nunca aplicó el filtro de `visible_en_qr` (solo la del comensal lo hace --
ver `test_menu_topping_visibility.py`, spec 129 US3).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_menu_manual_order_toppings -v
"""
import unittest
from decimal import Decimal

from app.characterization_tests import cart_fixtures as fx
from app.api.v1.menu.router import _build_menu


class MenuManualOrderToppingsTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()

    def _producto_en_menu(self, nombre: str, *, only_visible_en_qr: bool = False):
        categories = _build_menu(self.db, only_visible_en_qr=only_visible_en_qr)
        for cat in categories:
            for p in cat.products:
                if p.name == nombre:
                    return p
        return None

    def test_adicion_aparece_en_el_catalogo_del_pedido_manual(self):
        category = fx.make_category(self.db)
        product = fx.make_product(
            self.db, category=category, name="Gomitas", is_topping=True, visible_en_qr=False,
        )
        fx.make_variant(self.db, product=product, price=Decimal("2000"))
        self.db.commit()

        menu_product = self._producto_en_menu("Gomitas", only_visible_en_qr=False)

        self.assertIsNotNone(
            menu_product, "una adición debe seguir pedible desde el pedido manual"
        )


if __name__ == "__main__":
    unittest.main()
