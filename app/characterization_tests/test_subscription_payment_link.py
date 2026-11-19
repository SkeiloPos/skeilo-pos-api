"""Tests de la nueva funcionalidad — spec 161-suscripcion-pago-wompi, Historia de Usuario 3
(FR-011 a FR-018, SC-007; contrato `subscription-api.md § 2`): el ADMIN pide un link de pago de Wompi
por el valor mensual del plan.

No son characterization tests — comportamiento enteramente nuevo. Dos bloques:

- `get_or_create_payment_link` llamado directamente (como el resto de este paquete): creación,
  reutilización del pendiente vigente, expiración, los cuatro 409, el 503 y la carrera entre dos solicitudes;
- la aplicación real por HTTP (`TestClient` con `dependency_overrides`, patrón de `test_bootstrap.py`): los
  códigos y cuerpos exactos del contrato.

La concurrencia REAL (`SELECT … FOR UPDATE` y el índice parcial bajo PostgreSQL) no cabe en SQLite: se
verifica con `quickstart.md § 4`.

    python -m unittest app.characterization_tests.test_subscription_payment_link -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock
from urllib.parse import parse_qsl, urlsplit

from fastapi import HTTPException
from sqlalchemy import func, select
from starlette.testclient import TestClient

from app.api.v1.plan import subscription_service as service
from app.characterization_tests import fixtures  # noqa: F401  - variables de entorno inertes antes de importar app.main
from app.characterization_tests import plan_fixtures as fx
from app.core import wompi
from app.core.config import settings
from app.core.db import get_db, get_tenant
from app.core.dependencies import get_current_user, require_tenant_admin
from app.core.exceptions import PaymentGatewayNotConfiguredError, SubscriptionNotPayableError
from app.core.timezone import utc_now_naive
from app.models.subscription_payment import SubscriptionPayment

AHORA = datetime(2026, 12, 6, 12, 0, 0)
PRECIO = Decimal("89900.00")
LLAVES = dict(WOMPI_PUBLIC_KEY="pub_test_abc", WOMPI_INTEGRITY_SECRET="test_integrity_xyz")
SIN_LLAVES = dict(WOMPI_PUBLIC_KEY=None, WOMPI_INTEGRITY_SECRET=None)


def _con_llaves(testcase, **cfg):
    patcher = mock.patch.multiple(settings, **(cfg or LLAVES))
    patcher.start()
    testcase.addCleanup(patcher.stop)


def _contar(db, *where) -> int:
    stmt = select(func.count()).select_from(SubscriptionPayment)
    if where:
        stmt = stmt.where(*where)
    return db.execute(stmt).scalar_one()


def _escenario(db, vence, *, ciclo="mensual", precio=PRECIO, rol="ADMIN"):
    """Tenant con el vencimiento dado (`None` = sin vencimiento) y un usuario del rol dado."""
    plan = fx.make_plan(db, precio_mensual=precio)
    tenant = fx.make_tenant(db, plan=plan, ciclo_facturacion=ciclo if vence is not None else None)
    if vence is not None:
        tenant.plan_iniciado_en = vence - timedelta(days=30)
        tenant.plan_vence_en = vence
    user = fx.make_user(db, tenant, role_name=rol)
    db.commit()
    return plan, tenant, user


# ======================================================================
# El servicio
# ======================================================================


class CreacionDelLinkTests(unittest.TestCase):
    def setUp(self):
        _con_llaves(self)
        self.db = fx.new_session()

    def test_crea_un_pago_pending_por_el_precio_mensual_en_cada_estado_cobrable(self):
        for descripcion, vence in (
            ("por_vencer", AHORA + timedelta(hours=47)),
            ("en_gracia", AHORA - timedelta(hours=2)),
            ("bloqueada", AHORA - timedelta(days=30)),
        ):
            with self.subTest(descripcion):
                plan, tenant, user = _escenario(self.db, vence)

                pago, reused = service.get_or_create_payment_link(self.db, tenant, user, now=AHORA)

                self.assertFalse(reused)
                self.assertEqual(pago.status, "pending")
                self.assertEqual(pago.amount, PRECIO)
                self.assertEqual(pago.tenant_id, tenant.id)
                self.assertEqual(pago.plan_id, plan.id)
                self.assertEqual(pago.ciclo_facturacion, "mensual")
                self.assertEqual(pago.gateway, "wompi")
                self.assertEqual(pago.requested_by_user_id, user.id)
                self.assertEqual(pago.payment_link_expires_at, AHORA + timedelta(hours=24))
                self.assertIsNone(pago.paid_at)
                self.assertIsNone(pago.period_start)
                self.assertIsNone(pago.period_end)

    def test_la_referencia_es_la_del_pago_y_la_url_esta_firmada_con_el_secreto_de_integridad(self):
        _, tenant, user = _escenario(self.db, AHORA + timedelta(hours=10))

        pago, _ = service.get_or_create_payment_link(self.db, tenant, user, now=AHORA)

        self.assertEqual(pago.gateway_reference, wompi.build_reference(pago.id))
        partes = urlsplit(pago.payment_link_url)
        params = dict(parse_qsl(partes.query))
        self.assertEqual(f"{partes.scheme}://{partes.netloc}{partes.path}", "https://checkout.wompi.co/p/")
        self.assertEqual(params["public-key"], "pub_test_abc")
        self.assertEqual(params["currency"], "COP")
        self.assertEqual(params["reference"], pago.gateway_reference)
        self.assertEqual(params["amount-in-cents"], "8990000")
        self.assertEqual(params["expiration-time"], "2026-12-07T12:00:00.000Z")
        self.assertEqual(params["redirect-url"], f"https://{tenant.host}/")
        self.assertEqual(
            params["signature:integrity"],
            wompi.integrity_signature(pago.gateway_reference, 8990000, "COP", "2026-12-07T12:00:00.000Z", "test_integrity_xyz"),
        )

    def test_el_secreto_de_integridad_nunca_aparece_en_la_url_ni_en_el_pago(self):
        _, tenant, user = _escenario(self.db, AHORA + timedelta(hours=10))

        pago, _ = service.get_or_create_payment_link(self.db, tenant, user, now=AHORA)

        for campo in ("payment_link_url", "gateway_reference", "gateway", "status"):
            self.assertNotIn("test_integrity_xyz", str(getattr(pago, campo)))

    def test_el_redirect_sale_de_la_plantilla_configurada(self):
        _, tenant, user = _escenario(self.db, AHORA + timedelta(hours=10))

        with mock.patch.object(settings, "WOMPI_REDIRECT_URL_TEMPLATE", "https://{host}/plan/gracias"):
            pago, _ = service.get_or_create_payment_link(self.db, tenant, user, now=AHORA)

        params = dict(parse_qsl(urlsplit(pago.payment_link_url).query))
        self.assertEqual(params["redirect-url"], f"https://{tenant.host}/plan/gracias")

    def test_la_vigencia_sale_de_la_configuracion(self):
        _, tenant, user = _escenario(self.db, AHORA + timedelta(hours=10))

        with mock.patch.object(settings, "WOMPI_LINK_TTL_HOURS", 2):
            pago, _ = service.get_or_create_payment_link(self.db, tenant, user, now=AHORA)

        self.assertEqual(pago.payment_link_expires_at, AHORA + timedelta(hours=2))

    def test_el_pago_queda_confirmado_en_la_base(self):
        _, tenant, user = _escenario(self.db, AHORA + timedelta(hours=10))

        pago, _ = service.get_or_create_payment_link(self.db, tenant, user, now=AHORA)
        self.db.expire_all()

        self.assertEqual(_contar(self.db, SubscriptionPayment.id == pago.id), 1)


class ReutilizacionTests(unittest.TestCase):
    def setUp(self):
        _con_llaves(self)
        self.db = fx.new_session()
        _, self.tenant, self.user = _escenario(self.db, AHORA + timedelta(hours=10))

    def test_la_segunda_solicitud_devuelve_el_mismo_link_sin_crear_otro_pago(self):
        primero, reused1 = service.get_or_create_payment_link(self.db, self.tenant, self.user, now=AHORA)
        segundo, reused2 = service.get_or_create_payment_link(self.db, self.tenant, self.user, now=AHORA + timedelta(hours=1))

        self.assertFalse(reused1)
        self.assertTrue(reused2)
        self.assertEqual(segundo.id, primero.id)
        self.assertEqual(segundo.payment_link_url, primero.payment_link_url)
        self.assertEqual(_contar(self.db), 1)

    def test_un_link_vencido_se_marca_expired_y_se_crea_uno_nuevo(self):
        viejo, _ = service.get_or_create_payment_link(self.db, self.tenant, self.user, now=AHORA)

        nuevo, reused = service.get_or_create_payment_link(self.db, self.tenant, self.user, now=AHORA + timedelta(hours=25))
        self.db.expire_all()

        self.assertFalse(reused)
        self.assertNotEqual(nuevo.id, viejo.id)
        self.assertEqual(self.db.get(SubscriptionPayment, viejo.id).status, "expired")
        self.assertEqual(nuevo.status, "pending")
        self.assertEqual(_contar(self.db, SubscriptionPayment.status == "pending"), 1)
        self.assertEqual(_contar(self.db), 2)

    def test_el_link_justo_en_el_instante_de_expirar_ya_no_se_reutiliza(self):
        viejo, _ = service.get_or_create_payment_link(self.db, self.tenant, self.user, now=AHORA)

        nuevo, reused = service.get_or_create_payment_link(self.db, self.tenant, self.user, now=AHORA + timedelta(hours=24))

        self.assertFalse(reused)
        self.assertNotEqual(nuevo.id, viejo.id)

    def test_un_pago_rechazado_no_se_reutiliza_y_permite_pedir_uno_nuevo(self):
        viejo, _ = service.get_or_create_payment_link(self.db, self.tenant, self.user, now=AHORA)
        viejo.status = "rejected"
        self.db.commit()

        nuevo, reused = service.get_or_create_payment_link(self.db, self.tenant, self.user, now=AHORA + timedelta(minutes=5))

        self.assertFalse(reused)
        self.assertNotEqual(nuevo.id, viejo.id)

    def test_cada_tenant_tiene_su_propio_pendiente(self):
        service.get_or_create_payment_link(self.db, self.tenant, self.user, now=AHORA)
        _, otro, otro_admin = _escenario(self.db, AHORA + timedelta(hours=10))

        pago, reused = service.get_or_create_payment_link(self.db, otro, otro_admin, now=AHORA)

        self.assertFalse(reused)
        self.assertEqual(pago.tenant_id, otro.id)
        self.assertEqual(_contar(self.db, SubscriptionPayment.status == "pending"), 2)

    def test_carrera_si_otra_solicitud_gano_el_indice_parcial_resuelve_con_el_mismo_pago(self):
        """Dos ADMIN pulsan «Pagar ahora» a la vez: ambos ven que no hay pendiente. El segundo `INSERT` choca con
        `ux__subscription_payments__tenant_id__pending`; el servicio lo atrapa y devuelve el pago ganador."""
        ganador, _ = service.get_or_create_payment_link(self.db, self.tenant, self.user, now=AHORA)
        busquedas = iter([None, ganador])  # la primera búsqueda «no ve» al ganador; la de recuperación sí

        with mock.patch.object(service, "_find_pending", side_effect=lambda *a, **k: next(busquedas)):
            pago, reused = service.get_or_create_payment_link(self.db, self.tenant, self.user, now=AHORA)

        self.assertTrue(reused)
        self.assertEqual(pago.id, ganador.id)
        self.assertEqual(_contar(self.db, SubscriptionPayment.status == "pending"), 1)


class CasosNoCobrablesTests(unittest.TestCase):
    """FR-012/FR-013: conflicto controlado y NINGÚN pago creado."""

    def setUp(self):
        _con_llaves(self)
        self.db = fx.new_session()

    def _rechaza(self, vence, razon, **kw):
        _, tenant, user = _escenario(self.db, vence, **kw)
        with self.assertRaises(SubscriptionNotPayableError) as ctx:
            service.get_or_create_payment_link(self.db, tenant, user, now=AHORA)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.reason, razon)
        self.assertEqual(_contar(self.db), 0)

    def test_activa_no_puede_pagar_por_adelantado(self):
        self._rechaza(AHORA + timedelta(hours=49), "already_active")

    def test_activa_con_el_vencimiento_lejano_tampoco(self):
        self._rechaza(AHORA + timedelta(days=25), "already_active")

    def test_el_borde_de_48_horas_ya_es_por_vencer_y_si_puede(self):
        _, tenant, user = _escenario(self.db, AHORA + timedelta(hours=48))

        pago, _ = service.get_or_create_payment_link(self.db, tenant, user, now=AHORA)

        self.assertEqual(pago.status, "pending")

    def test_sin_vencimiento(self):
        self._rechaza(None, "no_expiry")

    def test_ciclo_anual(self):
        self._rechaza(AHORA + timedelta(hours=10), "unsupported_cycle", ciclo="anual")

    def test_plan_sin_precio_mensual(self):
        self._rechaza(AHORA + timedelta(hours=10), "no_price", precio=None)

    def test_un_pendiente_previo_no_salva_a_un_tenant_activa(self):
        """Si el tenant ya pagó (queda `activa`), el pendiente que le quedara no se reutiliza: ya no es pagable."""
        _, tenant, user = _escenario(self.db, AHORA + timedelta(hours=10))
        service.get_or_create_payment_link(self.db, tenant, user, now=AHORA)
        tenant.plan_vence_en = AHORA + timedelta(days=30)  # renovación aprobada
        self.db.commit()

        with self.assertRaises(SubscriptionNotPayableError) as ctx:
            service.get_or_create_payment_link(self.db, tenant, user, now=AHORA + timedelta(hours=1))
        self.assertEqual(ctx.exception.reason, "already_active")


class SinPasarelaTests(unittest.TestCase):
    """FR-017: sin credenciales, 503 controlado y NINGÚN pago pendiente."""

    def setUp(self):
        self.db = fx.new_session()

    def test_sin_ninguna_credencial(self):
        _con_llaves(self, **SIN_LLAVES)
        _, tenant, user = _escenario(self.db, AHORA + timedelta(hours=10))

        with self.assertRaises(PaymentGatewayNotConfiguredError) as ctx:
            service.get_or_create_payment_link(self.db, tenant, user, now=AHORA)

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(_contar(self.db), 0)

    def test_faltando_solo_el_secreto_de_integridad(self):
        _con_llaves(self, WOMPI_PUBLIC_KEY="pub_test_abc", WOMPI_INTEGRITY_SECRET="")
        _, tenant, user = _escenario(self.db, AHORA + timedelta(hours=10))

        with self.assertRaises(PaymentGatewayNotConfiguredError):
            service.get_or_create_payment_link(self.db, tenant, user, now=AHORA)
        self.assertEqual(_contar(self.db), 0)

    def test_el_secreto_de_eventos_no_hace_falta_para_pedir_un_link(self):
        _con_llaves(self, **LLAVES, WOMPI_EVENTS_SECRET=None)
        _, tenant, user = _escenario(self.db, AHORA + timedelta(hours=10))

        pago, _ = service.get_or_create_payment_link(self.db, tenant, user, now=AHORA)

        self.assertEqual(pago.status, "pending")

    def test_sin_pasarela_gana_sobre_el_conflicto_de_estado(self):
        """Orden del contrato: llaves (503) antes que estado/ciclo/precio (409)."""
        _con_llaves(self, **SIN_LLAVES)
        _, tenant, user = _escenario(self.db, AHORA + timedelta(days=25))  # activa

        with self.assertRaises(PaymentGatewayNotConfiguredError):
            service.get_or_create_payment_link(self.db, tenant, user, now=AHORA)

    def test_un_pendiente_ya_existente_no_se_toca_si_luego_faltan_las_llaves(self):
        with mock.patch.multiple(settings, **LLAVES):
            _, tenant, user = _escenario(self.db, AHORA + timedelta(hours=10))
            existente, _ = service.get_or_create_payment_link(self.db, tenant, user, now=AHORA)
        _con_llaves(self, **SIN_LLAVES)

        with self.assertRaises(PaymentGatewayNotConfiguredError):
            service.get_or_create_payment_link(self.db, tenant, user, now=AHORA)

        self.assertEqual(self.db.get(SubscriptionPayment, existente.id).status, "pending")


class SoloAdminTests(unittest.TestCase):
    def test_los_demas_roles_reciben_403_de_require_tenant_admin(self):
        db = fx.new_session()
        for rol in ("CASHIER", "MESERO", "KITCHEN"):
            with self.subTest(rol):
                _, _, user = _escenario(db, AHORA + timedelta(hours=10), rol=rol)
                with self.assertRaises(HTTPException) as ctx:
                    require_tenant_admin(user=user)
                self.assertEqual(ctx.exception.status_code, 403)

    def test_el_admin_pasa(self):
        db = fx.new_session()
        _, _, user = _escenario(db, AHORA + timedelta(hours=10), rol="ADMIN")

        self.assertIs(require_tenant_admin(user=user), user)


# ======================================================================
# El contrato HTTP
# ======================================================================


def _aplicacion_real():
    # `app.main` ejecuta `create_app()` al importarse y esta llama a `initialize_database()`, que abre
    # PostgreSQL; se sustituye por una función vacía ANTES de importar (mismo motivo que
    # `test_module_not_in_plan_error.py`).
    with mock.patch("app.core.db.initialize_database", lambda *a, **k: None):
        from app.main import app
    return app


class PaymentLinkHttpTests(unittest.TestCase):
    URL = "/api/v1/plan/subscription/payment-link"

    def setUp(self):
        self.app = _aplicacion_real()
        self.db = fx.new_session(multithreaded=True)  # TestClient atiende el endpoint en otro hilo
        self.addCleanup(self.app.dependency_overrides.clear)

    def _cliente(self, tenant, user) -> TestClient:
        self.app.dependency_overrides[get_db] = lambda: self.db
        self.app.dependency_overrides[get_tenant] = lambda: tenant
        self.app.dependency_overrides[get_current_user] = lambda: user
        return TestClient(self.app, raise_server_exceptions=False)

    def test_201_con_el_cuerpo_del_contrato_y_200_al_reutilizar(self):
        _con_llaves(self)
        vence = utc_now_naive() + timedelta(hours=10)  # por_vencer con el reloj real
        _, tenant, admin = _escenario(self.db, vence)
        cliente = self._cliente(tenant, admin)

        resp = cliente.post(self.URL)

        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(
            set(body),
            {"payment_id", "status", "amount", "currency", "gateway_reference", "payment_link_url",
             "payment_link_expires_at", "reused"},
        )
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["amount"], "89900.00")
        self.assertEqual(body["currency"], "COP")
        self.assertIs(body["reused"], False)
        self.assertTrue(body["gateway_reference"].startswith("SKP-"))
        self.assertTrue(body["payment_link_url"].startswith("https://checkout.wompi.co/p/?"))
        self.assertTrue(body["payment_link_expires_at"].endswith("+00:00"))

        again = cliente.post(self.URL)

        self.assertEqual(again.status_code, 200)
        self.assertIs(again.json()["reused"], True)
        self.assertEqual(again.json()["payment_id"], body["payment_id"])
        self.assertEqual(_contar(self.db), 1)

    def test_403_para_un_rol_que_no_es_admin(self):
        _con_llaves(self)
        _, tenant, cajero = _escenario(self.db, utc_now_naive() + timedelta(hours=10), rol="CASHIER")

        resp = self._cliente(tenant, cajero).post(self.URL)

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json(), {"detail": "Tenant admin access required"})
        self.assertEqual(_contar(self.db), 0)

    def test_409_con_code_y_reason_para_un_tenant_al_dia(self):
        _con_llaves(self)
        _, tenant, admin = _escenario(self.db, utc_now_naive() + timedelta(days=25))

        resp = self._cliente(tenant, admin).post(self.URL)

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(
            resp.json(),
            {
                "detail": "Tu suscripción está al día; podrás pagar la siguiente cuando falten 48 horas para vencer.",
                "code": "subscription_not_payable",
                "reason": "already_active",
            },
        )

    def test_409_para_ciclo_anual_sin_vencimiento_y_sin_precio(self):
        _con_llaves(self)
        for razon, vence, kw in (
            ("unsupported_cycle", utc_now_naive() + timedelta(hours=10), {"ciclo": "anual"}),
            ("no_expiry", None, {}),
            ("no_price", utc_now_naive() + timedelta(hours=10), {"precio": None}),
        ):
            with self.subTest(razon):
                _, tenant, admin = _escenario(self.db, vence, **kw)

                resp = self._cliente(tenant, admin).post(self.URL)

                self.assertEqual(resp.status_code, 409)
                self.assertEqual(resp.json()["reason"], razon)

    def test_503_con_code_sin_credenciales_y_sin_ninguna_fila(self):
        _con_llaves(self, **SIN_LLAVES)
        _, tenant, admin = _escenario(self.db, utc_now_naive() + timedelta(hours=10))

        resp = self._cliente(tenant, admin).post(self.URL)

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(set(resp.json()), {"detail", "code"})
        self.assertEqual(resp.json()["code"], "payment_gateway_not_configured")
        self.assertEqual(_contar(self.db), 0)

    def test_el_endpoint_no_acepta_get(self):
        _, tenant, admin = _escenario(self.db, utc_now_naive() + timedelta(hours=10))

        self.assertEqual(self._cliente(tenant, admin).get(self.URL).status_code, 405)


if __name__ == "__main__":
    unittest.main()
