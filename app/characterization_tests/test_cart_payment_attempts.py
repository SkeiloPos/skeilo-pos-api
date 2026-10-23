"""Tests de la nueva funcionalidad — spec 024-pagos-ordenes-mesa, User Stories
2 y 5: el comensal paga por transferencia (comprobante) y reintenta tras un
rechazo (FR-004/FR-008/FR-011/FR-012/FR-015/FR-015a).

No son characterization tests: el flujo de pago del comensal es comportamiento
enteramente nuevo (research.md spec 024 — el `cart` de hoy no tiene ningún paso
de pago). Se verifican contra `spec.md`/`contracts/diner-payment-flow.md`.

Ampliado por spec 025-revision-pago-antes-envio (T008, T010, T018, T019): el
pedido nace junto con su primer intento de pago, en `submit_cart` — dejan de
existir dos llamadas HTTP separadas para lo mismo (crear la orden, y luego su
primer intento). Se verifica contra
`contracts/submit-cart-with-payment.md`/`contracts/payment-receipt-presign.md`.

spec 088 (A-93): el comprobante deja de ser texto libre. `submit_cart` y `attach_receipt`
reciben el esquema del negocio (`tenant_schema="tenant_test"`, el mismo del presign), validan
que el comprobante sea un archivo existente de la carpeta `comprobantes` del propio negocio
(R2 simulado con `object_exists`) y persisten la **key** (no la URL absoluta); lo que estos
tests verifican no cambia.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_cart_payment_attempts -v
"""
from decimal import Decimal
import unittest
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import func, select

from app.characterization_tests import cart_fixtures as fx
from app.characterization_tests import orders_fixtures as ofx
from app.api.v1.cart import service
from app.models.customer_order import CustomerOrder
from app.models.order_payment_attempt import OrderPaymentAttempt


SCHEMA = "tenant_test"
RECEIPT_A = f"{SCHEMA}/comprobantes/a1b2c3d4e5f60718293a4b5c6d7e8f90.jpg"
RECEIPT_B = f"{SCHEMA}/comprobantes/b1b2c3d4e5f60718293a4b5c6d7e8f90.jpg"


class TestCartPaymentAttempts(unittest.TestCase):
    def setUp(self):
        # spec 088 (FR-008): el comprobante existe en R2 (simulado; los tests no abren red).
        patcher = mock.patch("app.core.asset_refs.object_exists", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    # ------------------------------------------------------------- Helpers

    def _seed_order(self, *, order_status: str = "recibida"):
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        participant = fx.make_participant(db, table_session=ts)
        order = fx.make_customer_order(db, participant, status=order_status)
        return db, participant, order

    def _seed_session_con_carrito(self):
        """Comensal con sesión abierta y un carrito no vacío, sin ninguna
        `CustomerOrder` todavía — el punto de partida de `submit_cart`
        (spec 025)."""
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        participant = fx.make_participant(db, table_session=ts)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=Decimal("8000"))
        cart = fx.make_cart(db, participant=participant)
        fx.make_cart_item(db, cart, variant)
        db.commit()
        return db, participant, variant

    # ------------------------------------------------- list_payment_methods (US2)

    def test_list_payment_methods_solo_activos(self):
        """FR-004/Acceptance Scenario 1 (US2): el comensal solo ve métodos
        `active=True`, ninguno desactivado."""
        db, participant, order = self._seed_order()
        fx.make_payment_method(db, name="Efectivo", is_cash=True, active=True)
        fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer", active=True)
        fx.make_payment_method(db, name="Daviplata", is_cash=False, type="transfer", active=False)
        db.commit()

        methods = service.list_payment_methods(db)
        names = {m.name for m in methods}
        self.assertEqual(names, {"Efectivo", "Nequi"})

    # ------------------------------------------------- create_payment_attempt (US2)

    def test_create_payment_attempt_ok_queda_pendiente(self):
        db, participant, order = self._seed_order()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()

        attempt = service.create_payment_attempt(db, participant.id, order.id, nequi.id)
        self.assertEqual(attempt.status, "pendiente")
        self.assertEqual(attempt.order_id, order.id)
        self.assertIsNone(attempt.receipt_file_url)

    def test_create_payment_attempt_409_si_ya_hay_uno_pendiente(self):
        """FR-015a: no se permite un segundo intento mientras uno sigue
        pendiente de revisión."""
        db, participant, order = self._seed_order()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()

        service.create_payment_attempt(db, participant.id, order.id, nequi.id)
        with self.assertRaises(HTTPException) as ctx:
            service.create_payment_attempt(db, participant.id, order.id, nequi.id)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_create_payment_attempt_404_si_la_orden_no_es_del_comensal(self):
        db, participant, order = self._seed_order()
        otro_participant = fx.make_participant(db, table_session=fx.make_table_session(db))
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.create_payment_attempt(db, otro_participant.id, order.id, nequi.id)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_create_payment_attempt_409_si_la_orden_no_esta_recibida(self):
        db, participant, order = self._seed_order(order_status="abierta")
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.create_payment_attempt(db, participant.id, order.id, nequi.id)
        self.assertEqual(ctx.exception.status_code, 409)

    # ------------------------------------------------- presign/attach receipt (US2)

    def test_presign_y_attach_receipt_ok(self):
        """Acceptance Scenario 2-3 (US2): el comensal ve los datos de pago del
        método elegido (payment_info) y, al subir el comprobante, queda
        asociado al intento — que sigue `pendiente` (sin autoconfirmación,
        FR-013)."""
        db, participant, order = self._seed_order()
        nequi = fx.make_payment_method(
            db, name="Nequi", is_cash=False, type="transfer",
            payment_info={"cuenta": "3001234567", "titular": "Heladería La 14"},
        )
        db.commit()
        attempt = service.create_payment_attempt(db, participant.id, order.id, nequi.id)

        presign = service.presign_receipt(
            db, "tenant_test", participant.id, attempt.id, "image/jpeg"
        )
        self.assertTrue(presign.upload_url)
        self.assertTrue(presign.public_url.endswith(".jpg"))

        updated = service.attach_receipt(
            db, participant.id, attempt.id, presign.public_url, tenant_schema=SCHEMA
        )
        self.assertEqual(updated.status, "pendiente")
        # spec 088 (A-93): en base de datos vive la key, no la URL absoluta.
        self.assertEqual(updated.receipt_file_url, presign.key)
        self.assertEqual(nequi.payment_info["cuenta"], "3001234567")

    def test_attach_receipt_409_si_ya_tiene_uno(self):
        db, participant, order = self._seed_order()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()
        attempt = service.create_payment_attempt(db, participant.id, order.id, nequi.id)
        service.attach_receipt(db, participant.id, attempt.id, RECEIPT_A, tenant_schema=SCHEMA)

        with self.assertRaises(HTTPException) as ctx:
            service.attach_receipt(db, participant.id, attempt.id, RECEIPT_B, tenant_schema=SCHEMA)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_presign_receipt_409_si_metodo_es_efectivo(self):
        db, participant, order = self._seed_order()
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        db.commit()
        attempt = service.create_payment_attempt(db, participant.id, order.id, efectivo.id)

        with self.assertRaises(HTTPException) as ctx:
            service.presign_receipt(db, "tenant_test", participant.id, attempt.id, "image/jpeg")
        self.assertEqual(ctx.exception.status_code, 409)

    # ------------------------------------------------- current_payment_attempt / US2 escenario 7

    def test_orden_sigue_pendiente_de_pago_mientras_no_hay_confirmado(self):
        """Acceptance Scenario 7 (US2): con un comprobante ya subido pero sin
        que el cajero decida, `list_my_orders` sigue mostrando el intento
        como `pendiente` — nunca se autoconfirma."""
        db, participant, order = self._seed_order()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()
        attempt = service.create_payment_attempt(db, participant.id, order.id, nequi.id)
        service.attach_receipt(db, participant.id, attempt.id, RECEIPT_A, tenant_schema=SCHEMA)

        orders = service.list_my_orders(db, participant.id)
        self.assertEqual(orders[0].current_payment_attempt.status, "pendiente")

    # ------------------------------------------------- US5: reintento tras rechazo

    def test_reintento_tras_rechazo_crea_intento_nuevo_conserva_items(self):
        """US5, Acceptance Scenarios 1-3: tras un rechazo, el comensal sube un
        nuevo comprobante — nace un intento nuevo, el rechazado se conserva
        en el historial, y los ítems de la orden no se tocan."""
        db, participant, order = self._seed_order()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product)
        ofx.make_order_item(db, order, variant, estado_cocina="pendiente")
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()

        first = service.create_payment_attempt(db, participant.id, order.id, nequi.id)
        first.status = "rechazado"
        first.rejection_reason = "monto_no_coincide"
        db.commit()

        # Mientras el primero está rechazado (no pendiente), un intento nuevo
        # es admitido (FR-015a solo bloquea si hay uno *pendiente*).
        second = service.create_payment_attempt(db, participant.id, order.id, nequi.id)
        self.assertNotEqual(second.id, first.id)
        self.assertEqual(second.status, "pendiente")

        orders = service.list_my_orders(db, participant.id)
        order_out = orders[0]
        self.assertEqual(len(order_out.items), 1)  # el ítem de la orden no se tocó
        # El intento vigente (más reciente) es el nuevo, no el rechazado.
        self.assertEqual(order_out.current_payment_attempt.id, second.id)
        self.assertEqual(order_out.current_payment_attempt.status, "pendiente")

    def test_reintento_bloqueado_mientras_el_nuevo_sigue_pendiente(self):
        """Edge case de spec.md: un tercer intento no se admite mientras el
        segundo (tras el primer rechazo) sigue pendiente de revisión."""
        db, participant, order = self._seed_order()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()

        first = service.create_payment_attempt(db, participant.id, order.id, nequi.id)
        first.status = "rechazado"
        first.rejection_reason = "transferencia_no_completada"
        db.commit()
        service.create_payment_attempt(db, participant.id, order.id, nequi.id)

        with self.assertRaises(HTTPException) as ctx:
            service.create_payment_attempt(db, participant.id, order.id, nequi.id)
        self.assertEqual(ctx.exception.status_code, 409)

    # ------------------------------------------------- US1: revisión antes de enviar (T008)

    def test_sin_completar_pago_no_existe_ninguna_orden_y_metodos_solo_activos(self):
        """FR-001/FR-002/FR-003, Acceptance Scenarios 1-3 (US1): mientras el
        comensal arma su carrito y revisa cómo pagar, no existe **ninguna**
        `CustomerOrder` para él — ausencia de fila, no un estado nuevo
        (research.md Decisión 3) — y `list_payment_methods` (spec 024, sin
        cambios) solo devuelve métodos `active=True`."""
        db, participant, variant = self._seed_session_con_carrito()
        fx.make_payment_method(db, name="Efectivo", is_cash=True, active=True)
        fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer", active=True)
        fx.make_payment_method(db, name="Daviplata", is_cash=False, type="transfer", active=False)
        db.commit()

        count = db.execute(
            select(func.count(CustomerOrder.id)).where(
                CustomerOrder.participant_id == participant.id
            )
        ).scalar_one()
        self.assertEqual(count, 0)

        methods = service.list_payment_methods(db)
        names = {m.name for m in methods}
        self.assertEqual(names, {"Efectivo", "Nequi"})

    # ------------------------------------------------- US2: efectivo (T010)

    def test_submit_cart_efectivo_crea_orden_e_intento_pendiente(self):
        """FR-004, Acceptance Scenario 1 (US2): elegir efectivo y confirmar
        crea la orden junto con su primer intento, pendiente de que el
        cajero registre el monto recibido."""
        db, participant, variant = self._seed_session_con_carrito()
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        db.commit()

        order = service.submit_cart(db, participant, efectivo.id)

        self.assertEqual(order.status, "recibida")
        attempt = order.current_payment_attempt
        self.assertEqual(attempt.status, "pendiente")
        self.assertTrue(attempt.is_cash)
        self.assertIsNone(attempt.receipt_file_url)

    def test_submit_cart_efectivo_con_receipt_file_url_falla_422(self):
        """FR-004: un pago en efectivo no admite comprobante."""
        db, participant, variant = self._seed_session_con_carrito()
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.submit_cart(
                db, participant, efectivo.id, receipt_file_url="https://example.invalid/a.jpg"
            )
        self.assertEqual(ctx.exception.status_code, 422)

    # ------------------------------------------------- US3: transferencia (T018)

    def test_presign_payment_receipt_sin_attempt_id_no_crea_ninguna_orden(self):
        """contracts/payment-receipt-presign.md: el presign genérico no
        exige ningún `attempt_id` (no hay ningún recurso todavía al que
        asociarlo) y, tras llamarlo, sigue sin existir ninguna
        `CustomerOrder` — el archivo se sube a R2 antes de que el pedido
        exista."""
        db, participant, variant = self._seed_session_con_carrito()
        db.commit()

        presign = service.presign_payment_receipt(db, "tenant_test", participant.id, "image/jpeg")
        self.assertTrue(presign.upload_url)
        self.assertTrue(presign.public_url.endswith(".jpg"))

        count = db.execute(
            select(func.count(CustomerOrder.id)).where(
                CustomerOrder.participant_id == participant.id
            )
        ).scalar_one()
        self.assertEqual(count, 0)

    def test_submit_cart_transferencia_con_receipt_file_url_crea_orden(self):
        """FR-005/FR-006/FR-007, Acceptance Scenario 1 (US3): con el
        comprobante ya subido, `submit_cart` crea la orden con
        `current_payment_attempt.receipt_file_url` igual al `public_url` del
        presign."""
        db, participant, variant = self._seed_session_con_carrito()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()
        presign = service.presign_payment_receipt(db, "tenant_test", participant.id, "image/jpeg")

        order = service.submit_cart(
            db, participant, nequi.id, receipt_file_url=presign.public_url, tenant_schema=SCHEMA
        )

        # spec 088 (A-93): se persiste la key del `public_url` del presign.
        self.assertEqual(order.current_payment_attempt.receipt_file_url, presign.key)

    def test_submit_cart_transferencia_sin_receipt_file_url_crea_orden(self):
        """spec 132 (A-144, FR-001/FR-023): el comensal ya no sube comprobante
        desde el QR — `submit_cart` crea el pedido igual, con el intento
        `pendiente` y `receipt_file_url=None`. Antes de spec 132 esto fallaba
        con 422 (ver A-144 en registro-de-anomalias.md, Principio II)."""
        db, participant, variant = self._seed_session_con_carrito()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()

        order = service.submit_cart(db, participant, nequi.id)
        self.assertEqual(order.current_payment_attempt.status, "pendiente")
        self.assertIsNone(order.current_payment_attempt.receipt_file_url)

    # ------------------------------------------------- FR-012: reintento sin volver a subir (T019)

    def test_reintento_tras_fallo_de_commit_no_deja_nada_y_reintento_crea_orden(self):
        """FR-012, edge case "reintento tras fallo de creación": si
        `submit_cart` falla después de que el comprobante ya se subió, no
        queda ninguna `CustomerOrder` ni `OrderPaymentAttempt` a medias; un
        reintento con el mismo `public_url` (sin volver a llamar al presign)
        sí crea la orden."""
        db, participant, variant = self._seed_session_con_carrito()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()
        presign = service.presign_payment_receipt(db, "tenant_test", participant.id, "image/jpeg")

        with mock.patch.object(
            db, "commit", side_effect=Exception("fallo forzado por test")
        ):
            with self.assertRaises(Exception):
                service.submit_cart(
                    db, participant, nequi.id, receipt_file_url=presign.public_url,
                    tenant_schema=SCHEMA,
                )

        self.assertEqual(
            db.execute(
                select(func.count(CustomerOrder.id)).where(
                    CustomerOrder.participant_id == participant.id
                )
            ).scalar_one(),
            0,
        )
        self.assertEqual(
            db.execute(select(func.count(OrderPaymentAttempt.id))).scalar_one(), 0
        )

        order = service.submit_cart(
            db, participant, nequi.id, receipt_file_url=presign.public_url, tenant_schema=SCHEMA
        )
        self.assertEqual(order.current_payment_attempt.receipt_file_url, presign.key)

    # ------------------------------------------------- spec 130: pago mixto del comensal

    def test_submit_cart_mixto_resuelve_un_unico_metodo_de_transferencia(self):
        """FR-005/FR-006/FR-007: con un único método de transferencia activo se
        resuelve solo; el desglose (`cash_amount`/`transfer_amount`/
        `transfer_method_id`) queda persistido en el intento."""
        db, participant, variant = self._seed_session_con_carrito()  # total = $8.000
        mixto = fx.make_payment_method(db, name="Pago mixto", is_cash=False, type="mixed")
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()
        presign = service.presign_payment_receipt(db, "tenant_test", participant.id, "image/jpeg")

        order = service.submit_cart(
            db, participant, mixto.id, receipt_file_url=presign.public_url, tenant_schema=SCHEMA,
            cash_amount=Decimal("3000"),
        )

        attempt = order.current_payment_attempt
        self.assertEqual(attempt.cash_amount, Decimal("3000"))
        self.assertEqual(attempt.transfer_amount, Decimal("5000"))
        self.assertEqual(attempt.transfer_method_id, nequi.id)
        self.assertEqual(attempt.receipt_file_url, presign.key)
        self.assertIsNone(attempt.cash_confirmed_at)
        self.assertEqual(attempt.status, "pendiente")

    def test_submit_cart_mixto_con_dos_transferencias_exige_elegir(self):
        """FR-005: con más de un método de transferencia activo, omitir
        `transfer_method_id` falla con 422."""
        db, participant, variant = self._seed_session_con_carrito()
        mixto = fx.make_payment_method(db, name="Pago mixto", is_cash=False, type="mixed")
        fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        bancolombia = fx.make_payment_method(db, name="Bancolombia", is_cash=False, type="transfer")
        db.commit()
        presign = service.presign_payment_receipt(db, "tenant_test", participant.id, "image/jpeg")

        with self.assertRaises(HTTPException) as ctx:
            service.submit_cart(
                db, participant, mixto.id, receipt_file_url=presign.public_url, tenant_schema=SCHEMA,
                cash_amount=Decimal("3000"),
            )
        self.assertEqual(ctx.exception.status_code, 422)

        # Elegido explícitamente, sí funciona.
        order = service.submit_cart(
            db, participant, mixto.id, receipt_file_url=presign.public_url, tenant_schema=SCHEMA,
            cash_amount=Decimal("3000"), transfer_method_id=bancolombia.id,
        )
        self.assertEqual(order.current_payment_attempt.transfer_method_id, bancolombia.id)

    def test_submit_cart_mixto_cash_amount_nulo_o_cero_falla_422(self):
        """FR-006: `cash_amount` es obligatorio y debe ser mayor a $0."""
        db, participant, variant = self._seed_session_con_carrito()
        mixto = fx.make_payment_method(db, name="Pago mixto", is_cash=False, type="mixed")
        fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()
        presign = service.presign_payment_receipt(db, "tenant_test", participant.id, "image/jpeg")

        with self.assertRaises(HTTPException) as ctx:
            service.submit_cart(
                db, participant, mixto.id, receipt_file_url=presign.public_url, tenant_schema=SCHEMA,
            )
        self.assertEqual(ctx.exception.status_code, 422)

        with self.assertRaises(HTTPException) as ctx:
            service.submit_cart(
                db, participant, mixto.id, receipt_file_url=presign.public_url, tenant_schema=SCHEMA,
                cash_amount=Decimal("0"),
            )
        self.assertEqual(ctx.exception.status_code, 422)

    def test_submit_cart_mixto_cash_amount_igual_o_mayor_al_total_falla_422(self):
        """FR-006: el monto en efectivo debe ser estrictamente menor al total
        ($8.000) — orienta a elegir 'Efectivo' si lo cubre todo."""
        db, participant, variant = self._seed_session_con_carrito()
        mixto = fx.make_payment_method(db, name="Pago mixto", is_cash=False, type="mixed")
        fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()
        presign = service.presign_payment_receipt(db, "tenant_test", participant.id, "image/jpeg")

        with self.assertRaises(HTTPException) as ctx:
            service.submit_cart(
                db, participant, mixto.id, receipt_file_url=presign.public_url, tenant_schema=SCHEMA,
                cash_amount=Decimal("8000"),
            )
        self.assertEqual(ctx.exception.status_code, 422)

        # Ninguna orden ni intento quedó a medias tras el 422 (mismo criterio
        # que el resto de `submit_cart`, FR-012 de spec 025).
        self.assertEqual(
            db.execute(
                select(func.count(CustomerOrder.id)).where(
                    CustomerOrder.participant_id == participant.id
                )
            ).scalar_one(),
            0,
        )

    def test_submit_cart_mixto_sin_comprobante_crea_orden(self):
        """spec 132 (A-144, FR-002/FR-023): el comensal ya no sube comprobante
        de la porción por transferencia desde el QR — `submit_cart` crea el
        pedido igual. Antes de spec 132 esto fallaba con 422."""
        db, participant, variant = self._seed_session_con_carrito()
        mixto = fx.make_payment_method(db, name="Pago mixto", is_cash=False, type="mixed")
        fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()

        order = service.submit_cart(db, participant, mixto.id, cash_amount=Decimal("3000"))
        attempt = order.current_payment_attempt
        self.assertEqual(attempt.cash_amount, Decimal("3000"))
        self.assertEqual(attempt.transfer_amount, Decimal("5000"))
        self.assertIsNone(attempt.receipt_file_url)


if __name__ == "__main__":
    unittest.main()
