"""Spec 135 (A-146): revierte A-124/FR-005 de spec 112 -- un pago mixto ya no
admite repetir el mismo `payment_method_id` en dos líneas. Cubre las 3 rutas
que confluyen en `build_sale` (research.md D1): `PayIn.payments`
(`checkout.pay_order`), `CheckoutAndSendIn.payments`
(`checkout.checkout_and_send`) y `SaleCreate.payments`
(`sales.service.checkout`) -- las 3 rechazan con 422 y 0 filas `Payment`
creadas cuando dos líneas repiten método.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_sales_duplicate_payment_method -v
"""
from decimal import Decimal
import unittest

from fastapi import HTTPException

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import checkout
from app.api.v1.orders.schemas import CheckoutAndSendIn, PayIn
from app.api.v1.sales import service
from app.api.v1.sales.schemas import PaymentIn, SaleCreate, SaleItemIn
from app.models.payment import Payment

PRECIO = Decimal("10000")


class TestSalesDuplicatePaymentMethod(unittest.TestCase):
    def _seed_hold_order(self):
        db = fx.new_session()
        table = fx.make_dining_table(db, status="ocupada")
        ts = fx.make_table_session(db, table=table)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        order = fx.make_customer_order(db, ts, status="recibida", channel="POS")
        fx.make_order_item(db, order, variant, quantity=1)
        register = fx.make_cash_register(db)
        shift = fx.make_cash_shift(db, register=register)
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()
        cashier = fx.make_user_double()
        return db, order, shift, nequi, cashier

    def test_pay_order_rechaza_metodo_repetido(self):
        db, order, shift, nequi, cashier = self._seed_hold_order()
        order.status = "bloqueada"
        db.commit()
        data = PayIn(
            cash_shift_id=shift.id,
            payments=[
                PaymentIn(payment_method_id=nequi.id, amount=Decimal("4000")),
                PaymentIn(payment_method_id=nequi.id, amount=Decimal("6000")),
            ],
        )

        with self.assertRaises(HTTPException) as ctx:
            checkout.pay_order(db, order.id, data, cashier)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("Nequi", ctx.exception.detail)

        self.assertEqual(db.query(Payment).count(), 0)
        db.refresh(order)
        self.assertNotEqual(order.status, "pagada")

    def test_checkout_and_send_rechaza_metodo_repetido(self):
        db, order, shift, nequi, cashier = self._seed_hold_order()
        data = CheckoutAndSendIn(
            version=order.version, cash_shift_id=shift.id,
            payments=[
                PaymentIn(payment_method_id=nequi.id, amount=Decimal("4000")),
                PaymentIn(payment_method_id=nequi.id, amount=Decimal("6000")),
            ],
        )

        with self.assertRaises(HTTPException) as ctx:
            checkout.checkout_and_send(db, order.id, data, cashier)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("Nequi", ctx.exception.detail)

        self.assertEqual(db.query(Payment).count(), 0)
        db.refresh(order)
        self.assertNotEqual(order.status, "pagada")

    def test_sale_create_rechaza_metodo_repetido(self):
        db = fx.new_session()
        variant = fx.make_variant(db, price=PRECIO)
        shift = fx.make_cash_shift(db)
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()
        cashier = fx.make_user_double()

        data = SaleCreate(
            cash_shift_id=shift.id,
            items=[SaleItemIn(product_variant_id=variant.id, quantity=1)],
            payments=[
                PaymentIn(payment_method_id=nequi.id, amount=Decimal("4000")),
                PaymentIn(payment_method_id=nequi.id, amount=Decimal("6000")),
            ],
        )

        with self.assertRaises(HTTPException) as ctx:
            service.checkout(db, data, cashier)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("Nequi", ctx.exception.detail)

        self.assertEqual(db.query(Payment).count(), 0)


if __name__ == "__main__":
    unittest.main()
