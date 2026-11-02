"""Spec 093-cajero-carta-agotado (User Story 2, escenario 7): `cart.service` rechaza
agregar/editar un ítem de un producto marcado "Agotado", y rechaza confirmar todo el
carrito (`submit_cart`) si alguno de sus ítems quedó agotado después de haberse
agregado -- nombrando todos los productos agotados de una vez, sin crear ninguna orden
y sin tocar el carrito.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_cart_sold_out -v
"""
from decimal import Decimal
import unittest
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.characterization_tests import cart_fixtures as fx
from app.api.v1.cart import service
from app.api.v1.cart.schemas import CartItemIn, CartItemUpdate
from app.models.cart import Cart
from app.models.customer_order import CustomerOrder


class CartSoldOutTests(unittest.TestCase):
    def _seed_session(self):
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        participant = fx.make_participant(db, table_session=ts)
        return db, participant

    def _seed_variant(self, db, **kw):
        category = fx.make_category(db)
        available = kw.pop("available", True)
        product = fx.make_product(db, category=category, available=available)
        kw.setdefault("price", Decimal("8000"))
        return fx.make_variant(db, product=product, **kw), product

    def _efectivo(self, db):
        return fx.make_payment_method(db, name=f"efectivo-{uuid4()}", is_cash=True)

    # -- add_item / update_item (single item) ---------------------------------

    def test_add_item_de_producto_agotado_409_nombra_el_producto(self):
        db, participant = self._seed_session()
        variant, product = self._seed_variant(db, available=False)

        with self.assertRaises(HTTPException) as ctx:
            service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn(product.name, ctx.exception.detail["error"])
        self.assertEqual(ctx.exception.detail["producto"], product.name)

    def test_update_item_hacia_producto_agotado_409(self):
        db, participant = self._seed_session()
        variant, product = self._seed_variant(db, available=True)
        added = service.add_item(
            db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1)
        )
        item_id = added.items[0].id

        # Se marca agotado DESPUÉS de agregado -- igual que el escenario 7.
        product.available = False
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.update_item(db, participant.id, item_id, CartItemUpdate(quantity=2))

        self.assertEqual(ctx.exception.status_code, 409)

    def test_add_item_de_producto_disponible_sigue_funcionando(self):
        db, participant = self._seed_session()
        variant, _ = self._seed_variant(db, available=True)

        resp = service.add_item(
            db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1)
        )

        self.assertEqual(len(resp.items), 1)

    # -- submit_cart (multi item) -----------------------------------------------

    def test_submit_cart_con_un_producto_agotado_rechaza_todo_sin_crear_orden(self):
        """Escenario 7: el comensal agregó el producto mientras estaba disponible;
        un cajero lo marca agotado antes de que confirme -- se rechaza el envío
        completo, nombrando el producto, sin crear ninguna orden, y el carrito
        se conserva intacto."""
        db, participant = self._seed_session()
        variant, product = self._seed_variant(db, available=True)
        efectivo = self._efectivo(db)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))

        product.available = False
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.submit_cart(db, participant, efectivo.id)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn(product.name, ctx.exception.detail["productos_agotados"])
        self.assertIn(product.name, ctx.exception.detail["error"])

        # No se creó ninguna orden.
        self.assertIsNone(db.execute(select(CustomerOrder)).scalar_one_or_none())
        # El carrito se conserva, con su ítem.
        cart = db.execute(
            select(Cart).where(Cart.participant_id == participant.id, Cart.status == "abierto")
        ).scalar_one()
        self.assertEqual(len(cart.items), 1)

    def test_submit_cart_nombra_varios_productos_agotados_a_la_vez(self):
        db, participant = self._seed_session()
        v1, p1 = self._seed_variant(db, available=True)
        v2, p2 = self._seed_variant(db, available=True)
        efectivo = self._efectivo(db)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=v1.id, quantity=1))
        service.add_item(db, participant.id, CartItemIn(product_variant_id=v2.id, quantity=1))

        p1.available = False
        p2.available = False
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.submit_cart(db, participant, efectivo.id)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertCountEqual(ctx.exception.detail["productos_agotados"], [p1.name, p2.name])

    def test_submit_cart_resto_del_carrito_se_conserva_junto_al_agotado(self):
        """El resto del carrito (el producto que SÍ sigue disponible) se conserva,
        no solo el agotado."""
        db, participant = self._seed_session()
        v1, p1 = self._seed_variant(db, available=True)
        v2, p2 = self._seed_variant(db, available=True)
        efectivo = self._efectivo(db)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=v1.id, quantity=1))
        service.add_item(db, participant.id, CartItemIn(product_variant_id=v2.id, quantity=1))

        p1.available = False
        db.commit()

        with self.assertRaises(HTTPException):
            service.submit_cart(db, participant, efectivo.id)

        cart = db.execute(
            select(Cart).where(Cart.participant_id == participant.id, Cart.status == "abierto")
        ).scalar_one()
        self.assertEqual(len(cart.items), 2)

    def test_submit_cart_sin_productos_agotados_sigue_funcionando(self):
        db, participant = self._seed_session()
        variant, _ = self._seed_variant(db, available=True)
        efectivo = self._efectivo(db)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))

        order = service.submit_cart(db, participant, efectivo.id)

        self.assertEqual(order.status, "pendiente")


if __name__ == "__main__":
    unittest.main()
