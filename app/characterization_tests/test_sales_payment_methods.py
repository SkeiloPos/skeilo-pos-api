"""Tests de la nueva funcionalidad — spec 024-pagos-ordenes-mesa, User Story 1:
el tenant configura sus métodos de pago (FR-001/FR-002/FR-003).

No son characterization tests ("CONGELA comportamiento actual"): `payment_info`
y `PATCH /sales/payment-methods/{id}` son comportamiento nuevo (Constitución,
Principio IV/X) — se verifican contra `spec.md`, no contra un comportamiento
heredado.

Editado por spec 032-catalogo-metodos-pago (FR-007/FR-011): ninguno de estos
tests creaba métodos vía `service.create_payment_method`/`router` con `name`
libre — todos siembran filas directamente con `orders_fixtures.make_payment_method`
(un fixture, no la API) y solo ejercitan `service.update_payment_method`
(`PATCH`), que nunca aceptó `name`. Por eso no necesitaron cambios de fondo; el
`name`/`type` libres que antes aceptaba `POST /sales/payment-methods` ya no
existen (`PaymentMethodCreate` ahora exige `catalog_id`) — esa creación nueva,
vía catálogo, se prueba en `test_sales_payment_methods_catalog.py`.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_sales_payment_methods -v
"""
import unittest

from fastapi import HTTPException
from sqlalchemy import select

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.sales import service
from app.api.v1.sales.schemas import PaymentMethodUpdate
from app.models.payment import PaymentMethod


class TestSalesPaymentMethods(unittest.TestCase):
    # ---------------------------------------------------------- Acceptance Scenario 1

    def test_alta_metodo_transferencia_con_payment_info_queda_disponible(self):
        """FR-001/FR-002: un método de transferencia nuevo, con payment_info,
        aparece en la lista del tenant."""
        db = fx.new_session()
        method = fx.make_payment_method(
            db, name="Nequi", is_cash=False, type="transfer",
            payment_info={"cuenta": "3001234567", "titular": "Heladería La 14"},
        )
        db.commit()

        found = db.execute(
            select(PaymentMethod).where(PaymentMethod.id == method.id)
        ).scalar_one()
        self.assertEqual(found.payment_info, {"cuenta": "3001234567", "titular": "Heladería La 14"})
        self.assertTrue(found.active)

    # ---------------------------------------------------------- Acceptance Scenario 2

    def test_desactivar_uno_de_varios_metodos_activos_no_lo_borra(self):
        """Desactivar un método con otros activos: queda inactivo, sigue
        existiendo (no se borra), y su historial de pagos previos no se
        toca."""
        db = fx.new_session()
        fx.make_payment_method(db, name="Efectivo", is_cash=True)
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()

        updated = service.update_payment_method(
            db, nequi.id, PaymentMethodUpdate(active=False)
        )
        self.assertFalse(updated.active)
        self.assertIsNotNone(db.get(PaymentMethod, nequi.id))

    # ---------------------------------------------------------- Acceptance Scenario 3

    def test_desactivar_ultimo_metodo_activo_falla_409(self):
        """FR-003: no se permite dejar el tenant sin ningún método activo."""
        db = fx.new_session()
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.update_payment_method(db, efectivo.id, PaymentMethodUpdate(active=False))
        self.assertEqual(ctx.exception.status_code, 409)

        # No quedó desactivado: la excepción no debe haber mutado el estado.
        self.assertTrue(db.get(PaymentMethod, efectivo.id).active)

    def test_desactivar_ultimo_activo_entre_varios_metodos_inactivos_falla_409(self):
        """El conteo de 'activos restantes' excluye el propio método, no
        cuenta los ya inactivos de antes."""
        db = fx.new_session()
        fx.make_payment_method(db, name="Bancolombia", is_cash=False, type="transfer", active=False)
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer", active=True)
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.update_payment_method(db, nequi.id, PaymentMethodUpdate(active=False))
        self.assertEqual(ctx.exception.status_code, 409)

    # ---------------------------------------------------------- Acceptance Scenario 4

    def test_editar_payment_info_no_altera_intento_de_pago_ya_creado(self):
        """Editar los datos de pago de un método no cambia nada de un
        `OrderPaymentAttempt` ya creado con ese método — el intento solo
        referencia `payment_method_id`, nunca copia estos datos."""
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        participant = fx.make_participant(db, table_session=ts)
        order = fx.make_customer_order(db, ts, participant=participant, status="pendiente")
        nequi = fx.make_payment_method(
            db, name="Nequi", is_cash=False, type="transfer",
            payment_info={"cuenta": "3001234567"},
        )
        attempt = fx.make_payment_attempt(db, order, nequi, status="pendiente")
        db.commit()

        service.update_payment_method(
            db, nequi.id, PaymentMethodUpdate(payment_info={"cuenta": "3009999999"})
        )

        db.refresh(attempt)
        self.assertEqual(attempt.payment_method_id, nequi.id)
        self.assertEqual(attempt.status, "pendiente")


if __name__ == "__main__":
    unittest.main()
