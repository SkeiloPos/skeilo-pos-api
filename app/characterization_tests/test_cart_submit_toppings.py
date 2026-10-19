"""Spec 129 (US3): `submit_cart` copia `CartItemTopping` -> `OrderItemTopping`
tal cual (snapshot, nunca recalculado) -- mismo criterio que ya aplica a
`CartItemOption` -> `OrderItemOption` (spec 089).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_cart_submit_toppings -v
"""
from decimal import Decimal
import unittest
from uuid import uuid4

from app.characterization_tests import cart_fixtures as fx
from app.api.v1.cart import service
from app.api.v1.cart.schemas import CartItemIn
from app.api.v1.catalog.schemas import ToppingSelectionIn
from app.models.product_topping import ProductTopping
from app.models.order_item import OrderItemTopping

PRECIO = Decimal("8000")
PRECIO_GOMITAS = Decimal("2000")


class CartSubmitToppingsTests(unittest.TestCase):
    def test_submit_cart_copia_la_adicion_a_la_orden(self):
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        participant = fx.make_participant(db, table_session=ts)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category, name="Granizado")
        variant = fx.make_variant(db, product=product, price=PRECIO)
        gomitas = fx.make_product(db, category=category, name="Gomitas", is_topping=True)
        gomitas_variant = fx.make_variant(db, product=gomitas, price=PRECIO_GOMITAS)
        db.add(ProductTopping(product_id=product.id, topping_product_id=gomitas.id))
        efectivo = fx.make_payment_method(db, name=f"efectivo-{uuid4()}", is_cash=True)
        db.commit()

        service.add_item(
            db, participant.id,
            CartItemIn(
                product_variant_id=variant.id, quantity=1,
                toppings=[ToppingSelectionIn(topping_product_id=gomitas.id, quantity=2)],
            ),
        )

        order = service.submit_cart(db, participant, efectivo.id)

        [item] = order.items
        self.assertEqual(item.addons_total, PRECIO_GOMITAS * 2)
        [fila] = db.query(OrderItemTopping).filter_by(order_item_id=item.id).all()
        self.assertEqual(fila.topping_product_variant_id, gomitas_variant.id)
        self.assertEqual(fila.quantity, 2)
        self.assertEqual(Decimal(fila.unit_price), PRECIO_GOMITAS)
        self.assertFalse(fila.added_after_sale)
        self.assertIsNone(fila.complementary_sale_id)


if __name__ == "__main__":
    unittest.main()
