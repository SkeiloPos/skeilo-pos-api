"""Spec 129 (US3, FR-014, Edge Case): si el administrador desasocia una adición
del producto mientras el comensal ya la tenía adjunta a una línea del carrito,
`submit_cart` rechaza el envío y quita esa adición de la línea -- sin tocar el
resto del pedido (la línea principal sigue siendo válida, mismo patrón que spec
128/US2 para una categoría oculta, pero aquí solo se quita la adición, no la
línea completa).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_cart_topping_rejection -v
"""
from decimal import Decimal
import unittest
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.characterization_tests import cart_fixtures as fx
from app.api.v1.cart import service
from app.api.v1.cart.schemas import CartItemIn
from app.api.v1.catalog.schemas import ToppingSelectionIn
from app.models.cart import Cart
from app.models.customer_order import CustomerOrder
from app.models.product_topping import ProductTopping

PRECIO = Decimal("8000")
PRECIO_GOMITAS = Decimal("2000")


class CartToppingRejectionTests(unittest.TestCase):
    def _seed_session(self):
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        participant = fx.make_participant(db, table_session=ts)
        return db, participant

    def _efectivo(self, db):
        return fx.make_payment_method(db, name=f"efectivo-{uuid4()}", is_cash=True)

    def test_submit_cart_con_adicion_desasociada_rechaza_y_la_quita_sin_perder_la_linea(self):
        db, participant = self._seed_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category, name="Granizado")
        variant = fx.make_variant(db, product=product, price=PRECIO)
        gomitas = fx.make_product(db, category=category, name="Gomitas", is_topping=True)
        gomitas_variant = fx.make_variant(db, product=gomitas, price=PRECIO_GOMITAS)
        link = ProductTopping(product_id=product.id, topping_product_id=gomitas.id)
        db.add(link)
        db.commit()
        efectivo = self._efectivo(db)

        service.add_item(
            db, participant.id,
            CartItemIn(
                product_variant_id=variant.id, quantity=1,
                toppings=[ToppingSelectionIn(topping_product_id=gomitas.id, quantity=1)],
            ),
        )

        # El administrador desasocia la adición después de que el comensal la agregó.
        db.delete(db.get(ProductTopping, link.id))
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.submit_cart(db, participant, efectivo.id)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Gomitas", ctx.exception.detail["productos_no_disponibles"])
        self.assertEqual(
            ctx.exception.detail["error"],
            "Algunas adiciones ya no están disponibles y se quitaron de tu carrito",
        )

        # No se creó ninguna orden.
        self.assertIsNone(db.execute(select(CustomerOrder)).scalar_one_or_none())
        # La línea principal (Granizado) sigue en el carrito, sin la adición.
        cart = db.execute(
            select(Cart).where(Cart.participant_id == participant.id, Cart.status == "abierto")
        ).scalar_one()
        self.assertEqual(len(cart.items), 1)
        self.assertEqual(cart.items[0].toppings, [])
        self.assertEqual(cart.items[0].addons_total, Decimal("0"))

    def test_submit_cart_con_adicion_todavia_asociada_sigue_funcionando(self):
        db, participant = self._seed_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category, name="Granizado")
        variant = fx.make_variant(db, product=product, price=PRECIO)
        gomitas = fx.make_product(db, category=category, name="Gomitas", is_topping=True)
        fx.make_variant(db, product=gomitas, price=PRECIO_GOMITAS)
        db.add(ProductTopping(product_id=product.id, topping_product_id=gomitas.id))
        db.commit()
        efectivo = self._efectivo(db)

        service.add_item(
            db, participant.id,
            CartItemIn(
                product_variant_id=variant.id, quantity=1,
                toppings=[ToppingSelectionIn(topping_product_id=gomitas.id, quantity=1)],
            ),
        )

        order = service.submit_cart(db, participant, efectivo.id)

        self.assertIsNotNone(order)


if __name__ == "__main__":
    unittest.main()
