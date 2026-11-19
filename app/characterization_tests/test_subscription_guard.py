"""Tests de la nueva funcionalidad — spec 161-suscripcion-pago-wompi, Historia de Usuario 2
(FR-005 a FR-010, SC-002, SC-003; A-161, A-162; contrato `blocked-access.md`): con la suscripción
`bloqueada` TODA operación del tenant —personal y comensal por QR— se rechaza con
`403 subscription_blocked`, salvo las rutas exentas; durante la gracia nada se bloquea.

No son characterization tests — comportamiento enteramente nuevo. Cuatro bloques:

- la regla pura de exención por ruta;
- `enforce_subscription_access` y sus fronteras exactas (reloj fijado);
- los dos puntos por donde pasa todo tenant: `get_tenant` (cabecera `x-tenant-host`) y
  `resolve_tenant_by_id` (comensal con token), y que `get_optional_tenant` no bloquea;
- la aplicación real por HTTP: una ruta bloqueada frente a una exenta.

    python -m unittest app.characterization_tests.test_subscription_guard -v
"""
from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException
from starlette.requests import Request
from starlette.testclient import TestClient

from app.characterization_tests import fixtures  # noqa: F401  - variables de entorno inertes antes de importar app.*
from app.characterization_tests import plan_fixtures as fx
from app.core.dependencies import get_current_user
from app.core.db import get_db, get_tenant, resolve_tenant_by_id
from app.core.exceptions import SubscriptionBlockedError
from app.core.subscription_guard import EXEMPT_PREFIXES, enforce_subscription_access, is_exempt_path

#: Instante fijo para probar las fronteras exactas sin depender del reloj.
AHORA = datetime(2026, 12, 6, 12, 0, 0)


def _request(path_format: str, *, method: str = "GET") -> Request:
    """Una `Request` mínima con la ruta ya resuelta, como la deja Starlette antes de correr las dependencias."""
    scope = {
        "type": "http",
        "method": method,
        "path": f"/api/v1{path_format}",
        "headers": [],
        "route": SimpleNamespace(path_format=f"/api/v1{path_format}"),
    }
    return Request(scope)


def _tenant(vence):
    return SimpleNamespace(plan_vence_en=vence, id=1)


def _reloj(testcase, instante=AHORA):
    patcher = mock.patch("app.core.subscription.utc_now_naive", return_value=instante)
    patcher.start()
    testcase.addCleanup(patcher.stop)


# ======================================================================
# La regla de exención
# ======================================================================


class ExencionPorRutaTests(unittest.TestCase):
    def test_los_prefijos_exentos_son_los_del_contrato(self):
        self.assertEqual(
            set(EXEMPT_PREFIXES),
            {"/auth", "/bootstrap", "/plan", "/notifications", "/realtime", "/legal/platform-acceptance"},
        )

    def test_rutas_exentas(self):
        for path in (
            "/auth/login", "/auth/refresh-token", "/auth/logout", "/auth/forgot-password", "/auth/change-password",
            "/bootstrap",
            "/plan", "/plan/subscription/payment-link", "/plan/subscription/payments",
            "/notifications", "/notifications/{notification_id}/attend", "/notifications/push/subscriptions",
            "/realtime/ticket",
            # spec 162: la aceptación de documentos de plataforma sigue disponible con el tenant bloqueado.
            "/legal/platform-acceptance/pending", "/legal/platform-acceptance/accept",
        ):
            with self.subTest(path):
                self.assertTrue(is_exempt_path(path))

    def test_rutas_bloqueadas(self):
        for path in (
            "/orders", "/orders/{order_id}", "/sales", "/cash/shifts", "/products", "/categories", "/inventory/items",
            "/promotions", "/reports/sales", "/users", "/invitations", "/tenant", "/uploads/presign", "/audit-logs",
            "/menu", "/cart/submit", "/table-sessions/{id}/bill", "/invoices", "/catalog",
            "/unit-measures", "/presentations",
            # spec 162: el resto de /legal es configuración del negocio y SÍ se bloquea.
            "/legal/tenant-profile", "/legal/tenant-document/publish", "/legal/tenant-document/current",
            "/legal", "/legal/platform-acceptances", "/legal/platform-acceptance-x",
        ):
            with self.subTest(path):
                self.assertFalse(is_exempt_path(path))

    def test_la_exencion_es_por_segmento_y_no_por_texto(self):
        """`/planes` o `/plan-algo` no son `/plan`; `/authors` no es `/auth`."""
        for path in ("/planes", "/plan-algo", "/authors", "/notifications-extra", "/bootstrap-x", "/realtimes"):
            with self.subTest(path):
                self.assertFalse(is_exempt_path(path))


# ======================================================================
# enforce_subscription_access: gracia y bloqueo, fronteras exactas
# ======================================================================


class EnforceSubscriptionAccessTests(unittest.TestCase):
    def setUp(self):
        _reloj(self)

    def test_durante_la_gracia_no_bloquea_ninguna_ruta(self):
        tenant = _tenant(AHORA - timedelta(hours=1))
        for path in ("/orders", "/sales", "/menu", "/plan"):
            with self.subTest(path):
                enforce_subscription_access(tenant, _request(path))

    def test_a_119h_59m_59s_de_vencido_no_bloquea(self):
        enforce_subscription_access(_tenant(AHORA - timedelta(hours=120) + timedelta(seconds=1)), _request("/orders"))

    def test_a_120_horas_exactas_bloquea(self):
        with self.assertRaises(SubscriptionBlockedError) as ctx:
            enforce_subscription_access(_tenant(AHORA - timedelta(hours=120)), _request("/orders"))
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.code, "subscription_blocked")

    def test_bloqueado_rechaza_las_rutas_de_negocio(self):
        tenant = _tenant(AHORA - timedelta(days=30))
        for path in ("/orders", "/sales", "/cash/shifts", "/products", "/menu", "/reports/sales", "/tenant"):
            with self.subTest(path):
                with self.assertRaises(SubscriptionBlockedError):
                    enforce_subscription_access(tenant, _request(path))

    def test_bloqueado_deja_pasar_las_rutas_exentas(self):
        tenant = _tenant(AHORA - timedelta(days=30))
        for path in ("/auth/login", "/auth/refresh-token", "/bootstrap", "/plan",
                     "/plan/subscription/payment-link", "/plan/subscription/payments",
                     "/notifications", "/realtime/ticket"):
            with self.subTest(path):
                enforce_subscription_access(tenant, _request(path))

    def test_el_metodo_http_no_cambia_la_exencion(self):
        tenant = _tenant(AHORA - timedelta(days=30))
        enforce_subscription_access(tenant, _request("/plan/subscription/payment-link", method="POST"))
        with self.assertRaises(SubscriptionBlockedError):
            enforce_subscription_access(tenant, _request("/orders", method="POST"))

    def test_sin_vencimiento_nunca_bloquea(self):
        enforce_subscription_access(_tenant(None), _request("/orders"))

    def test_sin_request_no_hay_exenciones(self):
        """El comensal (`resolve_tenant_by_id`) no pasa `request`: nada es exento."""
        with self.assertRaises(SubscriptionBlockedError):
            enforce_subscription_access(_tenant(AHORA - timedelta(days=30)))

    def test_sin_request_y_en_gracia_no_bloquea(self):
        enforce_subscription_access(_tenant(AHORA - timedelta(hours=2)))

    def test_si_la_ruta_no_esta_resuelta_usa_la_ruta_de_la_url(self):
        scope = {"type": "http", "method": "GET", "path": "/api/v1/plan", "headers": []}  # sin "route"
        enforce_subscription_access(_tenant(AHORA - timedelta(days=30)), Request(scope))
        scope["path"] = "/api/v1/orders"
        with self.assertRaises(SubscriptionBlockedError):
            enforce_subscription_access(_tenant(AHORA - timedelta(days=30)), Request(scope))

    def test_un_bloqueado_levanta_el_bloqueo_en_cuanto_cambia_plan_vence_en(self):
        """FR-009: sin cachés; la siguiente petición ve el nuevo vencimiento."""
        tenant = _tenant(AHORA - timedelta(days=30))
        with self.assertRaises(SubscriptionBlockedError):
            enforce_subscription_access(tenant, _request("/orders"))

        tenant.plan_vence_en = AHORA + timedelta(days=30)

        enforce_subscription_access(tenant, _request("/orders"))


# ======================================================================
# Los puntos por donde pasa todo tenant
# ======================================================================


@contextmanager
def _sesion(db):
    yield db


class PuntosDeResolucionTests(unittest.TestCase):
    """`get_tenant` (staff y menú público, por `x-tenant-host`) y `resolve_tenant_by_id` (comensal, por
    token) llaman al guard. `get_optional_tenant` (solo `/bootstrap`) no."""

    def setUp(self):
        _reloj(self)
        self.db = fx.new_session()
        plan = fx.make_plan(self.db)
        self.en_gracia = fx.make_tenant(self.db, plan=plan, ciclo_facturacion="mensual")
        self.en_gracia.plan_vence_en = AHORA - timedelta(hours=2)
        self.bloqueado = fx.make_tenant(self.db, plan=plan, ciclo_facturacion="mensual")
        self.bloqueado.plan_vence_en = AHORA - timedelta(days=30)
        self.db.commit()

    def _con_host(self, tenant, path_format):
        req = _request(path_format)
        req.scope["headers"] = [(b"x-tenant-host", b"heladeria.skeilopos.com")]
        patcher = mock.patch("app.core.db.find_tenant_by_host", return_value=tenant)
        patcher.start()
        self.addCleanup(patcher.stop)
        return req

    # ---------------------------------------------------------- get_tenant

    def test_get_tenant_bloquea_una_ruta_de_negocio(self):
        with self.assertRaises(SubscriptionBlockedError):
            get_tenant(self._con_host(self.bloqueado, "/orders"))

    def test_get_tenant_no_bloquea_una_ruta_exenta(self):
        self.assertIs(get_tenant(self._con_host(self.bloqueado, "/plan")), self.bloqueado)

    def test_get_tenant_no_bloquea_en_gracia(self):
        self.assertIs(get_tenant(self._con_host(self.en_gracia, "/orders")), self.en_gracia)

    def test_get_tenant_sigue_dando_404_si_el_host_no_resuelve(self):
        req = _request("/orders")
        req.scope["headers"] = [(b"x-tenant-host", b"nadie.skeilopos.com")]
        with mock.patch("app.core.db.find_tenant_by_host", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                get_tenant(req)
        self.assertEqual(ctx.exception.status_code, 404)

    # ---------------------------------------------------------- resolve_tenant_by_id (comensal)

    def test_el_comensal_de_un_tenant_bloqueado_es_rechazado(self):
        with mock.patch("app.core.db.with_db", lambda schema: _sesion(self.db)):
            with self.assertRaises(SubscriptionBlockedError):
                resolve_tenant_by_id(self.bloqueado.id)

    def test_el_comensal_de_un_tenant_en_gracia_pasa(self):
        with mock.patch("app.core.db.with_db", lambda schema: _sesion(self.db)):
            self.assertEqual(resolve_tenant_by_id(self.en_gracia.id).id, self.en_gracia.id)

    def test_un_tenant_inexistente_sigue_dando_404(self):
        with mock.patch("app.core.db.with_db", lambda schema: _sesion(self.db)):
            with self.assertRaises(HTTPException) as ctx:
                resolve_tenant_by_id(999_999)
        self.assertEqual(ctx.exception.status_code, 404)

    # ---------------------------------------------------------- get_optional_tenant (/bootstrap)

    def test_get_optional_tenant_no_bloquea(self):
        from app.core.dependencies import get_optional_tenant

        req = self._con_host(self.bloqueado, "/bootstrap")
        with mock.patch("app.core.dependencies.find_tenant_by_host", return_value=self.bloqueado):
            self.assertIs(get_optional_tenant(req), self.bloqueado)


# ======================================================================
# La aplicación real, por HTTP
# ======================================================================


def _aplicacion_real():
    # `app.main` ejecuta `create_app()` al importarse y esta llama a `initialize_database()`, que abre
    # PostgreSQL; se sustituye por una función vacía ANTES de importar (mismo motivo que
    # `test_module_not_in_plan_error.py`).
    with mock.patch("app.core.db.initialize_database", lambda *a, **k: None):
        from app.main import app
    return app


class GuardPorHttpTests(unittest.TestCase):
    """`get_tenant` NO se sustituye aquí: el guard corre de verdad dentro de la dependencia real, con la
    ruta real resuelta por Starlette. Solo se sustituyen la búsqueda del tenant por host, la sesión y
    el usuario autenticado."""

    def setUp(self):
        _reloj(self)
        self.app = _aplicacion_real()
        self.db = fx.new_session(multithreaded=True)
        plan = fx.make_plan(self.db, inventario_access=True)
        self.tenant = fx.make_tenant(self.db, plan=plan, ciclo_facturacion="mensual")
        self.tenant.plan_vence_en = AHORA - timedelta(days=30)  # bloqueada
        self.cajero = fx.make_user(self.db, self.tenant, role_name="CASHIER")
        self.db.commit()
        self.app.dependency_overrides[get_db] = lambda: self.db
        self.app.dependency_overrides[get_current_user] = lambda: self.cajero
        self.addCleanup(self.app.dependency_overrides.clear)
        patcher = mock.patch("app.core.db.find_tenant_by_host", return_value=self.tenant)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.headers = {"x-tenant-host": "heladeria.skeilopos.com"}

    def test_una_ruta_de_negocio_responde_403_con_el_cuerpo_exacto(self):
        resp = self.client.get("/api/v1/tenant", headers=self.headers)

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(
            resp.json(),
            {"detail": "Tu plan venció. Debe renovarse para seguir usando el sistema.", "code": "subscription_blocked"},
        )

    def test_el_plan_sigue_respondiendo_con_el_tenant_bloqueado(self):
        resp = self.client.get("/api/v1/plan", headers=self.headers)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["subscription_state"], "bloqueada")
        self.assertTrue(resp.json()["vencido"])

    def test_el_mismo_tenant_en_gracia_si_pasa_la_ruta_de_negocio(self):
        self.tenant.plan_vence_en = AHORA - timedelta(hours=2)
        self.db.commit()

        resp = self.client.get("/api/v1/tenant", headers=self.headers)

        self.assertNotEqual(resp.status_code, 403)

    def test_un_pago_que_mueve_el_vencimiento_desbloquea_la_peticion_siguiente(self):
        self.assertEqual(self.client.get("/api/v1/tenant", headers=self.headers).status_code, 403)

        self.tenant.plan_vence_en = AHORA + timedelta(days=30)  # lo que hará el webhook al aprobar
        self.db.commit()

        self.assertNotEqual(self.client.get("/api/v1/tenant", headers=self.headers).status_code, 403)


if __name__ == "__main__":
    unittest.main()
