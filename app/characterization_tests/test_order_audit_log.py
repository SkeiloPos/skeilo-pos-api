"""Tests de la nueva funcionalidad — spec 074-auditoria-ordenes: el log de
auditoría del ciclo de vida de una orden (FR-001 … FR-012, SC-001 … SC-005).

No son characterization tests: la emisión de eventos de auditoría hacia Sentry
Logs es comportamiento enteramente nuevo. Se verifican contra
`spec.md`/`data-model.md`/`contracts/order-audit-log-event.md` del repositorio
`pos-specs`, no contra un comportamiento heredado.

Dos niveles, como fija `research.md` § 6:

  - **Unitarios** del helper (`_hash_sensitive`, `record_order_audit_event`),
    mockeando el punto de salida real (`sentry_sdk.logger.info`) para inspeccionar
    el payload exacto que viajaría a Sentry. Requieren `ENVIRONMENT="prod"`
    parcheado: fuera de prod el helper es un no-op deliberado (research.md § 2).
  - **De integración** de los 8 puntos de emisión (research.md § 4), mockeando
    `record_order_audit_event` en el módulo bajo prueba y ejecutando la función
    de servicio real contra fixtures de base de datos reales.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_order_audit_log -v
"""
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest import mock
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.cart import service as cart_service
from app.api.v1.orders import checkout
from app.api.v1.orders import service as orders_service
from app.api.v1.orders.schemas import (
    CancelIn, CheckoutAndSendIn, OrderChannel, OrderCreate, OrderItemIn, OrderType,
)
from app.api.v1.sales.schemas import PaymentIn
from app.core.config import settings
from app.core.order_audit import (
    ActorType,
    OrderAuditActor,
    OrderAuditEventType,
    _hash_sensitive,
    record_order_audit_event,
)
from app.models.order_payment_attempt import OrderPaymentAttempt
from app.models.session_participant import SessionParticipant

PRECIO = Decimal("18000")

SECRETO = "clave-de-auditoria-de-prueba"

NOMBRE_COMENSAL = "María Pérez"

# spec 088 (A-93): el comprobante que se persiste y se hashea es la KEY (carpeta `comprobantes`
# del negocio); el frontend puede seguir enviando la URL del presign, que `submit_cart` reduce a
# esta key. `object_exists` se simula en los tests que pasan por `submit_cart`.
COMPROBANTE = "tenant_test/comprobantes/abc123.jpg"
COMPROBANTE_URL = f"https://example.invalid/{COMPROBANTE}"


def _prod():
    """El gate de entorno (research.md § 2) hace de `record_order_audit_event`
    un no-op fuera de prod; los tests unitarios del helper lo levantan."""
    return mock.patch.object(settings, "ENVIRONMENT", "prod")


def _con_secreto():
    return mock.patch.object(settings, "AUDIT_HASH_SECRET", SECRETO)


def _staff():
    """Doble del `User` del cajero: el mismo patrón de `orders_fixtures`
    (`make_user_double`), más los dos atributos que la auditoría lee del
    usuario real — su rol y su tenant (data-model.md § Actor, FR-004)."""
    return SimpleNamespace(
        id=uuid4(), name="Cajero de prueba", role_name="CASHIER", tenant_id=7,
    )


def _eventos(recorder, event_type=None) -> list[dict]:
    """Los kwargs de cada llamada capturada a `record_order_audit_event`,
    opcionalmente filtrados por tipo de evento."""
    llamadas = [c.kwargs for c in recorder.call_args_list]
    if event_type is None:
        return llamadas
    return [c for c in llamadas if c["event_type"] is event_type]


class TestOrderAuditHelper(unittest.TestCase):
    """Fase 2 (T008/T009): el helper de emisión y la función de hash."""

    def _record(self, **kw):
        """Ejecuta `record_order_audit_event` en un entorno 'prod' simulado y
        devuelve el doble de `sentry_sdk` para inspeccionar lo enviado."""
        kw.setdefault("event_type", OrderAuditEventType.ORDER_CREATED)
        kw.setdefault("order_id", uuid4())
        kw.setdefault("tenant_id", 7)
        kw.setdefault("actor", OrderAuditActor(type=ActorType.COMENSAL, id=str(uuid4())))
        with _prod(), mock.patch("app.core.order_audit.sentry_sdk") as sentry:
            record_order_audit_event(**kw)
        return sentry

    def _atributos(self, **kw) -> dict:
        sentry = self._record(**kw)
        sentry.logger.info.assert_called_once()
        return sentry.logger.info.call_args.kwargs["attributes"]

    # --------------------------------------------------- _hash_sensitive (T008)

    def test_hash_sensitive_mismo_valor_mismo_hash(self):
        """FR-012: la misma entrada produce siempre la misma salida — es lo que
        permite reconocer un comprobante repetido sin revelarlo."""
        with _con_secreto():
            self.assertEqual(_hash_sensitive(COMPROBANTE), _hash_sensitive(COMPROBANTE))
            self.assertNotIn(COMPROBANTE, _hash_sensitive(COMPROBANTE))

    def test_hash_sensitive_valores_distintos_hashes_distintos(self):
        with _con_secreto():
            self.assertNotEqual(_hash_sensitive("María Pérez"), _hash_sensitive("María Perez"))

    def test_hash_sensitive_sin_secreto_falla_explicito(self):
        """research.md § 3: sin `AUDIT_HASH_SECRET` falla explícito — nunca cae
        en silencio a otro secreto del sistema (p. ej. `JWT_SECRET`)."""
        with mock.patch.object(settings, "AUDIT_HASH_SECRET", None):
            with self.assertRaises(RuntimeError) as ctx:
                _hash_sensitive(NOMBRE_COMENSAL)
        self.assertIn("AUDIT_HASH_SECRET", str(ctx.exception))

    # ------------------------------------------ record_order_audit_event (T009)

    def test_atributos_obligatorios_siempre_presentes(self):
        """data-model.md, reglas 2 y 3: `event_type`/`order_id`/`tenant_id`/
        `actor_type`/`occurred_at` están en el 100% de los eventos."""
        order_id, tenant_id = uuid4(), 42
        atributos = self._atributos(
            event_type=OrderAuditEventType.ORDER_CANCELLED,
            order_id=order_id,
            tenant_id=tenant_id,
            actor=OrderAuditActor(type=ActorType.SISTEMA),
        )
        for clave in ("event_type", "order_id", "tenant_id", "actor_type", "occurred_at"):
            self.assertIn(clave, atributos)
        self.assertEqual(atributos["event_type"], "order.cancelled")
        self.assertEqual(atributos["order_id"], str(order_id))
        self.assertEqual(atributos["tenant_id"], tenant_id)
        self.assertEqual(atributos["actor_type"], "sistema")

    def test_payload_plano_sin_objetos_anidados(self):
        """contracts/order-audit-log-event.md § Envoltorio común: Sentry Logs
        solo preserva escalares como atributo filtrable, así que ni `actor` ni
        `details` viajan como objeto anidado — se aplanan al nivel superior."""
        user_id = uuid4()
        atributos = self._atributos(
            event_type=OrderAuditEventType.PAYMENT_CASH_CONFIRMED,
            actor=OrderAuditActor(type=ActorType.CAJERO, id=str(user_id), role="CASHIER"),
            details={"amount_received": 20000.0, "change": 2000.0},
        )
        self.assertNotIn("actor", atributos)
        self.assertNotIn("details", atributos)
        self.assertEqual(atributos["actor_id"], str(user_id))
        self.assertEqual(atributos["actor_role"], "CASHIER")
        self.assertEqual(atributos["amount_received"], 20000.0)
        self.assertEqual(atributos["change"], 2000.0)
        for clave, valor in atributos.items():
            self.assertIsInstance(valor, (bool, int, float, str), msg=f"{clave} no es escalar")

    def test_ninguna_clave_viaja_con_valor_none(self):
        """Un `None` se omite, no se envía: `format_attribute(None)` produciría
        el string literal 'None' en el panel de Sentry."""
        atributos = self._atributos(
            actor=OrderAuditActor(type=ActorType.COMENSAL, id=str(uuid4())),
            details={"channel": "QR_MENU", "order_type": None, "diner_name_hash": None},
        )
        self.assertNotIn("actor_role", atributos)
        self.assertNotIn("order_type", atributos)
        self.assertNotIn("diner_name_hash", atributos)
        self.assertNotIn(None, list(atributos.values()))

    def test_template_es_el_event_type(self):
        """FR-007: el mensaje del log es el propio `event_type`, lo que hace de
        la auditoría una categoría distinguible del logging operativo."""
        sentry = self._record(event_type=OrderAuditEventType.PAYMENT_TRANSFER_REJECTED)
        self.assertEqual(
            sentry.logger.info.call_args.args[0], "order.payment.transfer_rejected"
        )

    def test_una_falla_de_sentry_nunca_se_propaga(self):
        """FR-011: la auditoría jamás rompe la transición de negocio que ya se
        comprometió en base de datos."""
        with _prod(), mock.patch("app.core.order_audit.sentry_sdk") as sentry:
            sentry.logger.info.side_effect = RuntimeError("boom")
            record_order_audit_event(
                event_type=OrderAuditEventType.ORDER_CREATED,
                order_id=uuid4(),
                tenant_id=7,
                actor=OrderAuditActor(type=ActorType.SISTEMA),
            )  # no debe lanzar

    def test_fuera_de_prod_no_envia_nada(self):
        """research.md § 2: mismo guard que ya usa `error_middleware.py`."""
        with mock.patch.object(settings, "ENVIRONMENT", "dev"), \
                mock.patch("app.core.order_audit.sentry_sdk") as sentry:
            record_order_audit_event(
                event_type=OrderAuditEventType.ORDER_CREATED,
                order_id=uuid4(),
                tenant_id=7,
                actor=OrderAuditActor(type=ActorType.SISTEMA),
            )
        sentry.logger.info.assert_not_called()


class TestOrderAuditIntegration(unittest.TestCase):
    """Fases 3-5 (US1/US2/US3): los 7 puntos de emisión, sobre las funciones de
    servicio reales y fixtures de base de datos reales."""

    def setUp(self):
        """`AUDIT_HASH_SECRET` configurado en todos estos tests: es la
        situación real de producción, y sin él cada punto de integración que
        hashea un dato sensible dejaría el campo fuera (degradación tolerada
        por FR-011, pero no es lo que se quiere verificar aquí)."""
        parche = _con_secreto()
        parche.start()
        self.addCleanup(parche.stop)

    # ------------------------------------------------------------- Helpers

    def _seed_carrito_qr(self, *, is_cash=True, nombre=NOMBRE_COMENSAL):
        """Comensal con carrito no vacío y un método de pago activo — el punto
        de partida de `submit_cart`. Con receta e inventario de sobra para que
        la confirmación posterior (que sí descuenta) no falle por stock."""
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        participant = fx.make_participant(db, table_session=ts, display_name=nombre)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        insumo = fx.make_inventory_item(db, current_stock=Decimal("1000"))
        fx.make_recipe_item(db, variant, insumo, quantity=Decimal("1"))
        cart = fx.make_cart(db, participant=participant)
        fx.make_cart_item(db, cart, variant)
        method = (
            fx.make_payment_method(db, name="Efectivo", is_cash=True) if is_cash
            else fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        )
        db.commit()
        return db, participant, method

    def _seed_orden_recibida(self, *, is_cash=True, receipt=None):
        """Orden 'recibida' con su ítem y un intento de pago pendiente — el
        punto de partida de aprobar/rechazar/confirmar efectivo."""
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
        method = (
            fx.make_payment_method(db, name="Efectivo", is_cash=True) if is_cash
            else fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        )
        attempt = fx.make_payment_attempt(
            db, order, method, status="pendiente", receipt_file_url=receipt
        )
        db.commit()
        return db, order, attempt

    def _attempt_de(self, db, order_id):
        return db.execute(
            select(OrderPaymentAttempt).where(OrderPaymentAttempt.order_id == order_id)
        ).scalars().first()

    def _orden_creada_por_staff(self, db, *, hold_for_payment=False, user=None):
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        insumo = fx.make_inventory_item(db, current_stock=Decimal("1000"))
        fx.make_recipe_item(db, variant, insumo, quantity=Decimal("1"))
        db.commit()
        user = user or _staff()
        data = OrderCreate(
            channel=OrderChannel.POS,
            order_type=OrderType.DINE_IN,
            customer_name="Cliente de prueba",
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1)],
            hold_for_payment=hold_for_payment,
        )
        return orders_service.create_order(db, data, user_id=user.id, user=user), user

    def _seed_hold_order_con_receta(self):
        """Mesa + orden 'recibida' (como si hubiera nacido con
        `hold_for_payment=True`, T013) con una variante con receta, y turno de
        caja abierto — el punto de partida de `checkout_and_send` (T040)."""
        db = fx.new_session()
        table = fx.make_dining_table(db, status="ocupada")
        ts = fx.make_table_session(db, table=table)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        insumo = fx.make_inventory_item(db, current_stock=Decimal("1000"))
        fx.make_recipe_item(db, variant, insumo, quantity=Decimal("1"))
        order = fx.make_customer_order(db, ts, status="recibida", channel="POS")
        fx.make_order_item(db, order, variant, quantity=1, estado_cocina="pendiente")
        register = fx.make_cash_register(db)
        shift = fx.make_cash_shift(db, register=register)
        method = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        db.commit()
        return db, order, shift, method

    # ---------------------------------------------------- submit_cart (T010)

    def test_submit_cart_emite_order_created_con_actor_comensal(self):
        db, participant, method = self._seed_carrito_qr()

        with mock.patch.object(cart_service, "record_order_audit_event") as rec:
            order = cart_service.submit_cart(
                db, participant, method.id, tenant_id=7,
            )

        eventos = _eventos(rec, OrderAuditEventType.ORDER_CREATED)
        self.assertEqual(len(eventos), 1)
        evento = eventos[0]
        self.assertEqual(evento["order_id"], order.id)
        self.assertEqual(evento["tenant_id"], 7)
        self.assertEqual(evento["actor"], OrderAuditActor(
            type=ActorType.COMENSAL, id=str(participant.id),
        ))
        self.assertEqual(evento["details"]["channel"], "QR_MENU")
        self.assertEqual(evento["details"]["order_type"], "DINE_IN")

    # ---------------------------------------------------- create_order (T011)

    def test_create_order_emite_order_created_con_actor_cajero(self):
        db = fx.new_session()
        with mock.patch.object(orders_service, "record_order_audit_event") as rec:
            order, user = self._orden_creada_por_staff(db, hold_for_payment=True)

        eventos = _eventos(rec, OrderAuditEventType.ORDER_CREATED)
        self.assertEqual(len(eventos), 1)
        evento = eventos[0]
        self.assertEqual(evento["order_id"], order.id)
        self.assertEqual(evento["tenant_id"], user.tenant_id)
        self.assertEqual(evento["actor"], OrderAuditActor(
            type=ActorType.CAJERO, id=str(user.id), role="CASHIER",
        ))
        self.assertEqual(
            evento["details"],
            {"channel": "POS", "order_type": "DINE_IN", "hold_for_payment": True},
        )

    # -------------------------------------------- create_payment_attempt (T012)

    def test_create_payment_attempt_emite_payment_attempt_created(self):
        """El reintento tras un rechazo (el otro camino que crea un intento;
        el primero nace dentro de `submit_cart`)."""
        db, order, attempt = self._seed_orden_recibida(is_cash=False)
        attempt.status = "rechazado"
        attempt.rejection_reason = "comprobante_sospechoso"
        nequi = fx.make_payment_method(db, name="Nequi 2", is_cash=False, type="transfer")
        db.commit()

        with mock.patch.object(cart_service, "record_order_audit_event") as rec:
            cart_service.create_payment_attempt(
                db, order.participant_id, order.id, nequi.id, tenant_id=7,
            )

        eventos = _eventos(rec, OrderAuditEventType.PAYMENT_ATTEMPT_CREATED)
        self.assertEqual(len(eventos), 1)
        evento = eventos[0]
        self.assertEqual(evento["order_id"], order.id)
        self.assertEqual(evento["actor"].type, ActorType.COMENSAL)
        self.assertEqual(evento["details"]["payment_method_type"], "transfer")
        self.assertEqual(evento["details"]["payment_method_name"], "Nequi 2")

    # ------------------------------------------------- _confirm_order_impl (T013)

    def test_confirm_order_emite_order_confirmed_tras_la_transicion(self):
        db, order, attempt = self._seed_orden_recibida()
        attempt.status = "confirmado"
        db.commit()
        user = _staff()

        with mock.patch.object(checkout, "record_order_audit_event") as rec:
            result = checkout.confirm_order(db, order.id, user)

        self.assertEqual(result.status, "abierta")  # la transición sí ocurrió
        eventos = _eventos(rec, OrderAuditEventType.ORDER_CONFIRMED)
        self.assertEqual(len(eventos), 1)
        self.assertEqual(eventos[0]["order_id"], order.id)
        self.assertEqual(eventos[0]["tenant_id"], user.tenant_id)

    # ------------------------------------- confirm_cash_payment_attempt (T014)

    def test_confirm_cash_emite_payment_cash_confirmed(self):
        db, order, attempt = self._seed_orden_recibida()
        shift = fx.make_cash_shift(db)
        db.commit()
        user = _staff()

        with mock.patch.object(checkout, "record_order_audit_event") as rec:
            checkout.confirm_cash_payment_attempt(
                db, attempt.id, Decimal("20000"), shift.id, user
            )

        eventos = _eventos(rec, OrderAuditEventType.PAYMENT_CASH_CONFIRMED)
        self.assertEqual(len(eventos), 1)
        evento = eventos[0]
        self.assertEqual(evento["order_id"], order.id)
        self.assertEqual(evento["actor"], OrderAuditActor(
            type=ActorType.CAJERO, id=str(user.id), role="CASHIER",
        ))
        self.assertEqual(evento["details"]["amount_received"], 20000.0)
        self.assertEqual(evento["details"]["change"], float(Decimal("20000") - PRECIO))

    # ---------------------------------------- approve_payment_attempt (T015)

    def test_approve_emite_transfer_approved(self):
        db, order, attempt = self._seed_orden_recibida(is_cash=False, receipt=COMPROBANTE)
        shift = fx.make_cash_shift(db)
        db.commit()
        user = _staff()

        with mock.patch.object(checkout, "record_order_audit_event") as rec:
            checkout.approve_payment_attempt(db, attempt.id, shift.id, user)

        eventos = _eventos(rec, OrderAuditEventType.PAYMENT_TRANSFER_APPROVED)
        self.assertEqual(len(eventos), 1)
        self.assertEqual(eventos[0]["order_id"], order.id)
        self.assertEqual(eventos[0]["actor"].type, ActorType.CAJERO)

    # ----------------------------------------- reject_payment_attempt (T016)

    def test_reject_emite_transfer_rejected_con_su_motivo(self):
        db, order, attempt = self._seed_orden_recibida(is_cash=False, receipt=COMPROBANTE)
        user = _staff()

        with mock.patch.object(checkout, "record_order_audit_event") as rec:
            checkout.reject_payment_attempt(db, attempt.id, "monto_no_coincide", user)

        eventos = _eventos(rec, OrderAuditEventType.PAYMENT_TRANSFER_REJECTED)
        self.assertEqual(len(eventos), 1)
        self.assertEqual(eventos[0]["order_id"], order.id)
        self.assertEqual(eventos[0]["details"]["rejection_reason"], "monto_no_coincide")

    # ---------------------------------------------------- cancel_order (T017)

    def test_cancel_order_emite_order_cancelled_por_staff_y_por_comensal(self):
        """FR-009: `initiated_by` distingue las dos rutas de entrada, y
        `inventory_loss` marca si algún ítem ya consumido no volvió al stock."""
        # (a) staff, con un ítem ya 'en_preparacion' sobre una orden abierta:
        # ese insumo ya se combinó, no vuelve al inventario → pérdida.
        db, order, _ = self._seed_orden_recibida()
        order.status = "abierta"
        for item in order.items:
            item.estado_cocina = "en_preparacion"
        db.commit()
        user = _staff()

        with mock.patch.object(checkout, "record_order_audit_event") as rec:
            checkout.cancel_order(
                db, order.id, CancelIn(motivo="cliente se retiró"), user, tenant_id=7,
            )

        eventos = _eventos(rec, OrderAuditEventType.ORDER_CANCELLED)
        self.assertEqual(len(eventos), 1)
        evento = eventos[0]
        self.assertEqual(evento["tenant_id"], 7)
        self.assertEqual(evento["actor"].type, ActorType.CAJERO)
        self.assertEqual(evento["details"]["initiated_by"], "staff")
        self.assertEqual(evento["details"]["reason"], "cliente se retiró")
        self.assertIs(evento["details"]["inventory_loss"], True)

        # (b) el propio comensal, sobre una orden 'recibida' (nada descontado).
        db2, order2, _ = self._seed_orden_recibida()
        participant = db2.get(SessionParticipant, order2.participant_id)

        with mock.patch.object(checkout, "record_order_audit_event") as rec2:
            cart_service.cancel_my_order(
                db2, participant, order2.id, "me equivoqué", tenant_id=7,
            )

        eventos2 = _eventos(rec2, OrderAuditEventType.ORDER_CANCELLED)
        self.assertEqual(len(eventos2), 1)
        evento2 = eventos2[0]
        self.assertEqual(evento2["actor"], OrderAuditActor(
            type=ActorType.COMENSAL, id=str(participant.id),
        ))
        self.assertEqual(evento2["details"]["initiated_by"], "comensal")
        self.assertIs(evento2["details"]["inventory_loss"], False)

    # ----------------------------------------- transición fallida (T018)

    def test_ningun_evento_si_la_transicion_falla(self):
        """FR-010: el evento solo sale tras un `commit` exitoso — nunca por un
        intento rechazado por la validación de negocio."""
        # (a) efectivo insuficiente → 422, sin evento.
        db, order, attempt = self._seed_orden_recibida()
        shift = fx.make_cash_shift(db)
        db.commit()
        with mock.patch.object(checkout, "record_order_audit_event") as rec:
            with self.assertRaises(HTTPException) as ctx:
                checkout.confirm_cash_payment_attempt(
                    db, attempt.id, PRECIO - Decimal("1"), shift.id, _staff()
                )
        self.assertEqual(ctx.exception.status_code, 422)
        rec.assert_not_called()

        # (b) confirmar una orden sin pago confirmado → 409, sin evento.
        db2, order2, _ = self._seed_orden_recibida()
        with mock.patch.object(checkout, "record_order_audit_event") as rec2:
            with self.assertRaises(HTTPException) as ctx2:
                checkout.confirm_order(db2, order2.id, _staff())
        self.assertEqual(ctx2.exception.status_code, 409)
        rec2.assert_not_called()

        # (c) comanda manual con una variante sin receta → 409, sin evento.
        db3 = fx.new_session()
        category = fx.make_category(db3)
        product = fx.make_product(db3, category=category)
        variant = fx.make_variant(db3, product=product, price=PRECIO)
        db3.commit()
        data = OrderCreate(
            channel=OrderChannel.POS,
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1)],
        )
        with mock.patch.object(orders_service, "record_order_audit_event") as rec3:
            with self.assertRaises(HTTPException):
                orders_service.create_order(db3, data, user_id=uuid4())
        rec3.assert_not_called()

    # ------------------------------------------ actor automático/manual (T027)

    def test_order_confirmed_distingue_pago_automatico_de_confirmacion_manual(self):
        """US2: la confirmación disparada por un pago tiene actor `sistema`; la
        llamada directa a `confirm_order` (vía de recuperación), `cajero`."""
        # (a) automática, dentro de confirm_cash_payment_attempt.
        db, order, attempt = self._seed_orden_recibida()
        shift = fx.make_cash_shift(db)
        db.commit()
        with mock.patch.object(checkout, "record_order_audit_event") as rec:
            checkout.confirm_cash_payment_attempt(
                db, attempt.id, Decimal("20000"), shift.id, _staff()
            )
        automatico = _eventos(rec, OrderAuditEventType.ORDER_CONFIRMED)
        self.assertEqual(len(automatico), 1)
        self.assertEqual(automatico[0]["actor"].type, ActorType.SISTEMA)
        self.assertIsNone(automatico[0]["actor"].id)
        self.assertEqual(automatico[0]["details"]["trigger"], "automatic_payment")

        # (b) automática, dentro de approve_payment_attempt.
        db2, order2, attempt2 = self._seed_orden_recibida(
            is_cash=False, receipt=COMPROBANTE
        )
        shift2 = fx.make_cash_shift(db2)
        db2.commit()
        with mock.patch.object(checkout, "record_order_audit_event") as rec2:
            checkout.approve_payment_attempt(db2, attempt2.id, shift2.id, _staff())
        automatico2 = _eventos(rec2, OrderAuditEventType.ORDER_CONFIRMED)
        self.assertEqual(len(automatico2), 1)
        self.assertEqual(automatico2[0]["actor"].type, ActorType.SISTEMA)
        self.assertEqual(automatico2[0]["details"]["trigger"], "automatic_payment")

        # (c) manual, vía confirm_order.
        db3, order3, attempt3 = self._seed_orden_recibida()
        attempt3.status = "confirmado"
        db3.commit()
        user = _staff()
        with mock.patch.object(checkout, "record_order_audit_event") as rec3:
            checkout.confirm_order(db3, order3.id, user)
        manual = _eventos(rec3, OrderAuditEventType.ORDER_CONFIRMED)
        self.assertEqual(len(manual), 1)
        self.assertEqual(manual[0]["actor"], OrderAuditActor(
            type=ActorType.CAJERO, id=str(user.id), role="CASHIER",
        ))
        self.assertEqual(manual[0]["details"]["trigger"], "manual")

    # ------------------------------------------------- actor inequívoco (T028)

    def test_todos_los_eventos_llevan_un_actor_inequivoco(self):
        """SC-002: el 100% de los eventos de los 8 tipos identifica su actor
        sin ambigüedad — `actor.id` no nulo salvo para `sistema`."""
        capturados: list[dict] = []

        # (a) orden por QR pagada en efectivo: created + attempt.created +
        # cash_confirmed + confirmed.
        db, participant, efectivo = self._seed_carrito_qr()
        with mock.patch.object(cart_service, "record_order_audit_event") as rec_cart:
            order = cart_service.submit_cart(db, participant, efectivo.id, tenant_id=7)
        capturados += _eventos(rec_cart)
        shift = fx.make_cash_shift(db)
        db.commit()
        attempt = self._attempt_de(db, order.id)
        with mock.patch.object(checkout, "record_order_audit_event") as rec_pago:
            checkout.confirm_cash_payment_attempt(
                db, attempt.id, Decimal("20000"), shift.id, _staff()
            )
        capturados += _eventos(rec_pago)

        # (b) comanda manual cancelada: created + cancelled.
        db_b = fx.new_session()
        with mock.patch.object(orders_service, "record_order_audit_event") as rec_manual:
            order_b, user_b = self._orden_creada_por_staff(db_b, hold_for_payment=True)
        capturados += _eventos(rec_manual)
        with mock.patch.object(checkout, "record_order_audit_event") as rec_cancel:
            checkout.cancel_order(
                db_b, order_b.id, CancelIn(motivo="anulada"), user_b, tenant_id=7,
            )
        capturados += _eventos(rec_cancel)

        # (c) transferencia aprobada y, en otra orden, rechazada.
        db_c, order_c, attempt_c = self._seed_orden_recibida(
            is_cash=False, receipt=COMPROBANTE
        )
        shift_c = fx.make_cash_shift(db_c)
        db_c.commit()
        with mock.patch.object(checkout, "record_order_audit_event") as rec_ok:
            checkout.approve_payment_attempt(db_c, attempt_c.id, shift_c.id, _staff())
        capturados += _eventos(rec_ok)

        db_d, order_d, attempt_d = self._seed_orden_recibida(
            is_cash=False, receipt=COMPROBANTE
        )
        with mock.patch.object(checkout, "record_order_audit_event") as rec_ko:
            checkout.reject_payment_attempt(db_d, attempt_d.id, "comprobante_sospechoso", _staff())
        capturados += _eventos(rec_ko)

        # (e) cobro y envío en un solo paso (FR-014, adenda): confirmed(sistema)
        # + payment.checkout_and_send.
        db_e, order_e, shift_e, method_e = self._seed_hold_order_con_receta()
        data_e = CheckoutAndSendIn(
            version=order_e.version, cash_shift_id=shift_e.id,
            payments=[PaymentIn(payment_method_id=method_e.id, amount=PRECIO)],
        )
        with mock.patch.object(checkout, "record_order_audit_event") as rec_e:
            checkout.checkout_and_send(db_e, order_e.id, data_e, _staff())
        capturados += _eventos(rec_e)

        # Los 8 tipos de evento quedaron cubiertos por la secuencia.
        self.assertEqual(
            {e["event_type"] for e in capturados}, set(OrderAuditEventType)
        )
        for evento in capturados:
            actor = evento["actor"]
            self.assertIn(actor.type, (ActorType.COMENSAL, ActorType.CAJERO, ActorType.SISTEMA))
            self.assertIsNotNone(evento["order_id"])
            if actor.type is ActorType.SISTEMA:
                self.assertIsNone(actor.id)
            else:
                self.assertIsNotNone(actor.id)

    # --------------------------------------------- nombre del comensal (T031)

    def test_order_created_lleva_el_hash_del_nombre_nunca_el_nombre(self):
        """US3/FR-005: sobre lo que realmente saldría hacia Sentry, el nombre
        del comensal aparece solo como HMAC."""
        db, participant, efectivo = self._seed_carrito_qr()

        with _prod(), _con_secreto(), \
                mock.patch("app.core.order_audit.sentry_sdk") as sentry:
            cart_service.submit_cart(db, participant, efectivo.id, tenant_id=7)
            esperado = _hash_sensitive(NOMBRE_COMENSAL)

        creado = [
            c.kwargs["attributes"] for c in sentry.logger.info.call_args_list
            if c.args[0] == "order.created"
        ]
        self.assertEqual(len(creado), 1)
        self.assertEqual(creado[0]["diner_name_hash"], esperado)
        for atributos in [c.kwargs["attributes"] for c in sentry.logger.info.call_args_list]:
            for valor in atributos.values():
                self.assertNotIn(NOMBRE_COMENSAL, str(valor))

    # ----------------------------------------------- comprobante de pago (T032)

    def test_el_mismo_comprobante_produce_el_mismo_hash_en_sus_tres_eventos(self):
        """FR-012: `order.payment_attempt.created`, `transfer_approved` y
        `transfer_rejected` comparten el `receipt_hash` del comprobante, que
        nunca viaja como URL en texto plano.

        spec 088 (A-93, research D11): el hash esperado se calcula sobre la **key** persistida
        (aunque el frontend envíe la URL del presign): la igualdad entre los eventos del mismo
        intento se preserva."""
        with _prod(), _con_secreto(), \
                mock.patch("app.core.order_audit.sentry_sdk") as sentry, \
                mock.patch("app.core.asset_refs.object_exists", return_value=True):
            esperado = _hash_sensitive(COMPROBANTE)

            # Intento rechazado: nace en `submit_cart` (con su comprobante) y
            # lo rechaza el cajero — el mismo intento en sus dos eventos.
            db, participant, nequi = self._seed_carrito_qr(is_cash=False)
            order = cart_service.submit_cart(
                db, participant, nequi.id, COMPROBANTE_URL, tenant_id=7,
                tenant_schema="tenant_test",
            )
            attempt = self._attempt_de(db, order.id)
            checkout.reject_payment_attempt(db, attempt.id, "comprobante_sospechoso", _staff())

            # Intento aprobado, con el mismo comprobante en otra orden.
            db2, participant2, nequi2 = self._seed_carrito_qr(is_cash=False)
            order2 = cart_service.submit_cart(
                db2, participant2, nequi2.id, COMPROBANTE_URL, tenant_id=7,
                tenant_schema="tenant_test",
            )
            shift2 = fx.make_cash_shift(db2)
            db2.commit()
            attempt2 = self._attempt_de(db2, order2.id)
            checkout.approve_payment_attempt(db2, attempt2.id, shift2.id, _staff())

        por_tipo: dict[str, list[dict]] = {}
        for llamada in sentry.logger.info.call_args_list:
            por_tipo.setdefault(llamada.args[0], []).append(llamada.kwargs["attributes"])

        for tipo in (
            "order.payment_attempt.created",
            "order.payment.transfer_approved",
            "order.payment.transfer_rejected",
        ):
            self.assertIn(tipo, por_tipo, msg=f"no se emitió {tipo}")
            for atributos in por_tipo[tipo]:
                self.assertEqual(atributos["receipt_hash"], esperado, msg=tipo)

        for atributos in [a for lista in por_tipo.values() for a in lista]:
            for valor in atributos.values():
                self.assertNotIn(COMPROBANTE, str(valor))
                self.assertNotIn(COMPROBANTE_URL, str(valor))

    # -------------------------------- checkout_and_send (T040, FR-014, adenda)

    def test_checkout_and_send_emite_order_confirmed_y_payment_checkout_and_send(self):
        """FR-014: cobrar y enviar a cocina en un solo paso también se audita
        — como una confirmación automática (actor `sistema`) más un evento de
        pago propio, aunque no pase por `_confirm_order_impl` ni por un
        `OrderPaymentAttempt`."""
        db, order, shift, method = self._seed_hold_order_con_receta()
        cashier = _staff()
        data = CheckoutAndSendIn(
            version=order.version, cash_shift_id=shift.id,
            payments=[PaymentIn(payment_method_id=method.id, amount=PRECIO)],
        )

        with mock.patch.object(checkout, "record_order_audit_event") as rec:
            sale = checkout.checkout_and_send(db, order.id, data, cashier)

        db.refresh(order)
        self.assertEqual(order.status, "pagada")  # la transición sí ocurrió

        confirmados = _eventos(rec, OrderAuditEventType.ORDER_CONFIRMED)
        self.assertEqual(len(confirmados), 1)
        self.assertEqual(confirmados[0]["order_id"], order.id)
        self.assertEqual(confirmados[0]["actor"], OrderAuditActor(type=ActorType.SISTEMA))
        self.assertEqual(confirmados[0]["details"]["trigger"], "automatic_payment")

        pagos = _eventos(rec, OrderAuditEventType.PAYMENT_CHECKOUT_AND_SEND)
        self.assertEqual(len(pagos), 1)
        evento = pagos[0]
        self.assertEqual(evento["order_id"], order.id)
        self.assertEqual(evento["tenant_id"], cashier.tenant_id)
        self.assertEqual(evento["actor"], OrderAuditActor(
            type=ActorType.CAJERO, id=str(cashier.id), role="CASHIER",
        ))
        self.assertEqual(evento["details"]["payment_method_types"], [method.type])
        self.assertEqual(evento["details"]["total_amount"], float(PRECIO))
        self.assertEqual(evento["details"]["payment_count"], 1)
        self.assertEqual(sale.total, PRECIO)

    def test_checkout_and_send_sin_evento_si_la_transaccion_falla(self):
        """FR-010: una versión desactualizada (409) no debe dejar ni el
        `order.confirmed` ni el `order.payment.checkout_and_send`."""
        db, order, shift, method = self._seed_hold_order_con_receta()
        data = CheckoutAndSendIn(
            version=order.version, cash_shift_id=shift.id,
            payments=[PaymentIn(payment_method_id=method.id, amount=PRECIO)],
        )
        # Primer cobro, exitoso, sube la versión — el segundo con la misma
        # `data` (versión vieja) debe fallar sin auditar nada.
        with mock.patch.object(checkout, "record_order_audit_event"):
            checkout.checkout_and_send(db, order.id, data, _staff())

        with mock.patch.object(checkout, "record_order_audit_event") as rec:
            with self.assertRaises(HTTPException) as ctx:
                checkout.checkout_and_send(db, order.id, data, _staff())
        self.assertEqual(ctx.exception.status_code, 409)
        rec.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
