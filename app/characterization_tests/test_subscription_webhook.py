"""Tests de la nueva funcionalidad — spec 161-suscripcion-pago-wompi, Historia de Usuario 4
(FR-019 a FR-027, SC-004 a SC-006; contrato `wompi-webhook.md § 2.2`): el aviso de Wompi aprueba o rechaza
el pago, recalcula las fechas del tenant dentro de UNA transacción y desbloquea al instante.

No son characterization tests — comportamiento enteramente nuevo. Dos bloques:

- `apply_wompi_event` llamado directamente: la matriz estado de Wompi × estado del pago, el recálculo de
  fechas (RN-7), idempotencia, atomicidad y todos los casos sin efecto;
- `POST /api/v1/webhooks/wompi` por HTTP, sin `x-tenant-host` ni token: códigos y cuerpos exactos, y el caso
  que importa de punta a punta: un tenant bloqueado que paga queda desbloqueado en la petición siguiente.

Los eventos se firman con la fórmula documentada de Wompi, recalculada aparte en esta prueba (no hay cuenta
de Wompi; su confirmación con un evento real de Sandbox es la tarea pendiente FR-041).

    python -m unittest app.characterization_tests.test_subscription_webhook -v
"""
from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from unittest import mock

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from starlette.testclient import TestClient

from app.api.v1.plan import subscription_service as service
from app.characterization_tests import fixtures  # noqa: F401  - variables de entorno inertes antes de importar app.main
from app.characterization_tests import plan_fixtures as fx
from app.core import wompi
from app.core.config import settings
from app.core.db import get_db, get_tenant
from app.core.dependencies import get_current_user, get_shared_db
from app.core.subscription import compute_subscription_status
from app.core.subscription_guard import enforce_subscription_access
from app.core.timezone import utc_now_naive
from app.models.subscription_payment import SubscriptionPayment

AHORA = datetime(2026, 12, 6, 12, 0, 0)
PRECIO = Decimal("89900.00")
CENTAVOS = 8_990_000
SECRETO = "test_events_local"


# ----------------------------------------------------------------------
# Eventos firmados y pagos de prueba
# ----------------------------------------------------------------------


def _checksum(event) -> str:
    """Fórmula documentada de Wompi, recalculada aparte del código bajo prueba."""
    dato = event["data"]
    valores = ""
    for ruta in event["signature"]["properties"]:
        cursor = dato
        for parte in ruta.split("."):
            cursor = cursor[parte]
        valores += str(cursor)
    return sha256((valores + str(event["timestamp"]) + SECRETO).encode()).hexdigest()


def _tx(pago) -> str:
    """Una transacción de Wompi distinta por pago: una misma transacción no puede ligarse a dos pagos, así que
    reutilizar un id entre escenarios chocaría (con razón) con esa defensa."""
    return f"TX-{pago.id.hex[:8]}"


def _evento(pago, *, status="APPROVED", cents=CENTAVOS, tx_id=None, reference=None, currency="COP",
            event="transaction.updated", properties=None):
    tx_id = tx_id or _tx(pago)
    ev = {
        "event": event,
        "data": {"transaction": {
            "id": tx_id, "status": status, "amount_in_cents": cents, "currency": currency,
            "reference": reference if reference is not None else pago.gateway_reference,
        }},
        "environment": "test",
        "signature": {
            "properties": properties or ["transaction.id", "transaction.status", "transaction.amount_in_cents"],
            "checksum": "",
        },
        "timestamp": 1530291411,
        "sent_at": "2026-11-30T14:12:09.000Z",
    }
    ev["signature"]["checksum"] = _checksum(ev).upper()
    return ev


def _escenario(db, vence, *, ciclo="mensual", status="pending", gateway_transaction_id=None, precio=PRECIO):
    """Tenant con el vencimiento dado y un pago del estado dado."""
    plan = fx.make_plan(db, precio_mensual=precio)
    tenant = fx.make_tenant(db, plan=plan, ciclo_facturacion=ciclo if vence is not None else None)
    if vence is not None:
        tenant.plan_iniciado_en = vence - timedelta(days=30)
        tenant.plan_vence_en = vence
    payment_id = uuid.uuid4()
    pago = SubscriptionPayment(
        id=payment_id, tenant_id=tenant.id, plan_id=plan.id, amount=precio, status=status,
        gateway_reference=wompi.build_reference(payment_id), gateway_transaction_id=gateway_transaction_id,
        payment_link_url="https://checkout.wompi.co/p/?x=1", payment_link_expires_at=AHORA + timedelta(hours=24),
    )
    db.add(pago)
    db.commit()
    return tenant, pago


def _aplicar(db, ev, now=AHORA):
    return service.apply_wompi_event(db, ev, now=now)


def _estado(db, pago) -> str:
    db.expire_all()
    return db.get(SubscriptionPayment, pago.id).status


def _contar(db) -> int:
    return db.execute(select(func.count()).select_from(SubscriptionPayment)).scalar_one()


# ======================================================================
# Aprobación: recálculo de fechas (FR-022, RN-7)
# ======================================================================


class AprobacionTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()

    def test_por_vencer_la_vigencia_nueva_parte_del_vencimiento_anterior(self):
        vence = AHORA + timedelta(hours=10)
        tenant, pago = _escenario(self.db, vence)

        self.assertEqual(_aplicar(self.db, _evento(pago)), "processed")
        self.db.expire_all()
        pago, tenant = self.db.get(SubscriptionPayment, pago.id), self.db.get(type(tenant), tenant.id)

        self.assertEqual(pago.status, "approved")
        self.assertEqual(pago.paid_at, AHORA)
        self.assertEqual(pago.period_start, vence)
        self.assertEqual(pago.period_end, vence + relativedelta(months=1))
        self.assertEqual(tenant.plan_iniciado_en, vence)
        self.assertEqual(tenant.plan_vence_en, vence + relativedelta(months=1))
        self.assertEqual(pago.gateway_transaction_id, _tx(pago))

    def test_en_gracia_la_vigencia_nueva_tambien_parte_del_vencimiento_anterior(self):
        vence = AHORA - timedelta(days=2)
        tenant, pago = _escenario(self.db, vence)

        _aplicar(self.db, _evento(pago))
        self.db.expire_all()

        self.assertEqual(self.db.get(SubscriptionPayment, pago.id).period_start, vence)
        self.assertEqual(self.db.get(type(tenant), tenant.id).plan_vence_en, vence + relativedelta(months=1))

    def test_bloqueada_la_vigencia_nueva_parte_de_la_fecha_de_pago(self):
        tenant, pago = _escenario(self.db, AHORA - timedelta(days=30))

        _aplicar(self.db, _evento(pago))
        self.db.expire_all()
        pago, tenant = self.db.get(SubscriptionPayment, pago.id), self.db.get(type(tenant), tenant.id)

        self.assertEqual(pago.period_start, AHORA)
        self.assertEqual(pago.period_end, AHORA + relativedelta(months=1))
        self.assertEqual(tenant.plan_iniciado_en, AHORA)
        self.assertEqual(tenant.plan_vence_en, AHORA + relativedelta(months=1))

    def test_a_119h_59m_59s_de_vencido_aun_es_gracia_y_parte_del_vencimiento_anterior(self):
        vence = AHORA - timedelta(hours=120) + timedelta(seconds=1)
        _, pago = _escenario(self.db, vence)

        _aplicar(self.db, _evento(pago))
        self.db.expire_all()

        self.assertEqual(self.db.get(SubscriptionPayment, pago.id).period_start, vence)

    def test_a_120_horas_exactas_de_vencido_ya_es_bloqueada_y_parte_de_la_fecha_de_pago(self):
        _, pago = _escenario(self.db, AHORA - timedelta(hours=120))

        _aplicar(self.db, _evento(pago))
        self.db.expire_all()

        self.assertEqual(self.db.get(SubscriptionPayment, pago.id).period_start, AHORA)

    def test_el_tenant_queda_activa_y_desbloqueado_al_instante(self):
        tenant, pago = _escenario(self.db, AHORA - timedelta(days=30))
        self.assertEqual(compute_subscription_status(tenant.plan_vence_en, AHORA).state, "bloqueada")

        _aplicar(self.db, _evento(pago))
        self.db.expire_all()
        tenant = self.db.get(type(tenant), tenant.id)

        self.assertEqual(compute_subscription_status(tenant.plan_vence_en, AHORA).state, "activa")
        with mock.patch("app.core.subscription.utc_now_naive", return_value=AHORA):
            enforce_subscription_access(tenant)  # ya no lanza

    def test_aprobar_estando_activa_acumula_un_mes_el_dinero_ya_entro(self):
        """Caso raro aceptado en la spec: el Super Admin renovó a mano mientras el link seguía pendiente."""
        vence = AHORA + timedelta(days=20)
        tenant, pago = _escenario(self.db, vence)

        _aplicar(self.db, _evento(pago))
        self.db.expire_all()

        self.assertEqual(self.db.get(type(tenant), tenant.id).plan_vence_en, vence + relativedelta(months=1))

    def test_solo_cambia_el_tenant_del_pago(self):
        vence = AHORA + timedelta(hours=10)
        _, pago = _escenario(self.db, vence)
        otro, otro_pago = _escenario(self.db, vence)

        _aplicar(self.db, _evento(pago))
        self.db.expire_all()

        self.assertEqual(self.db.get(type(otro), otro.id).plan_vence_en, vence)
        self.assertEqual(self.db.get(SubscriptionPayment, otro_pago.id).status, "pending")

    def test_sin_el_tenant_con_ciclo_mensual_y_vencimiento_el_pago_se_aprueba_sin_tocar_fechas(self):
        """FR-027: ciclo anual o sin vencimiento → aprobado, sin período; lo resuelve soporte."""
        for descripcion, vence, ciclo in (
            ("ciclo anual", AHORA + timedelta(hours=10), "anual"),
            ("sin vencimiento", None, "mensual"),
        ):
            with self.subTest(descripcion):
                tenant, pago = _escenario(self.db, vence, ciclo=ciclo)
                antes = (tenant.plan_iniciado_en, tenant.plan_vence_en)

                self.assertEqual(_aplicar(self.db, _evento(pago)), "processed")
                self.db.expire_all()
                pago, tenant = self.db.get(SubscriptionPayment, pago.id), self.db.get(type(tenant), tenant.id)

                self.assertEqual(pago.status, "approved")
                self.assertEqual(pago.paid_at, AHORA)
                self.assertIsNone(pago.period_start)
                self.assertIsNone(pago.period_end)
                self.assertEqual((tenant.plan_iniciado_en, tenant.plan_vence_en), antes)


# ======================================================================
# Idempotencia (FR-020, SC-005)
# ======================================================================


class IdempotenciaTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()

    def test_el_mismo_evento_repetido_responde_duplicate_y_no_extiende_otra_vez(self):
        vence = AHORA + timedelta(hours=10)
        tenant, pago = _escenario(self.db, vence)
        ev = _evento(pago)

        self.assertEqual(_aplicar(self.db, ev), "processed")
        self.assertEqual(_aplicar(self.db, ev), "duplicate")
        self.db.expire_all()

        self.assertEqual(self.db.get(type(tenant), tenant.id).plan_vence_en, vence + relativedelta(months=1))
        self.assertEqual(_contar(self.db), 1)

    def test_cinco_entregas_extienden_exactamente_un_mes(self):
        vence = AHORA + timedelta(hours=10)
        tenant, pago = _escenario(self.db, vence)
        ev = _evento(pago)

        resultados = [_aplicar(self.db, ev) for _ in range(5)]
        self.db.expire_all()

        self.assertEqual(resultados, ["processed"] + ["duplicate"] * 4)
        self.assertEqual(self.db.get(type(tenant), tenant.id).plan_vence_en, vence + relativedelta(months=1))

    def test_una_segunda_transaccion_aprobada_del_mismo_pago_no_extiende_otra_vez(self):
        vence = AHORA + timedelta(hours=10)
        tenant, pago = _escenario(self.db, vence)
        _aplicar(self.db, _evento(pago, tx_id="TX-1"))

        self.assertEqual(_aplicar(self.db, _evento(pago, tx_id="TX-2")), "duplicate")
        self.db.expire_all()

        self.assertEqual(self.db.get(type(tenant), tenant.id).plan_vence_en, vence + relativedelta(months=1))
        self.assertEqual(self.db.get(SubscriptionPayment, pago.id).gateway_transaction_id, "TX-1")


# ======================================================================
# La matriz estado de Wompi × estado del pago (FR-021, FR-024, FR-025)
# ======================================================================


class MatrizDeEstadosTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.vence = AHORA + timedelta(hours=10)

    def _evento_sobre(self, estado_pago, wompi_status, **kw):
        tenant, pago = _escenario(self.db, self.vence, status=estado_pago)
        resultado = _aplicar(self.db, _evento(pago, status=wompi_status, **kw))
        self.db.expire_all()
        return resultado, self.db.get(SubscriptionPayment, pago.id), self.db.get(type(tenant), tenant.id)

    def test_approved_se_aplica_desde_pending_expired_y_rejected(self):
        for estado in ("pending", "expired", "rejected"):
            with self.subTest(estado):
                resultado, pago, tenant = self._evento_sobre(estado, "APPROVED")

                self.assertEqual(resultado, "processed")
                self.assertEqual(pago.status, "approved")
                self.assertEqual(tenant.plan_vence_en, self.vence + relativedelta(months=1))

    def test_approved_sobre_voided_se_ignora(self):
        resultado, pago, tenant = self._evento_sobre("voided", "APPROVED")

        self.assertEqual(resultado, "ignored")
        self.assertEqual(pago.status, "voided")
        self.assertEqual(tenant.plan_vence_en, self.vence)

    def test_declined_y_error_rechazan_un_pending_sin_tocar_fechas(self):
        for wompi_status in ("DECLINED", "ERROR"):
            with self.subTest(wompi_status):
                resultado, pago, tenant = self._evento_sobre("pending", wompi_status)

                self.assertEqual(resultado, "processed")
                self.assertEqual(pago.status, "rejected")
                self.assertEqual(tenant.plan_vence_en, self.vence)
                self.assertIsNone(pago.paid_at)
                self.assertIsNone(pago.period_start)

    def test_un_rechazo_tardio_nunca_retrocede_un_approved(self):
        for wompi_status in ("DECLINED", "ERROR"):
            with self.subTest(wompi_status):
                resultado, pago, _ = self._evento_sobre("approved", wompi_status)

                self.assertEqual(resultado, "ignored")
                self.assertEqual(pago.status, "approved")

    def test_declined_solo_actua_sobre_pending(self):
        for estado in ("expired", "rejected", "voided"):
            with self.subTest(estado):
                resultado, pago, _ = self._evento_sobre(estado, "DECLINED")

                self.assertEqual(resultado, "ignored")
                self.assertEqual(pago.status, estado)

    def test_voided_tras_approved_registra_la_anulacion_sin_revertir_fechas(self):
        tenant, pago = _escenario(self.db, self.vence)
        _aplicar(self.db, _evento(pago, tx_id="TX-1"))
        extendida = self.vence + relativedelta(months=1)

        resultado = _aplicar(self.db, _evento(pago, status="VOIDED", tx_id="TX-1"))
        self.db.expire_all()

        self.assertEqual(resultado, "processed")
        self.assertEqual(self.db.get(SubscriptionPayment, pago.id).status, "voided")
        self.assertEqual(self.db.get(type(tenant), tenant.id).plan_vence_en, extendida)  # lo resuelve soporte a mano

    def test_voided_solo_actua_sobre_approved(self):
        for estado in ("pending", "expired", "rejected"):
            with self.subTest(estado):
                resultado, pago, _ = self._evento_sobre(estado, "VOIDED")

                self.assertEqual(resultado, "ignored")
                self.assertEqual(pago.status, estado)

    def test_pending_y_estados_desconocidos_se_ignoran(self):
        for wompi_status in ("PENDING", "ALGO_NUEVO", ""):
            with self.subTest(wompi_status):
                resultado, pago, tenant = self._evento_sobre("pending", wompi_status)

                self.assertEqual(resultado, "ignored")
                self.assertEqual(pago.status, "pending")
                self.assertEqual(tenant.plan_vence_en, self.vence)

    def test_el_estado_de_wompi_se_lee_sin_distinguir_mayusculas(self):
        resultado, pago, _ = self._evento_sobre("pending", "approved")

        self.assertEqual(resultado, "processed")
        self.assertEqual(pago.status, "approved")

    def test_un_reintento_aprobado_tras_un_rechazo_con_otra_transaccion_se_aplica(self):
        tenant, pago = _escenario(self.db, self.vence)
        _aplicar(self.db, _evento(pago, status="DECLINED", tx_id="TX-1"))

        resultado = _aplicar(self.db, _evento(pago, status="APPROVED", tx_id="TX-2"))
        self.db.expire_all()

        self.assertEqual(resultado, "processed")
        self.assertEqual(self.db.get(SubscriptionPayment, pago.id).status, "approved")
        self.assertEqual(self.db.get(SubscriptionPayment, pago.id).gateway_transaction_id, "TX-2")
        self.assertEqual(self.db.get(type(tenant), tenant.id).plan_vence_en, self.vence + relativedelta(months=1))


# ======================================================================
# Eventos sin efecto (FR-026): nunca cambian nada y nunca se responde con error
# ======================================================================


class SinEfectoTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.vence = AHORA + timedelta(hours=10)
        self.tenant, self.pago = _escenario(self.db, self.vence)

    def _sin_cambios(self):
        self.db.expire_all()
        self.assertEqual(self.db.get(SubscriptionPayment, self.pago.id).status, "pending")
        self.assertEqual(self.db.get(type(self.tenant), self.tenant.id).plan_vence_en, self.vence)

    def test_referencia_desconocida(self):
        self.assertEqual(_aplicar(self.db, _evento(self.pago, reference="SKP-" + "0" * 32)), "ignored")
        self._sin_cambios()
        self.assertEqual(_contar(self.db), 1)

    def test_evento_sin_referencia(self):
        ev = _evento(self.pago)
        del ev["data"]["transaction"]["reference"]

        self.assertEqual(_aplicar(self.db, ev), "ignored")
        self._sin_cambios()

    def test_monto_distinto(self):
        self.assertEqual(_aplicar(self.db, _evento(self.pago, cents=CENTAVOS - 1)), "ignored")
        self._sin_cambios()

    def test_moneda_distinta(self):
        self.assertEqual(_aplicar(self.db, _evento(self.pago, currency="USD")), "ignored")
        self._sin_cambios()

    def test_transaccion_ya_ligada_a_otro_pago(self):
        _, otro = _escenario(self.db, self.vence, status="approved", gateway_transaction_id="TX-AJENA")

        self.assertEqual(_aplicar(self.db, _evento(self.pago, tx_id="TX-AJENA")), "ignored")
        self._sin_cambios()
        self.assertEqual(self.db.get(SubscriptionPayment, otro.id).gateway_transaction_id, "TX-AJENA")

    def test_la_advertencia_no_incluye_el_cuerpo_completo_ni_secretos(self):
        with self.assertLogs("app.api.v1.plan.subscription_service", level="WARNING") as logs:
            _aplicar(self.db, _evento(self.pago, cents=1))

        texto = "\n".join(logs.output)
        self.assertIn(self.pago.gateway_reference, texto)
        self.assertIn(_tx(self.pago), texto)
        self.assertNotIn(SECRETO, texto)
        self.assertNotIn(_checksum(_evento(self.pago, cents=1)), texto.lower())


# ======================================================================
# Atomicidad (FR-023)
# ======================================================================


class AtomicidadTests(unittest.TestCase):
    def test_si_falla_el_recalculo_el_pago_sigue_pending_y_el_tenant_intacto(self):
        db = fx.new_session()
        vence = AHORA + timedelta(hours=10)
        tenant, pago = _escenario(db, vence)

        with mock.patch.object(service, "calculate_plan_vencimiento", side_effect=RuntimeError("falla")):
            with self.assertRaises(RuntimeError):
                _aplicar(db, _evento(pago))
        db.expire_all()

        self.assertEqual(db.get(SubscriptionPayment, pago.id).status, "pending")
        self.assertIsNone(db.get(SubscriptionPayment, pago.id).paid_at)
        self.assertEqual(db.get(type(tenant), tenant.id).plan_vence_en, vence)

    def test_despues_de_un_fallo_el_mismo_evento_se_puede_procesar(self):
        db = fx.new_session()
        vence = AHORA + timedelta(hours=10)
        tenant, pago = _escenario(db, vence)
        ev = _evento(pago)
        with mock.patch.object(service, "calculate_plan_vencimiento", side_effect=RuntimeError("falla")):
            with self.assertRaises(RuntimeError):
                _aplicar(db, ev)

        self.assertEqual(_aplicar(db, ev), "processed")  # el reintento de Wompi funciona
        db.expire_all()
        self.assertEqual(db.get(type(tenant), tenant.id).plan_vence_en, vence + relativedelta(months=1))


# ======================================================================
# POST /api/v1/webhooks/wompi por HTTP
# ======================================================================


def _aplicacion_real():
    # `app.main` ejecuta `create_app()` al importarse y esta llama a `initialize_database()`, que abre
    # PostgreSQL; se sustituye por una función vacía ANTES de importar (mismo motivo que
    # `test_module_not_in_plan_error.py`).
    with mock.patch("app.core.db.initialize_database", lambda *a, **k: None):
        from app.main import app
    return app


class WebhookHttpTests(unittest.TestCase):
    URL = "/api/v1/webhooks/wompi"

    def setUp(self):
        self.app = _aplicacion_real()
        self.db = fx.new_session(multithreaded=True)  # TestClient atiende el endpoint en otro hilo
        self.app.dependency_overrides[get_shared_db] = lambda: self.db
        self.addCleanup(self.app.dependency_overrides.clear)
        patcher = mock.patch.multiple(settings, WOMPI_EVENTS_SECRET=SECRETO)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.vence = utc_now_naive() + timedelta(hours=10)  # por_vencer con el reloj real
        self.tenant, self.pago = _escenario(self.db, self.vence)

    def _post(self, ev, *, headers=None):
        import json

        return self.client.post(self.URL, content=json.dumps(ev), headers={"content-type": "application/json", **(headers or {})})

    def test_aprobado_responde_200_processed_sin_tenant_ni_token(self):
        resp = self._post(_evento(self.pago))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "processed"})
        self.db.expire_all()
        self.assertEqual(self.db.get(SubscriptionPayment, self.pago.id).status, "approved")

    def test_repetido_responde_200_duplicate_para_que_wompi_no_reintente(self):
        ev = _evento(self.pago)
        self._post(ev)

        resp = self._post(ev)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "duplicate"})

    def test_sin_efecto_responde_200_ignored(self):
        for descripcion, ev in (
            ("referencia desconocida", _evento(self.pago, reference="SKP-" + "0" * 32)),
            ("monto distinto", _evento(self.pago, cents=1)),
            ("otro tipo de evento", _evento(self.pago, event="nequi_token.updated")),
        ):
            with self.subTest(descripcion):
                resp = self._post(ev)

                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json(), {"status": "ignored"})

    def test_checksum_alterado_es_401_y_no_cambia_nada(self):
        ev = _evento(self.pago)
        ev["signature"]["checksum"] = "0" * 64

        resp = self._post(ev)

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json(), {"detail": "Firma inválida"})
        self.db.expire_all()
        self.assertEqual(self.db.get(SubscriptionPayment, self.pago.id).status, "pending")

    def test_la_cabecera_x_event_checksum_si_llega_debe_coincidir(self):
        ev = _evento(self.pago)

        self.assertEqual(self._post(ev, headers={"X-Event-Checksum": "f" * 64}).status_code, 401)
        self.assertEqual(self._post(ev, headers={"X-Event-Checksum": _checksum(ev)}).status_code, 200)

    def test_sin_secreto_configurado_todo_evento_es_401(self):
        with mock.patch.multiple(settings, WOMPI_EVENTS_SECRET=None):
            resp = self._post(_evento(self.pago))

        self.assertEqual(resp.status_code, 401)
        self.db.expire_all()
        self.assertEqual(self.db.get(SubscriptionPayment, self.pago.id).status, "pending")

    def test_cuerpo_ilegible_o_que_no_es_un_objeto_es_400(self):
        for descripcion, cuerpo in (("no es JSON", b"esto no es json"), ("vacío", b""), ("lista", b"[1, 2]"), ("texto", b'"hola"')):
            with self.subTest(descripcion):
                resp = self.client.post(self.URL, content=cuerpo, headers={"content-type": "application/json"})

                self.assertEqual(resp.status_code, 400)

    def test_estructura_incompleta_y_properties_insuficientes_son_400(self):
        sin_firma = _evento(self.pago)
        del sin_firma["signature"]
        solo_id = _evento(self.pago, properties=["transaction.id"])

        self.assertEqual(self._post(sin_firma).status_code, 400)
        self.assertEqual(self._post(solo_id).status_code, 400)

    def test_un_evento_transaction_updated_sin_transaccion_es_400(self):
        ev = _evento(self.pago)
        ev["data"] = {"otra_cosa": {}}
        # Va firmado con properties que ya no existen en data -> estructura inválida.
        self.assertEqual(self._post(ev).status_code, 400)

    def test_no_acepta_get(self):
        self.assertEqual(self.client.get(self.URL).status_code, 405)

    def test_un_tenant_bloqueado_que_paga_queda_desbloqueado_en_la_peticion_siguiente(self):
        """El caso que importa de punta a punta (SC-004): el webhook no pasa por el guard, aprueba el pago y la
        siguiente petición del tenant ya no recibe `403 subscription_blocked`, sin volver a iniciar sesión."""
        self.tenant.plan_vence_en = utc_now_naive() - timedelta(days=30)  # bloqueada
        self.db.commit()
        cajero = fx.make_user(self.db, self.tenant, role_name="CASHIER")
        self.db.commit()
        self.app.dependency_overrides[get_db] = lambda: self.db
        self.app.dependency_overrides[get_current_user] = lambda: cajero
        headers = {"x-tenant-host": "heladeria.skeilopos.com"}

        with mock.patch("app.core.db.find_tenant_by_host", return_value=self.tenant):
            antes = self.client.get("/api/v1/tenant", headers=headers)
            webhook = self._post(_evento(self.pago))  # sin x-tenant-host: no resuelve tenant
            self.db.expire_all()
            despues = self.client.get("/api/v1/tenant", headers=headers)

        self.assertEqual(antes.status_code, 403)
        self.assertEqual(antes.json()["code"], "subscription_blocked")
        self.assertEqual(webhook.status_code, 200)
        self.assertNotEqual(despues.status_code, 403)

    def test_el_webhook_funciona_aunque_el_tenant_este_bloqueado(self):
        self.tenant.plan_vence_en = utc_now_naive() - timedelta(days=30)
        self.db.commit()

        resp = self._post(_evento(self.pago))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "processed"})


if __name__ == "__main__":
    unittest.main()
