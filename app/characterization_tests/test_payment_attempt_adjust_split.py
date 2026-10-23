"""Tests de la nueva funcionalidad — spec 132, User Story 4 (FR-013/FR-014):
reajuste de la división efectivo/transferencia de un intento `pendiente` al
confirmar, incluyendo convertir transferencia total <-> mixto <-> efectivo
total, con auditoría de los valores antes/después.

No son characterization tests: `adjust-split` es un endpoint nuevo
(research.md Decisión 4) — se verifica contra
`contracts/payment-attempts-api.md`, no contra comportamiento heredado.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_payment_attempt_adjust_split -v
"""
from decimal import Decimal
import unittest

from fastapi import HTTPException
from sqlalchemy import select

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import checkout
from app.models.audit_log import AuditLog

PRECIO = Decimal("18000")


class TestPaymentAttemptAdjustSplit(unittest.TestCase):
    def _seed_order(self, *, total=PRECIO):
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        participant = fx.make_participant(db, table_session=ts)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=total)
        order = fx.make_customer_order(db, ts, participant=participant, status="recibida")
        fx.make_order_item(db, order, variant, estado_cocina="pendiente")
        db.commit()
        return db, order

    def _user(self):
        return fx.make_user_double()

    def test_convierte_transferencia_total_en_mixto(self):
        """FR-013, US4 Acceptance Scenario 1: transferencia total ($18.000) se
        reajusta a $8.000 efectivo / $10.000 transferencia."""
        db, order = self._seed_order()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        attempt = fx.make_payment_attempt(db, order, nequi, status="pendiente")
        db.commit()

        result = checkout.adjust_split_payment_attempt(
            db, attempt.id, Decimal("8000"), Decimal("10000"), nequi.id, None, self._user(),
        )
        self.assertEqual(result.cash_amount, Decimal("8000"))
        self.assertEqual(result.transfer_amount, Decimal("10000"))
        self.assertEqual(result.status, "pendiente")

    def test_409_si_la_suma_no_coincide_con_el_total(self):
        """FR-013, US4 Acceptance Scenario 3."""
        db, order = self._seed_order()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        attempt = fx.make_payment_attempt(db, order, nequi, status="pendiente")
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            checkout.adjust_split_payment_attempt(
                db, attempt.id, Decimal("8000"), Decimal("9000"), nequi.id, None, self._user(),
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_409_si_el_intento_no_esta_pendiente(self):
        db, order = self._seed_order()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        attempt = fx.make_payment_attempt(db, order, nequi, status="confirmado")
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            checkout.adjust_split_payment_attempt(
                db, attempt.id, Decimal("8000"), Decimal("10000"), nequi.id, None, self._user(),
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_transfer_amount_cero_limpia_los_campos_mixtos_para_efectivo_total(self):
        """Contrato: `transfer_amount == 0` equivale a efectivo total — los
        campos mixtos vuelven a `None` (mismo estado que un intento de
        efectivo puro desde que se crea, data-model.md)."""
        db, order = self._seed_order()
        mixto = fx.make_payment_method(db, name="Pago mixto", is_cash=False, type="mixed")
        attempt = fx.make_payment_attempt(
            db, order, mixto, status="pendiente",
            cash_amount=Decimal("10000"), transfer_amount=Decimal("8000"),
        )
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        db.commit()

        result = checkout.adjust_split_payment_attempt(
            db, attempt.id, PRECIO, Decimal("0"), None, efectivo.id, self._user(),
        )
        self.assertIsNone(result.cash_amount)
        self.assertIsNone(result.transfer_amount)
        self.assertIsNone(result.transfer_method_id)
        self.assertEqual(result.payment_method_id, efectivo.id)
        self.assertFalse(result.is_cash is False)  # ya es efectivo

    def test_cash_amount_cero_limpia_los_campos_mixtos_para_transferencia_total(self):
        """Contrato: `cash_amount == 0` equivale a transferencia total."""
        db, order = self._seed_order()
        mixto = fx.make_payment_method(db, name="Pago mixto", is_cash=False, type="mixed")
        attempt = fx.make_payment_attempt(
            db, order, mixto, status="pendiente",
            cash_amount=Decimal("10000"), transfer_amount=Decimal("8000"),
        )
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()

        result = checkout.adjust_split_payment_attempt(
            db, attempt.id, Decimal("0"), PRECIO, nequi.id, None, self._user(),
        )
        self.assertIsNone(result.cash_amount)
        self.assertIsNone(result.transfer_amount)
        self.assertEqual(result.payment_method_id, nequi.id)

    def test_escribe_auditlog_con_valores_antes_y_despues(self):
        """FR-014, US4 Acceptance Scenario 2."""
        db, order = self._seed_order()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        attempt = fx.make_payment_attempt(db, order, nequi, status="pendiente")
        db.commit()
        user = self._user()

        checkout.adjust_split_payment_attempt(
            db, attempt.id, Decimal("8000"), Decimal("10000"), nequi.id, None, user,
        )

        log = db.execute(
            select(AuditLog).where(AuditLog.action == "payment_attempt.split_adjusted")
        ).scalar_one()
        self.assertEqual(log.entity, "order_payment_attempts")
        self.assertEqual(log.entity_id, attempt.id)
        self.assertIsNone(log.payload["cash_amount_antes"])
        self.assertIsNone(log.payload["transfer_amount_antes"])
        self.assertEqual(Decimal(str(log.payload["cash_amount_despues"])), Decimal("8000"))
        self.assertEqual(Decimal(str(log.payload["transfer_amount_despues"])), Decimal("10000"))


if __name__ == "__main__":
    unittest.main()
