"""Tests de la nueva funcionalidad — spec 132, User Story 5 (FR-017/FR-018):
las tres salidas de resolución tras un rechazo de transferencia -- reintento
único, y cancelación por impago con motivo distintivo para el reporte de
mermas. (La salida "efectivo total" ya queda cubierta por el flujo existente
de `create_payment_attempt`/`confirm_cash_payment_attempt` sobre un método en
efectivo, sin cambios de código — ver T041 en tasks.md.)

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_rejection_resolution -v
"""
from decimal import Decimal
import unittest

from fastapi import HTTPException
from sqlalchemy import select

from app.characterization_tests import cart_fixtures as cfx
from app.characterization_tests import orders_fixtures as ofx
from app.api.v1.cart import service as cart_service
from app.api.v1.orders import checkout
from app.models.audit_log import AuditLog
from app.models.order_payment_attempt import OrderPaymentAttempt


class TestRetryLimit(unittest.TestCase):
    """research.md Decisión 5 (FR-017, spec.md edge case línea 108): un único
    reintento de transferencia tras un rechazo."""

    def _seed_order(self):
        db = cfx.new_session()
        table = cfx.make_dining_table(db)
        ts = cfx.make_table_session(db, table=table)
        participant = cfx.make_participant(db, table_session=ts)
        order = cfx.make_customer_order(db, participant, status="recibida")
        nequi = cfx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()
        return db, participant, order, nequi

    def test_primer_reintento_tras_un_rechazo_es_admitido(self):
        db, participant, order, nequi = self._seed_order()
        first = cart_service.create_payment_attempt(db, participant.id, order.id, nequi.id)
        first.status = "rechazado"
        first.rejection_reason = "monto_no_coincide"
        db.commit()

        second = cart_service.create_payment_attempt(db, participant.id, order.id, nequi.id)
        self.assertEqual(second.status, "pendiente")

    def test_segundo_reintento_tras_dos_rechazos_responde_409(self):
        """spec.md edge case: "El flujo definido solo contempla un
        reintento; si vuelve a fallar, el cajero continúa con la salida de
        cancelación por impago." -- un tercer intento de transferencia no se
        admite."""
        db, participant, order, nequi = self._seed_order()
        first = cart_service.create_payment_attempt(db, participant.id, order.id, nequi.id)
        first.status = "rechazado"
        first.rejection_reason = "monto_no_coincide"
        db.commit()

        second = cart_service.create_payment_attempt(db, participant.id, order.id, nequi.id)
        second.status = "rechazado"
        second.rejection_reason = "transferencia_no_completada"
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            cart_service.create_payment_attempt(db, participant.id, order.id, nequi.id)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_reintento_tras_rechazo_no_limita_elegir_efectivo(self):
        """El límite de un único reintento solo aplica a transferencia
        (FR-017) -- pagar en efectivo tras un rechazo sigue disponible
        incluso después de agotar el reintento de transferencia."""
        db, participant, order, nequi = self._seed_order()
        efectivo = cfx.make_payment_method(db, name="Efectivo", is_cash=True)
        db.commit()
        first = cart_service.create_payment_attempt(db, participant.id, order.id, nequi.id)
        first.status = "rechazado"
        first.rejection_reason = "monto_no_coincide"
        db.commit()
        second = cart_service.create_payment_attempt(db, participant.id, order.id, nequi.id)
        second.status = "rechazado"
        second.rejection_reason = "transferencia_no_completada"
        db.commit()

        cash_attempt = cart_service.create_payment_attempt(db, participant.id, order.id, efectivo.id)
        self.assertEqual(cash_attempt.status, "pendiente")


class TestCancelacionPorImpago(unittest.TestCase):
    """research.md Decisión 6 (FR-018): reutiliza `cancel_order` con un
    motivo distintivo para el reporte de mermas."""

    def test_cancelar_pedido_con_intento_rechazado_anula_el_pago_y_registra_merma(self):
        db = ofx.new_session()
        table = ofx.make_dining_table(db)
        ts = ofx.make_table_session(db, table=table)
        participant = ofx.make_participant(db, table_session=ts)
        category = ofx.make_category(db)
        product = ofx.make_product(db, category=category)
        variant = ofx.make_variant(db, product=product, price=Decimal("18000"))
        order = ofx.make_customer_order(db, ts, participant=participant, status="abierta")
        ofx.make_order_item(db, order, variant, estado_cocina="en_preparacion")
        nequi = ofx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        ofx.make_payment_attempt(
            db, order, nequi, status="rechazado", rejection_reason="monto_no_coincide",
        )
        db.commit()
        user = ofx.make_user_double()

        from app.api.v1.orders.schemas import CancelIn
        checkout.cancel_order(
            db, order.id, CancelIn(motivo="impago_transferencia_rechazada"), user,
        )

        db.refresh(order)
        self.assertEqual(order.status, "cancelada")

        loss_log = db.execute(
            select(AuditLog).where(AuditLog.action == "order.cancel.loss")
        ).scalar_one()
        self.assertEqual(loss_log.payload["motivo"], "impago_transferencia_rechazada")


if __name__ == "__main__":
    unittest.main()
