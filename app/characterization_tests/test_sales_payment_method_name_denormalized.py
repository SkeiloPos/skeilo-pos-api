"""`GET /sales/{id}` (y el listado paginado `GET /sales`) ahora devuelven
`payment_method_name`/`is_cash` en cada línea de `payments`, resueltos desde
`Payment.payment_method` (mismo patrón que `OrderPaymentAttempt.payment_method_name`,
`app/models/order_payment_attempt.py`). Antes el frontend tenía que pedir el
catálogo completo de métodos de pago aparte (`GET /sales/payment-methods`, sin
filtro) solo para resolver ese nombre en el recibo — este cambio elimina esa
segunda llamada.

No son characterization tests de "comportamiento heredado": el campo es nuevo.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_sales_payment_method_name_denormalized -v
"""
from decimal import Decimal
import unittest

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.sales import router as sales_router, service
from app.api.v1.sales.schemas import PaymentIn, SaleCreate, SaleItemIn
from app.api.v1.sales.schemas import PaymentMethodUpdate

PRECIO = Decimal("10000")


class TestSalesPaymentMethodNameDenormalized(unittest.TestCase):
    def _seed_and_checkout(self, method_kwargs: dict):
        db = fx.new_session()
        # `tracks_inventory=False`: a este test no le importa el descuento de
        # inventario, solo la serialización de `payments` -- sin esto,
        # `service.checkout` exige una receta configurada (spec 027).
        product = fx.make_product(db, tracks_inventory=False)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        shift = fx.make_cash_shift(db)
        method = fx.make_payment_method(db, **method_kwargs)
        db.commit()
        cashier = fx.make_user_double()

        data = SaleCreate(
            cash_shift_id=shift.id,
            items=[SaleItemIn(product_variant_id=variant.id, quantity=1)],
            payments=[PaymentIn(payment_method_id=method.id, amount=PRECIO)],
        )
        sale = service.checkout(db, data, cashier)
        return db, sale, method

    def test_get_sale_incluye_nombre_e_is_cash_del_metodo(self):
        db, sale, _method = self._seed_and_checkout(
            {"name": "Nequi", "is_cash": False, "type": "transfer"}
        )

        fetched = sales_router.get_sale(sale.id, db, None)

        self.assertEqual(len(fetched.payments), 1)
        payment = fetched.payments[0]
        self.assertEqual(payment.payment_method_name, "Nequi")
        self.assertFalse(payment.is_cash)

    def test_desactivar_el_metodo_despues_de_la_venta_no_borra_el_nombre_historico(self):
        """FR-003/Principio VII: desactivar un método de pago no debe dejar
        sin nombre un pago ya hecho con él -- antes el frontend resolvía esto
        contra la lista completa (que sí incluye inactivos); ahora lo trae el
        propio `GET /sales/{id}`."""
        db, sale, method = self._seed_and_checkout(
            {"name": "Nequi", "is_cash": False, "type": "transfer"}
        )
        # Un segundo método activo para no chocar con la regla "al menos uno
        # activo" -- lo que este test cubre es la desactivación en sí, no esa
        # regla (ya probada en `test_sales_payment_methods.py`).
        fx.make_payment_method(db, name="Efectivo", is_cash=True)
        service.update_payment_method(db, method.id, PaymentMethodUpdate(active=False))
        db.commit()

        fetched = sales_router.get_sale(sale.id, db, None)

        self.assertEqual(fetched.payments[0].payment_method_name, "Nequi")

    def test_is_cash_true_para_un_pago_en_efectivo(self):
        db, sale, _method = self._seed_and_checkout({"name": "Efectivo", "is_cash": True})

        fetched = sales_router.get_sale(sale.id, db, None)

        self.assertTrue(fetched.payments[0].is_cash)


if __name__ == "__main__":
    unittest.main()
