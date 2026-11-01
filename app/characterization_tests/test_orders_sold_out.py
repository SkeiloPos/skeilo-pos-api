"""Spec 093-cajero-carta-agotado (User Story 2, escenario 5): `add_item_to_order`
(Terminal de mesas) y `create_order` (pedido manual) rechazan un producto marcado
"Agotado", igual que `cart.service` ya hace para el canal QR
(`test_cart_sold_out.py`). `create_order` además nombra TODOS los productos agotados
de un pedido con varios ítems de una sola vez, sin crear nada -- ni siquiera los ítems
cuyo producto sí estaba disponible.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_orders_sold_out -v
"""
from decimal import Decimal
import unittest
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import consolidation, service
from app.api.v1.orders.schemas import OrderChannel, OrderCreate, OrderItemIn
from app.models.customer_order import CustomerOrder
from app.models.order_item import OrderItem

PRECIO = Decimal("10000")


class OrdersSoldOutTests(unittest.TestCase):
    def _seed_variant_con_receta(self, db, *, available: bool = True):
        category = fx.make_category(db)
        product = fx.make_product(db, category=category, available=available)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        insumo = fx.make_inventory_item(db, current_stock=Decimal("1000"))
        fx.make_recipe_item(db, variant, insumo, quantity=Decimal("1"))
        return product, variant

    def _user(self):
        return fx.make_user_double()

    # -- add_item_to_order (Terminal de mesas) -------------------------------

    def test_add_item_to_order_de_producto_agotado_409(self):
        db = fx.new_session()
        table = fx.make_dining_table(db)
        product, variant = self._seed_variant_con_receta(db, available=False)
        db.commit()
        user = self._user()
        order = consolidation.get_or_create_open_order(db, table.id, user.id)
        db.commit()

        data = OrderItemIn(product_variant_id=variant.id, quantity=1, options=[])
        with self.assertRaises(HTTPException) as ctx:
            consolidation.add_item_to_order(db, order.id, data, user)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn(product.name, ctx.exception.detail["error"])

    def test_add_item_to_order_de_producto_disponible_sigue_funcionando(self):
        db = fx.new_session()
        table = fx.make_dining_table(db)
        _, variant = self._seed_variant_con_receta(db, available=True)
        db.commit()
        user = self._user()
        order = consolidation.get_or_create_open_order(db, table.id, user.id)
        db.commit()

        data = OrderItemIn(product_variant_id=variant.id, quantity=1, options=[])
        order = consolidation.add_item_to_order(db, order.id, data, user)

        self.assertEqual(len(order.items), 1)

    # -- create_order (pedido manual, varios ítems) --------------------------

    def test_create_order_con_un_item_agotado_rechaza_sin_crear_nada(self):
        db = fx.new_session()
        product_ok, variant_ok = self._seed_variant_con_receta(db, available=True)
        product_agotado, variant_agotado = self._seed_variant_con_receta(db, available=False)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            customer_name="Cliente de prueba",
            items=[
                OrderItemIn(product_variant_id=variant_ok.id, quantity=1, options=[]),
                OrderItemIn(product_variant_id=variant_agotado.id, quantity=1, options=[]),
            ],
        )

        with self.assertRaises(HTTPException) as ctx:
            service.create_order(db, data, uuid4())

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn(product_agotado.name, ctx.exception.detail["productos_agotados"])
        # Nada se creó -- ni la orden, ni el ítem del producto que sí estaba disponible.
        self.assertIsNone(db.execute(select(CustomerOrder)).scalar_one_or_none())
        self.assertIsNone(db.execute(select(OrderItem)).scalar_one_or_none())

    def test_create_order_nombra_varios_productos_agotados_a_la_vez(self):
        db = fx.new_session()
        p1, v1 = self._seed_variant_con_receta(db, available=False)
        p2, v2 = self._seed_variant_con_receta(db, available=False)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            customer_name="Cliente de prueba",
            items=[
                OrderItemIn(product_variant_id=v1.id, quantity=1, options=[]),
                OrderItemIn(product_variant_id=v2.id, quantity=1, options=[]),
            ],
        )

        with self.assertRaises(HTTPException) as ctx:
            service.create_order(db, data, uuid4())

        self.assertCountEqual(ctx.exception.detail["productos_agotados"], [p1.name, p2.name])

    def test_create_order_sin_productos_agotados_sigue_funcionando(self):
        db = fx.new_session()
        _, variant = self._seed_variant_con_receta(db, available=True)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            customer_name="Cliente de prueba",
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[])],
        )
        order = service.create_order(db, data, uuid4())

        self.assertEqual(order.status, "pendiente")

    # -- escenario 8: un pedido ya creado no se revalida ---------------------

    def test_pedido_ya_creado_no_se_afecta_al_marcar_el_producto_agotado_despues(self):
        db = fx.new_session()
        _, variant = self._seed_variant_con_receta(db, available=True)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            customer_name="Cliente de prueba",
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[])],
        )
        order = service.create_order(db, data, uuid4())
        order_id = order.id

        variant.product.available = False
        db.commit()

        # El pedido conserva su ítem y su estado, sin ninguna revalidación.
        reloaded = db.get(CustomerOrder, order_id)
        self.assertEqual(reloaded.status, "pendiente")
        self.assertEqual(len(reloaded.items), 1)


if __name__ == "__main__":
    unittest.main()
