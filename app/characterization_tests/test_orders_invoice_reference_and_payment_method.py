"""Referencia de factura real y método de pago reales en `GET /orders/{id}`
(spec 120, corrección de FR-012; `service.invoice_and_payment_info`).

Reportado por el negocio: la "Referencia" del detalle mostraba un código
sintético derivado del `id` de la orden que no coincidía con la factura
registrada en el sistema, y el "Método de pago" quedaba siempre vacío para
órdenes cobradas por mostrador (`pay_order`/`checkout_and_send`, que nunca
crean un `OrderPaymentAttempt` -- solo el camino QR lo hace).

Invoca `router.get_order` directamente, igual que `test_orders_billing_summary.py`.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_orders_invoice_reference_and_payment_method -v
"""
import unittest
from decimal import Decimal

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import router as orders_router
from app.models.invoice import Invoice
from app.models.payment import Payment


def _get_order(db, order_id):
    return orders_router.get_order(order_id=order_id, db=db, _=fx.make_user_double())


class _Base(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.ts = fx.make_table_session(self.db)

    def _invoice(self, sale, **kw):
        kw.setdefault("id", fx._uid())
        kw.setdefault("sale_id", sale.id)
        kw.setdefault("number", 4)
        kw.setdefault("subtotal", sale.subtotal)
        kw.setdefault("discount", sale.discount)
        kw.setdefault("tax", Decimal("0"))
        kw.setdefault("tip", Decimal("0"))
        kw.setdefault("total", sale.total)
        kw.setdefault("applied_promotions", [])
        kw.setdefault("status", "issued")
        kw.setdefault("user_id", sale.user_id)
        obj = Invoice(**kw)
        self.db.add(obj)
        self.db.flush()
        return obj

    def _payment(self, sale, method, **kw):
        kw.setdefault("id", fx._uid())
        kw.setdefault("sale_id", sale.id)
        kw.setdefault("payment_method_id", method.id)
        kw.setdefault("amount", sale.total or Decimal("1"))
        obj = Payment(**kw)
        self.db.add(obj)
        self.db.flush()
        return obj


class TestReferenciaDeFactura(_Base):
    def test_con_venta_e_invoice_la_referencia_es_prefijo_mas_numero_relleno_a_6(self):
        order = fx.make_customer_order(self.db, self.ts, status="pagada")
        sale = fx.make_sale(self.db, order=order, total=Decimal("16000"))
        self._invoice(sale, prefix="A-", number=4)
        self.db.commit()

        self.assertEqual(_get_order(self.db, order.id).invoice_reference, "A-000004")

    def test_sin_venta_todavia_la_referencia_es_none_no_un_codigo_inventado(self):
        order = fx.make_customer_order(self.db, self.ts, status="abierta")
        self.db.commit()

        self.assertIsNone(_get_order(self.db, order.id).invoice_reference)

    def test_con_venta_pero_sin_invoice_la_referencia_es_none(self):
        # Caso defensivo: una Sale sin Invoice asociada (no debería ocurrir en
        # el flujo normal, pero D15/RN-005 prohíben inventar un valor igual.
        order = fx.make_customer_order(self.db, self.ts, status="pagada")
        fx.make_sale(self.db, order=order, total=Decimal("16000"))
        self.db.commit()

        self.assertIsNone(_get_order(self.db, order.id).invoice_reference)


class TestMetodoDePago(_Base):
    def test_con_pago_de_mostrador_sin_intento_de_pago_qr_muestra_el_metodo_real(self):
        # El bug reportado: pay_order/checkout_and_send no crean
        # OrderPaymentAttempt, así que current_payment_attempt queda vacío
        # aunque la orden ya esté pagada con un método conocido.
        order = fx.make_customer_order(self.db, self.ts, status="pagada")
        sale = fx.make_sale(self.db, order=order, total=Decimal("16000"))
        nequi = fx.make_payment_method(self.db, name="Nequi", is_cash=False)
        self._payment(sale, nequi)
        self.db.commit()

        result = _get_order(self.db, order.id)
        self.assertEqual(result.payment_method_name, "Nequi")
        self.assertIsNone(result.current_payment_attempt)

    def test_sin_venta_todavia_el_metodo_de_pago_es_none(self):
        order = fx.make_customer_order(self.db, self.ts, status="abierta")
        self.db.commit()

        self.assertIsNone(_get_order(self.db, order.id).payment_method_name)


if __name__ == "__main__":
    unittest.main()
