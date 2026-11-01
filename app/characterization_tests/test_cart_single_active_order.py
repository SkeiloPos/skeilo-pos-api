"""Tests de la nueva funcionalidad — spec 024-pagos-ordenes-mesa, User Story
6: un participante solo puede tener una orden activa a la vez (FR-005/FR-006).

No es un characterization test: `submit_cart` no valida esto hoy (research.md
spec 024, Decisión 8) — es una restricción nueva.

Ampliado por spec 025-revision-pago-antes-envio (T011): `submit_cart` ahora
exige `payment_method_id` (el pedido nace con su primer intento de pago
adjunto) y el edge case de confirmación duplicada (FR-013, SC-006) — doble
toque/reintento de red sobre la misma sesión, que solo debe dejar **una**
`CustomerOrder`.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_cart_single_active_order -v
"""
from decimal import Decimal
import unittest

from fastapi import HTTPException
from sqlalchemy import func, select

from app.characterization_tests import cart_fixtures as fx
from app.api.v1.cart import service
from app.models.customer_order import CustomerOrder


class TestCartSingleActiveOrder(unittest.TestCase):
    def _seed_session_con_carrito(self):
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        participant = fx.make_participant(db, table_session=ts)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=Decimal("8000"))
        cart = fx.make_cart(db, participant=participant)
        fx.make_cart_item(db, cart, variant)
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        db.commit()
        return db, participant, variant, efectivo

    def test_segunda_orden_con_la_primera_pendiente_falla_409(self):
        """Acceptance Scenario 1 (US6)."""
        db, participant, variant, efectivo = self._seed_session_con_carrito()
        first = service.submit_cart(db, participant, efectivo.id)
        self.assertEqual(first.status, "pendiente")

        # Nuevo carrito abierto tras el submit (mismo patrón que
        # `_get_or_create_open_cart`); se le agrega un ítem para poder
        # reintentar el envío.
        cart2 = fx.make_cart(db, participant=participant)
        fx.make_cart_item(db, cart2, variant)
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.submit_cart(db, participant, efectivo.id)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_submit_cart_crea_la_orden_con_order_type_dine_in(self):
        """spec 055, research.md D4: el flujo QR es inherentemente de mesa —
        `submit_cart` siempre fija `order_type='DINE_IN'` (y `channel=
        'QR_MENU'`). Sin esto, todo pedido nuevo por QR quedaría con
        `order_type` vacío (hallazgo G1 del análisis de spec 055)."""
        db, participant, variant, efectivo = self._seed_session_con_carrito()
        order = service.submit_cart(db, participant, efectivo.id)

        self.assertEqual(order.channel, "QR_MENU")
        self.assertEqual(order.order_type, "DINE_IN")

    def test_segunda_orden_tras_finalizar_la_primera_se_permite(self):
        """Acceptance Scenario 2 (US6): 'finalizada' = pagada o cancelada
        (research.md spec 024, Decisión 8)."""
        db, participant, variant, efectivo = self._seed_session_con_carrito()
        first = service.submit_cart(db, participant, efectivo.id)
        first.status = "pagada"
        db.commit()

        cart2 = fx.make_cart(db, participant=participant)
        fx.make_cart_item(db, cart2, variant)
        db.commit()

        second = service.submit_cart(db, participant, efectivo.id)
        self.assertNotEqual(second.id, first.id)
        self.assertEqual(second.status, "pendiente")

    def test_segunda_orden_tras_cancelar_la_primera_se_permite(self):
        db, participant, variant, efectivo = self._seed_session_con_carrito()
        first = service.submit_cart(db, participant, efectivo.id)
        first.status = "cancelada"
        db.commit()

        cart2 = fx.make_cart(db, participant=participant)
        fx.make_cart_item(db, cart2, variant)
        db.commit()

        second = service.submit_cart(db, participant, efectivo.id)
        self.assertEqual(second.status, "pendiente")

    # ------------------------------------------- Confirmación duplicada (T011)

    def test_confirmacion_duplicada_misma_sesion_falla_409_una_sola_orden(self):
        """FR-013, SC-006, edge case "confirmación duplicada": doble
        toque/reintento de red — dos llamadas seguidas de `submit_cart` sobre
        la misma sesión de base de datos, cada una sobre su propio carrito no
        vacío (un doble toque real reenvía el mismo formulario dos veces; el
        carrito del comensal no cambia entre ambos toques, pero
        `_load_open_cart` ya cerró el primero en 'confirmado' — se siembra un
        segundo con los mismos ítems para aislar la validación que este test
        cubre, la de "orden activa", de la de "carrito abierto"). La segunda
        falla 409 (chequeo de aplicación, mismo camino que el resto de este
        módulo) y al final solo existe **una** `CustomerOrder` para el
        comensal — la garantía última la da
        `idx_active_order_per_participant` (data-model.md/research.md
        Decisión 4), pero aquí basta el chequeo de aplicación porque ambas
        llamadas comparten sesión."""
        db, participant, variant, efectivo = self._seed_session_con_carrito()

        service.submit_cart(db, participant, efectivo.id)

        cart2 = fx.make_cart(db, participant=participant)
        fx.make_cart_item(db, cart2, variant)
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.submit_cart(db, participant, efectivo.id)
        self.assertEqual(ctx.exception.status_code, 409)

        count = db.execute(
            select(func.count(CustomerOrder.id)).where(
                CustomerOrder.participant_id == participant.id
            )
        ).scalar_one()
        self.assertEqual(count, 1)

    # ---------------------------- Mezcla de orígenes QR/mostrador (spec 028, T015)

    def test_submit_cart_bloqueado_por_comanda_manual_activa_en_la_mesa_409(self):
        """spec 028, FR-013 (T015): simetría inversa de
        `orders.service.create_order` (T014) — una mesa no mezcla orígenes de
        pedido a la vez. Si el staff ya abrió una comanda de mostrador/mesero
        activa (`channel='POS'`, status no terminal) en la sesión, un
        pedido nuevo por QR no puede coexistir con ella. No es un
        characterization test: `submit_cart` no valida esto hoy — es una
        restricción nueva."""
        db, participant, variant, efectivo = self._seed_session_con_carrito()
        fx.make_customer_order(
            db, participant, participant_id=None, channel="POS", status="abierta",
        )
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.submit_cart(db, participant, efectivo.id)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_submit_cart_permite_qr_si_la_comanda_manual_ya_es_terminal(self):
        """Contraste del test anterior: una comanda de mostrador/mesero
        'pagada' o 'cancelada' ya no es 'activa', así que no bloquea un
        pedido nuevo por QR en la misma mesa."""
        db, participant, variant, efectivo = self._seed_session_con_carrito()
        fx.make_customer_order(
            db, participant, participant_id=None, channel="POS", status="pagada",
        )
        db.commit()

        order = service.submit_cart(db, participant, efectivo.id)
        self.assertEqual(order.status, "pendiente")


if __name__ == "__main__":
    unittest.main()
