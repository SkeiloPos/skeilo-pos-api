"""Spec 117 (A-126), T013: no existía hasta ahora ningún characterization test que
ejercitara el camino de precio de `sales/service.py::checkout` (venta de mostrador) de
punta a punta -- los `test_sales_payment_methods*.py` solo cubren disponibilidad/catálogo
de métodos de pago. Al escribirlo se descubrió **A-127** (bug preexistente, no
relacionado con esta spec): `checkout.auto_discount(...)` lanzaba `AttributeError`
siempre, porque el nombre del módulo `checkout` quedaba sombreado por la función
`def checkout(...)` del propio archivo -- corregido (alias `orders_checkout`) para que
este test pudiera, por primera vez, llamar a `service.checkout` real.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_sales_service_checkout -v
"""
from decimal import Decimal
import unittest

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.sales import service
from app.api.v1.sales.schemas import SaleCreate, SaleItemIn, PaymentIn
from app.api.v1.catalog.schemas import OptionSelectionIn


class TestSalesServiceCheckoutAddonsPerLine(unittest.TestCase):
    """Spec 117 (A-126): un grupo "Por cantidad" se cobra/consume por línea también en
    la venta de mostrador -- un grupo "Conteo" con recargo sigue folded (protegido,
    FR-013)."""

    def setUp(self):
        self.db = fx.new_session()
        self.variant = fx.make_variant(self.db, price=Decimal("15000"))
        insumo = fx.make_inventory_item(self.db, current_stock=Decimal("100"))
        fx.make_recipe_item(self.db, self.variant, insumo, quantity=Decimal("1"))
        self.shift = fx.make_cash_shift(self.db)
        self.efectivo = fx.make_payment_method(self.db, name="efe", is_cash=True)
        self.db.commit()
        self.cashier = fx.make_user_double()

    def _checkout(self, *, quantity, option, option_quantity=1, amount):
        data = SaleCreate(
            cash_shift_id=self.shift.id,
            items=[SaleItemIn(
                product_variant_id=self.variant.id, quantity=quantity,
                options=[OptionSelectionIn(option_id=option.id, quantity=option_quantity)],
            )],
            payments=[PaymentIn(payment_method_id=self.efectivo.id, amount=amount)],
        )
        return service.checkout(self.db, data, self.cashier)

    def test_grupo_cantidad_separa_unit_price_y_addons_total_y_marca_per_line_en_el_snapshot(self):
        group = fx.make_option_group(
            self.db, selection_mode="cantidad", pricing_type="incluido",
            min_select=0, max_select=1,
        )
        fx.link_variant_group(self.db, self.variant, group, min_select=0, max_select=1)
        gomitas = fx.make_option(self.db, group=group, extra_price=Decimal("1000"))

        sale = self._checkout(quantity=2, option=gomitas, amount=Decimal("50000"))

        [item] = sale.items
        self.assertEqual(item.unit_price, Decimal("15000"))
        self.assertEqual(item.line_total, Decimal("31000"))
        self.assertTrue(item.options[0].get("per_line"))

    def test_grupo_conteo_con_recargo_sigue_folded(self):
        group = fx.make_option_group(
            self.db, selection_mode="conteo", pricing_type="con_recargo",
            min_select=0, max_select=1,
        )
        fx.link_variant_group(self.db, self.variant, group, min_select=0, max_select=1)
        tocino = fx.make_option(self.db, group=group, extra_price=Decimal("3000"))

        sale = self._checkout(quantity=2, option=tocino, amount=Decimal("50000"))

        [item] = sale.items
        self.assertEqual(item.unit_price, Decimal("18000"))
        self.assertEqual(item.line_total, Decimal("36000"))
        self.assertNotIn("per_line", item.options[0])


if __name__ == "__main__":
    unittest.main()
