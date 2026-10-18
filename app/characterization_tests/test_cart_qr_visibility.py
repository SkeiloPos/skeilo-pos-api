"""Spec 128-categoria-visible-menu-qr (User Story 2, FR-007): `submit_cart`
rechaza confirmar un carrito si alguno de sus ítems pertenece a una categoría
que dejó de ser visible en QR (oculta, o desactivada) DESPUÉS de que el
comensal lo agregara -- nombrando todos los productos afectados de una vez,
sin crear ninguna orden. A diferencia de spec 093 (productos agotados,
`test_cart_sold_out.py`), aquí el carrito SÍ se actualiza: las líneas
afectadas se eliminan como parte del mismo rechazo (research.md, Decisión 5).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_cart_qr_visibility -v
"""
from decimal import Decimal
import unittest
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.characterization_tests import cart_fixtures as fx
from app.api.v1.cart import service
from app.api.v1.cart.schemas import CartItemIn
from app.models.cart import Cart
from app.models.customer_order import CustomerOrder


class CartQrVisibilityTests(unittest.TestCase):
    def _seed_session(self):
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        participant = fx.make_participant(db, table_session=ts)
        return db, participant

    def _seed_variant(self, db, **kw):
        active = kw.pop("category_active", True)
        visible_en_qr = kw.pop("visible_en_qr", True)
        category = fx.make_category(db, active=active, visible_en_qr=visible_en_qr)
        product = fx.make_product(db, category=category)
        kw.setdefault("price", Decimal("8000"))
        return fx.make_variant(db, product=product, **kw), product, category

    def _efectivo(self, db):
        return fx.make_payment_method(db, name=f"efectivo-{uuid4()}", is_cash=True)

    def test_submit_cart_con_categoria_oculta_en_qr_rechaza_y_limpia_el_carrito(self):
        """Escenario 6 de quickstart.md: el comensal agregó el producto
        mientras su categoría estaba visible; el administrador la oculta
        antes de que confirme -- se rechaza el envío, nombrando el
        producto, sin crear ninguna orden, y la línea se quita del
        carrito."""
        db, participant = self._seed_session()
        variant, product, category = self._seed_variant(db, visible_en_qr=True)
        efectivo = self._efectivo(db)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))

        category.visible_en_qr = False
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.submit_cart(db, participant, efectivo.id)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn(product.name, ctx.exception.detail["productos_no_disponibles"])
        # spec.md (Escenario 3) exige el texto literal, sin nombres
        # interpolados -- a diferencia de agotados (spec 093), que sí los
        # menciona en el mensaje.
        self.assertEqual(
            ctx.exception.detail["error"],
            "Algunos productos ya no están disponibles y se quitaron de tu carrito",
        )

        # No se creó ninguna orden.
        self.assertIsNone(db.execute(select(CustomerOrder)).scalar_one_or_none())
        # A diferencia de agotados: el carrito SÍ se actualiza -- la línea ya no existe.
        cart = db.execute(
            select(Cart).where(Cart.participant_id == participant.id, Cart.status == "abierto")
        ).scalar_one()
        self.assertEqual(len(cart.items), 0)

    def test_submit_cart_con_categoria_desactivada_tambien_rechaza(self):
        """`active=False` por sí solo ya quita la categoría del criterio de
        FR-002, sin importar `visible_en_qr`."""
        db, participant = self._seed_session()
        variant, product, category = self._seed_variant(db, visible_en_qr=True)
        efectivo = self._efectivo(db)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))

        category.active = False
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.submit_cart(db, participant, efectivo.id)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn(product.name, ctx.exception.detail["productos_no_disponibles"])

    def test_submit_cart_resto_del_carrito_se_conserva(self):
        """El producto de la categoría que SIGUE visible se conserva en el
        carrito, no solo se elimina el afectado."""
        db, participant = self._seed_session()
        v1, p1, c1 = self._seed_variant(db, visible_en_qr=True)
        v2, p2, c2 = self._seed_variant(db, visible_en_qr=True)
        efectivo = self._efectivo(db)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=v1.id, quantity=1))
        service.add_item(db, participant.id, CartItemIn(product_variant_id=v2.id, quantity=1))

        c1.visible_en_qr = False
        db.commit()

        with self.assertRaises(HTTPException):
            service.submit_cart(db, participant, efectivo.id)

        cart = db.execute(
            select(Cart).where(Cart.participant_id == participant.id, Cart.status == "abierto")
        ).scalar_one()
        self.assertEqual(len(cart.items), 1)
        self.assertEqual(cart.items[0].product_variant_id, v2.id)

    def test_submit_cart_con_categorias_visibles_sigue_funcionando(self):
        db, participant = self._seed_session()
        variant, _, _ = self._seed_variant(db, visible_en_qr=True)
        efectivo = self._efectivo(db)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))

        order = service.submit_cart(db, participant, efectivo.id)

        self.assertEqual(order.status, "recibida")


if __name__ == "__main__":
    unittest.main()
