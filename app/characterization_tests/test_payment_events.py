"""Tests de la nueva funcionalidad — spec 132, User Story 4 (FR-019):
`GET /orders/{order_id}/payment-events`, historial de auditoría consultable
del pedido (`AuditLog`, no el canal solo-Sentry de spec 074 — research.md
Decisión 3).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_payment_events -v
"""
from decimal import Decimal
import unittest

from fastapi import HTTPException

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import checkout

PRECIO = Decimal("18000")


class TestPaymentEvents(unittest.TestCase):
    def _seed_order_recibida(self):
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        participant = fx.make_participant(db, table_session=ts)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        insumo = fx.make_inventory_item(db, current_stock=Decimal("1000"))
        fx.make_recipe_item(db, variant, insumo, quantity=Decimal("1"))
        order = fx.make_customer_order(db, ts, participant=participant, status="recibida")
        fx.make_order_item(db, order, variant, estado_cocina="pendiente")
        db.commit()
        return db, order

    def _user(self):
        return fx.make_user_double()

    def test_lista_vacia_para_un_pedido_sin_eventos(self):
        db, order = self._seed_order_recibida()
        events = checkout.list_payment_events(db, order.id)
        self.assertEqual(events, [])

    def test_incluye_el_evento_de_rechazo_con_motivo_cajero_y_hora(self):
        db, order = self._seed_order_recibida()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        attempt = fx.make_payment_attempt(db, order, nequi, status="pendiente")
        db.commit()
        user = self._user()

        checkout.reject_payment_attempt(db, attempt.id, "monto_no_coincide", user)

        events = checkout.list_payment_events(db, order.id)
        rejected = [e for e in events if e.action == "payment_attempt.rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].entity_id, attempt.id)
        self.assertEqual(rejected[0].payload["motivo"], "monto_no_coincide")
        self.assertEqual(rejected[0].user_name, user.name)
        self.assertIsNotNone(rejected[0].at)

    def test_incluye_el_evento_de_confirmacion_y_el_de_reajuste_ordenados_por_hora(self):
        db, order = self._seed_order_recibida()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        attempt = fx.make_payment_attempt(db, order, nequi, status="pendiente")
        db.commit()
        user = self._user()
        shift = fx.make_cash_shift(db)

        checkout.adjust_split_payment_attempt(
            db, attempt.id, Decimal("8000"), Decimal("10000"), nequi.id, None, user,
        )
        checkout.approve_payment_attempt(db, attempt.id, shift.id, user)

        events = checkout.list_payment_events(db, order.id)
        actions = [e.action for e in events]
        self.assertIn("payment_attempt.split_adjusted", actions)
        self.assertIn("payment_attempt.confirmed", actions)
        # Ordenados por `at` ascendente (contrato): el reajuste ocurrió antes.
        self.assertLess(actions.index("payment_attempt.split_adjusted"), actions.index("payment_attempt.confirmed"))

    def test_404_si_el_pedido_no_existe(self):
        db, _ = self._seed_order_recibida()
        from uuid import uuid4
        with self.assertRaises(HTTPException) as ctx:
            checkout.list_payment_events(db, uuid4())
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
