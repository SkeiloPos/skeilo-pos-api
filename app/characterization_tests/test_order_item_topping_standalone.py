"""Spec 129 (US2, FR-006/FR-007): una adición se agrega al pedido manual como línea
independiente exactamente igual que cualquier producto -- sin ningún cambio de
esquema (research.md D3): `add_item_to_order` con el `product_variant_id` de la
adición ya cobra `precio × cantidad` de esa línea, sin relación con otras líneas.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_order_item_topping_standalone -v
"""
from decimal import Decimal
import unittest

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders.consolidation import add_item_to_order
from app.api.v1.orders.schemas import OrderItemIn

PRECIO_GOMITAS = Decimal("2000")
PRECIO_GRANIZADO = Decimal("8000")


class TestOrderItemToppingStandalone(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.table = fx.make_dining_table(self.db)
        self.ts = fx.make_table_session(self.db, table=self.table)
        self.order = fx.make_customer_order(self.db, self.ts, dining_table_id=self.table.id)
        self.user = fx.make_user_double()
        # tracks_inventory=False: ningún insumo en juego -- lo que se prueba aquí es
        # el cobro de la línea, no el descuento de inventario.
        base_product = fx.make_product(self.db, tracks_inventory=False)
        category = base_product.category
        self.gomitas_product = fx.make_product(
            self.db, category=category, name="Gomitas", is_topping=True, tracks_inventory=False,
        )
        self.gomitas_variant = fx.make_variant(
            self.db, product=self.gomitas_product, price=PRECIO_GOMITAS
        )
        self.granizado_variant = fx.make_variant(
            self.db, product=base_product, price=PRECIO_GRANIZADO
        )
        self.db.commit()

    def test_adicion_vendida_suelta_se_cobra_precio_por_cantidad(self):
        order = add_item_to_order(
            self.db, self.order.id,
            OrderItemIn(product_variant_id=self.gomitas_variant.id, quantity=3), self.user,
        )
        self.db.commit()

        linea = next(i for i in order.items if i.product_variant_id == self.gomitas_variant.id)
        self.assertEqual(linea.quantity, 3)
        self.assertEqual(Decimal(linea.unit_price), PRECIO_GOMITAS)
        self.assertEqual(linea.line_total, PRECIO_GOMITAS * 3)

    def test_linea_de_adicion_suelta_es_independiente_de_otras_lineas(self):
        add_item_to_order(
            self.db, self.order.id,
            OrderItemIn(product_variant_id=self.granizado_variant.id, quantity=2), self.user,
        )
        order = add_item_to_order(
            self.db, self.order.id,
            OrderItemIn(product_variant_id=self.gomitas_variant.id, quantity=1), self.user,
        )
        self.db.commit()

        granizado_line = next(
            i for i in order.items if i.product_variant_id == self.granizado_variant.id
        )
        gomitas_line = next(
            i for i in order.items if i.product_variant_id == self.gomitas_variant.id
        )
        self.assertEqual(granizado_line.line_total, PRECIO_GRANIZADO * 2)
        self.assertEqual(gomitas_line.line_total, PRECIO_GOMITAS * 1)


if __name__ == "__main__":
    unittest.main()
