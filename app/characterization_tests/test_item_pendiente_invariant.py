"""Spec 146 (FR-003, T015): invariante ya existente que esta spec fija
explícitamente como characterization test -- `OrderItem.estado_cocina` nace en
`'pendiente'` para todo ítem nuevo, sin importar el canal (QR, caja/mostrador,
domicilio) ni el `status` de la orden a la que pertenece. research.md § 2
confirma que `app/models/order_item.py` ya cumplía esto antes de A-150; aquí se
ejercitan los tres caminos reales de creación, no solo el default de columna.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_item_pendiente_invariant -v
"""
from decimal import Decimal
import unittest
from uuid import uuid4

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.cart import service as cart_service
from app.api.v1.cart.schemas import CartItemIn
from app.api.v1.orders import service as orders_service
from app.api.v1.orders.schemas import OrderChannel, OrderCreate, OrderItemIn, OrderType

PRECIO = Decimal("8000")


class TestItemNacePendienteEnTodoCanal(unittest.TestCase):
    def _seed_variant(self, db):
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        return fx.make_variant(db, product=product, price=PRECIO)

    def test_pos_dine_in_items_nacen_pendiente(self):
        db = fx.new_session()
        variant = self._seed_variant(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            order_type=OrderType.DINE_IN,
            customer_name="Cliente de prueba",
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1)],
        )
        order = orders_service.create_order(db, data, uuid4())

        self.assertEqual(order.status, "pendiente")
        self.assertTrue(order.items)
        for item in order.items:
            self.assertEqual(item.estado_cocina, "pendiente")

    def test_pos_delivery_items_nacen_pendiente(self):
        db = fx.new_session()
        variant = self._seed_variant(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            order_type=OrderType.DELIVERY,
            customer_name="Cliente de prueba",
            delivery_address="Calle 1 # 2-3",
            delivery_fee=Decimal("3000"),
            items=[OrderItemIn(product_variant_id=variant.id, quantity=2)],
        )
        order = orders_service.create_order(db, data, uuid4())

        self.assertEqual(order.status, "pendiente")
        self.assertTrue(order.items)
        for item in order.items:
            self.assertEqual(item.estado_cocina, "pendiente")

    def test_qr_items_nacen_pendiente(self):
        db = fx.new_session()
        table = fx.make_dining_table(db, status="libre")
        ts = fx.make_table_session(db, table=table)
        participant = fx.make_participant(db, table_session=ts)
        variant = self._seed_variant(db)
        efectivo = fx.make_payment_method(db, name="efectivo", is_cash=True)
        db.commit()

        cart_service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))
        order = cart_service.submit_cart(db, participant, efectivo.id)

        self.assertEqual(order.status, "pendiente")
        self.assertTrue(order.items)
        for item in order.items:
            self.assertEqual(item.estado_cocina, "pendiente")


if __name__ == "__main__":
    unittest.main()
