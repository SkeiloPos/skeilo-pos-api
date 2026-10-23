"""Tests de la nueva funcionalidad — spec 132, User Story 5 (FR-015/FR-020):
motivo de rechazo del conjunto cerrado (validado en el límite de la API vía
`PaymentAttemptRejectIn.reason: RejectionReason`) y marca de revisión en el
segundo rechazo del día para la misma mesa.

No son characterization tests: el conjunto cerrado y la marca de "segundo
rechazo" son comportamiento nuevo de spec 132 (research.md Decisiones 2 y 7).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_payment_attempt_reject -v
"""
from decimal import Decimal
import unittest

from pydantic import ValidationError
from sqlalchemy import select

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import checkout
from app.api.v1.orders.schemas import PaymentAttemptRejectIn
from app.models.audit_log import AuditLog

PRECIO = Decimal("18000")


class TestPaymentAttemptRejectReasonEnum(unittest.TestCase):
    def test_reason_fuera_del_conjunto_cerrado_falla_422_en_el_schema(self):
        """FR-015: el límite de la API rechaza cualquier valor fuera del
        conjunto cerrado -- `PaymentAttemptRejectIn` es un `Enum`/`Literal`,
        así que Pydantic lo invalida antes de llegar al servicio."""
        with self.assertRaises(ValidationError):
            PaymentAttemptRejectIn(reason="el cliente se fue corriendo")

    def test_reason_vacio_falla_422_en_el_schema(self):
        with self.assertRaises(ValidationError):
            PaymentAttemptRejectIn(reason="")

    def test_los_5_valores_del_conjunto_cerrado_son_validos(self):
        for value in (
            "cliente_no_muestra_app", "monto_no_coincide", "transferencia_no_completada",
            "cliente_se_niega_a_mostrar", "comprobante_sospechoso",
        ):
            with self.subTest(reason=value):
                body = PaymentAttemptRejectIn(reason=value)
                self.assertEqual(body.reason.value, value)


class TestTableRepeatRejection(unittest.TestCase):
    def _seed_order_en_sesion(self, db, ts, nequi):
        """Un nuevo pedido + intento pendiente en la MISMA sesión de mesa
        (`table_sessions.dining_table_id` es único: dos pedidos rechazados
        de la misma mesa en el mismo día conviven en la misma sesión activa,
        con comensales distintos) -- los rechazos de spec 132 (FR-020) se
        cuentan sobre `order_payment_attempts` real, en la misma base."""
        participant = fx.make_participant(db, table_session=ts)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        order = fx.make_customer_order(db, ts, participant=participant, status="recibida")
        fx.make_order_item(db, order, variant, estado_cocina="pendiente")
        attempt = fx.make_payment_attempt(db, order, nequi, status="pendiente")
        db.commit()
        return attempt

    def _user(self):
        return fx.make_user_double()

    def test_primer_rechazo_del_dia_no_escribe_marca_de_revision(self):
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()
        attempt = self._seed_order_en_sesion(db, ts, nequi)

        checkout.reject_payment_attempt(db, attempt.id, "monto_no_coincide", self._user())

        count = db.execute(
            select(AuditLog).where(AuditLog.action == "payment.table_repeat_rejection")
        ).scalars().all()
        self.assertEqual(len(count), 0)

    def test_segundo_rechazo_del_dia_en_la_misma_mesa_escribe_marca_de_revision(self):
        """FR-020, US5 Acceptance Scenario 5: el segundo rechazo del día para
        la misma mesa se marca para revisión, sin bloquear el flujo."""
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()

        attempt1 = self._seed_order_en_sesion(db, ts, nequi)
        result1 = checkout.reject_payment_attempt(db, attempt1.id, "monto_no_coincide", self._user())
        self.assertEqual(result1.status, "rechazado")  # no se bloquea

        attempt2 = self._seed_order_en_sesion(db, ts, nequi)
        checkout.reject_payment_attempt(db, attempt2.id, "comprobante_sospechoso", self._user())

        marks = db.execute(
            select(AuditLog).where(AuditLog.action == "payment.table_repeat_rejection")
        ).scalars().all()
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0].entity, "dining_tables")
        self.assertEqual(marks[0].entity_id, table.id)
        self.assertEqual(marks[0].payload["rejection_count_today"], 2)


if __name__ == "__main__":
    unittest.main()
