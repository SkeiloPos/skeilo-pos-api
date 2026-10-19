"""Spec 129: `settle_pending_balance` (spec 123) liquida también adiciones
(productos-topping, `OrderItemTopping`) pendientes, no solo adiciones de sabor
(`OrderItemOption`) -- sin esto, una adición agregada después de una `Sale`
emitida quedaría correctamente marcada `added_after_sale`, pero nunca se podría
cobrar con el pago complementario existente.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_sales_complementary_topping -v
"""
from decimal import Decimal
import unittest
from unittest import mock

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import toppings
from app.api.v1.sales import complementary
from app.api.v1.sales.schemas import ComplementaryPaymentIn, PaymentIn
from app.api.v1.catalog.schemas import ToppingSelectionIn
from app.models.order_item import OrderItemTopping
from app.models.product_topping import ProductTopping

PRECIO = Decimal("15000")
PRECIO_GOMITAS = Decimal("2000")


class TestSettlePendingBalanceWithToppings(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.table = fx.make_dining_table(self.db)
        self.ts = fx.make_table_session(self.db, table=self.table)
        self.order = fx.make_customer_order(self.db, self.ts, dining_table_id=self.table.id)
        self.variant = fx.make_variant(self.db, price=PRECIO)
        self.gomitas_product = fx.make_product(
            self.db, category=self.variant.product.category, name="Gomitas", is_topping=True,
        )
        self.gomitas_variant = fx.make_variant(
            self.db, product=self.gomitas_product, price=PRECIO_GOMITAS,
        )
        self.db.add(ProductTopping(
            product_id=self.variant.product_id, topping_product_id=self.gomitas_product.id,
        ))
        self.user = fx.make_user_double()
        self.tenant = fx.make_tenant_double()
        fx.make_sale(self.db, order=self.order, total=Decimal("22000"))

        self.register = fx.make_cash_register(self.db)
        self.shift = fx.make_cash_shift(self.db, register=self.register)
        self.method = fx.make_payment_method(self.db)
        self.db.commit()

    def _agregar_adicion(self):
        item = fx.make_order_item(
            self.db, self.order, self.variant, quantity=1,
            unit_price=PRECIO, estado_cocina="pendiente",
        )
        body = ToppingSelectionIn(topping_product_id=self.gomitas_product.id, quantity=1)
        with mock.patch.object(toppings, "has_module_access", return_value=False):
            toppings.add_topping_to_order_item(self.db, item.id, body, self.user, self.tenant)
        return item

    def _cobrar(self, monto):
        body = ComplementaryPaymentIn(
            cash_shift_id=self.shift.id,
            payments=[PaymentIn(payment_method_id=self.method.id, amount=monto)],
        )
        with mock.patch.object(complementary, "has_module_access", return_value=False):
            return complementary.settle_pending_balance(self.db, self.order.id, body, self.user, self.tenant)

    def test_cobro_liquida_la_adicion_pendiente_y_marca_la_fila(self):
        self._agregar_adicion()

        sale = self._cobrar(PRECIO_GOMITAS)

        self.assertEqual(sale.kind, "complementario")
        self.assertEqual(sale.total, PRECIO_GOMITAS)
        [fila] = self.db.query(OrderItemTopping).all()
        self.assertEqual(fila.complementary_sale_id, sale.id)

    def test_cobro_mixto_de_una_adicion_de_sabor_y_una_adicion_producto_suma_ambas(self):
        from app.api.v1.orders import additions
        group = fx.make_option_group(self.db, selection_mode="cantidad")
        fx.link_variant_group(self.db, self.variant, group)
        option = fx.make_option(self.db, group=group, extra_price=Decimal("1000"))
        item = fx.make_order_item(
            self.db, self.order, self.variant, quantity=1,
            unit_price=PRECIO, estado_cocina="pendiente",
        )
        from app.api.v1.catalog.schemas import OptionSelectionIn
        with mock.patch.object(additions, "has_module_access", return_value=False):
            additions.add_addition_to_order_item(
                self.db, item.id, OptionSelectionIn(option_id=option.id, quantity=1),
                self.user, self.tenant,
            )
        self._agregar_adicion()

        sale = self._cobrar(Decimal("1000") + PRECIO_GOMITAS)

        self.assertEqual(sale.total, Decimal("1000") + PRECIO_GOMITAS)


if __name__ == "__main__":
    unittest.main()
