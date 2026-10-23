"""Tests de la nueva funcionalidad — spec 132, Polish (FR-021, SC-006): los
endpoints nuevos/ajustados de pagos (`approve`, `reject`, `confirm-cash`,
`adjust-split`, `GET /orders/{order_id}/payment-events`) rechazan el acceso
cuando el pedido pertenece a otro tenant.

No hay código de aislamiento que escribir aquí: lo da el `search_path` por
esquema de tenant (research.md de spec 024/132) — cada sesión de esta suite
(`fx.new_session()`) es un SQLite en memoria independiente, el equivalente
verificable sobre esta suite de un segundo tenant (mismo patrón que
`test_orders_billing_summary.py::TestAislamientoPorTenant`, FR-021 original).
Lo que estos tests congelan es que el intento/pedido de la sesión A es
indistinguible de "no existe" para la sesión B: `404` sin revelar nada.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_payment_attempt_tenant_isolation -v
"""
from decimal import Decimal
from uuid import uuid4
import unittest

from fastapi import HTTPException

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import checkout

PRECIO = Decimal("18000")


class TestPaymentAttemptTenantIsolation(unittest.TestCase):
    def _seed_order_con_intento_pendiente(self):
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        participant = fx.make_participant(db, table_session=ts)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        order = fx.make_customer_order(db, ts, participant=participant, status="recibida")
        fx.make_order_item(db, order, variant, estado_cocina="pendiente")
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        attempt = fx.make_payment_attempt(db, order, nequi, status="pendiente")
        db.commit()
        # Ids simples, no los objetos ORM: la sesión `db` (tenant A) no sigue
        # viva para la sesión `db_b` (tenant B) de cada test.
        return order.id, attempt.id

    def _user(self):
        return fx.make_user_double()

    def _shift(self, db):
        return fx.make_cash_shift(db)

    def test_approve_de_otro_tenant_responde_404(self):
        _, attempt_id = self._seed_order_con_intento_pendiente()
        db_b = fx.new_session()
        db_b.commit()

        with self.assertRaises(HTTPException) as ctx:
            checkout.approve_payment_attempt(db_b, attempt_id, self._shift(db_b).id, self._user())
        self.assertEqual(ctx.exception.status_code, 404)

    def test_reject_de_otro_tenant_responde_404(self):
        _, attempt_id = self._seed_order_con_intento_pendiente()
        db_b = fx.new_session()
        db_b.commit()

        with self.assertRaises(HTTPException) as ctx:
            checkout.reject_payment_attempt(db_b, attempt_id, "monto_no_coincide", self._user())
        self.assertEqual(ctx.exception.status_code, 404)

    def test_confirm_cash_de_otro_tenant_responde_404(self):
        _, attempt_id = self._seed_order_con_intento_pendiente()
        db_b = fx.new_session()
        db_b.commit()

        with self.assertRaises(HTTPException) as ctx:
            checkout.confirm_cash_payment_attempt(
                db_b, attempt_id, Decimal("20000"), self._shift(db_b).id, self._user(),
            )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_adjust_split_de_otro_tenant_responde_404(self):
        _, attempt_id = self._seed_order_con_intento_pendiente()
        db_b = fx.new_session()
        db_b.commit()

        with self.assertRaises(HTTPException) as ctx:
            checkout.adjust_split_payment_attempt(
                db_b, attempt_id, Decimal("10000"), Decimal("8000"), None, None, self._user(),
            )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_payment_events_de_otro_tenant_responde_404_sin_revelar_nada(self):
        order_id, _ = self._seed_order_con_intento_pendiente()
        db_b = fx.new_session()
        db_b.commit()

        with self.assertRaises(HTTPException) as ajeno:
            checkout.list_payment_events(db_b, order_id)
        with self.assertRaises(HTTPException) as inexistente:
            checkout.list_payment_events(db_b, uuid4())

        self.assertEqual(ajeno.exception.status_code, 404)
        self.assertEqual(ajeno.exception.status_code, inexistente.exception.status_code)
        self.assertEqual(ajeno.exception.detail, inexistente.exception.detail)
        self.assertNotIn(str(order_id), str(ajeno.exception.detail))


if __name__ == "__main__":
    unittest.main()
