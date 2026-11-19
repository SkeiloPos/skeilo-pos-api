"""Tests de la nueva funcionalidad — spec 161-suscripcion-pago-wompi, FR-007: el rechazo por
suscripción bloqueada es un 403 con el código estable `subscription_blocked`, distinguible del
rechazo por módulo fuera del plan (`MODULE_NOT_IN_PLAN`, spec 141) y de cualquier otro acceso
denegado (A-162).

No son characterization tests — comportamiento enteramente nuevo. Tres bloques:

- la clase de error, sin HTTP (sigue siendo `HTTPException`, así que los `except HTTPException`
  existentes la atrapan);
- su manejador, sobre una aplicación mínima (el cuerpo `{detail, code}`);
- que `create_app()` lo registra, y que el manejador de `ModuleNotInPlanError` sigue ganando para su
  propio tipo (Starlette elige el handler por el MRO de la excepción).

Historia 3 (contrato `subscription-api.md § 2`): `SubscriptionNotPayableError` (409, con `reason`) y
`PaymentGatewayNotConfiguredError` (503) llevan su propio `code` y siguen siendo `HTTPException`.

    python -m unittest app.characterization_tests.test_subscription_errors -v
"""
from __future__ import annotations

import unittest
from unittest import mock

from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

from app.characterization_tests import fixtures  # noqa: F401  - variables de entorno inertes antes de importar app.*
from app.core.error_middleware import register_module_error_handler, register_subscription_error_handlers
from app.core.exceptions import (
    SUBSCRIPTION_NOT_PAYABLE_REASONS,
    ModuleNotInPlanError,
    PaymentGatewayNotConfiguredError,
    SubscriptionBlockedError,
    SubscriptionNotPayableError,
)

#: Texto de siempre del 403 por plan vencido (spec 033); no cambia con la spec 161: solo gana `code`.
DETALLE = "Tu plan venció. Debe renovarse para seguir usando el sistema."


class SubscriptionBlockedErrorTests(unittest.TestCase):
    def test_es_un_http_exception_403_con_code_estable(self):
        exc = SubscriptionBlockedError()

        self.assertIsInstance(exc, HTTPException)  # los `except HTTPException` existentes la siguen atrapando
        self.assertEqual(exc.status_code, 403)
        self.assertEqual(exc.code, "subscription_blocked")

    def test_detail_es_un_str_con_el_texto_de_siempre(self):
        exc = SubscriptionBlockedError()

        self.assertIsInstance(exc.detail, str)
        self.assertEqual(exc.detail, DETALLE)
        self.assertIn("venci", exc.detail.lower())  # contrato con las pruebas de spec 033

    def test_no_es_un_error_de_modulo_fuera_del_plan(self):
        self.assertNotIsInstance(SubscriptionBlockedError(), ModuleNotInPlanError)
        self.assertNotEqual(SubscriptionBlockedError.code, ModuleNotInPlanError.code)


def _app_minima() -> FastAPI:
    app = FastAPI()
    register_module_error_handler(app)
    register_subscription_error_handlers(app)

    @app.get("/bloqueado")
    def _bloqueado():
        raise SubscriptionBlockedError()

    @app.get("/sin-modulo")
    def _sin_modulo():
        raise ModuleNotInPlanError("inventario", "inventario")

    @app.get("/rol")
    def _rol():
        raise HTTPException(status_code=403, detail="Tenant admin access required")

    @app.get("/no-pagable/{reason}")
    def _no_pagable(reason: str):
        raise SubscriptionNotPayableError(reason)

    @app.get("/sin-pasarela")
    def _sin_pasarela():
        raise PaymentGatewayNotConfiguredError()

    return app


class SubscriptionBlockedHandlerTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(_app_minima(), raise_server_exceptions=False)

    def test_responde_403_con_detail_y_code(self):
        resp = self.client.get("/bloqueado")

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json(), {"detail": DETALLE, "code": "subscription_blocked"})

    def test_el_error_de_modulo_conserva_su_propio_cuerpo(self):
        resp = self.client.get("/sin-modulo")

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(
            resp.json(),
            {"detail": "Tu plan actual no incluye el módulo de inventario.", "code": "MODULE_NOT_IN_PLAN", "module": "inventario"},
        )

    def test_un_403_de_rol_sigue_sin_code(self):
        resp = self.client.get("/rol")

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json(), {"detail": "Tenant admin access required"})


class AplicacionRealTests(unittest.TestCase):
    def test_create_app_registra_el_manejador(self):
        # `app.main` ejecuta `create_app()` al importarse y esta llama a `initialize_database()`, que abre
        # PostgreSQL; se sustituye por una función vacía ANTES de importar (mismo motivo que
        # `test_module_not_in_plan_error.py`).
        with mock.patch("app.core.db.initialize_database", lambda *a, **k: None):
            from app.main import app

        self.assertIn(SubscriptionBlockedError, app.exception_handlers)
        self.assertIn(ModuleNotInPlanError, app.exception_handlers)


class SubscriptionNotPayableErrorTests(unittest.TestCase):
    def test_es_un_http_exception_409_con_code_y_reason(self):
        exc = SubscriptionNotPayableError("already_active")

        self.assertIsInstance(exc, HTTPException)
        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.code, "subscription_not_payable")
        self.assertEqual(exc.reason, "already_active")
        self.assertIsInstance(exc.detail, str)

    def test_las_cuatro_razones_del_contrato(self):
        self.assertEqual(
            set(SUBSCRIPTION_NOT_PAYABLE_REASONS), {"already_active", "no_expiry", "unsupported_cycle", "no_price"}
        )

    def test_cada_razon_tiene_su_propio_mensaje(self):
        mensajes = {SubscriptionNotPayableError(r).detail for r in SUBSCRIPTION_NOT_PAYABLE_REASONS}

        self.assertEqual(len(mensajes), 4)

    def test_una_razon_desconocida_se_rechaza_al_construir_el_error(self):
        with self.assertRaises(ValueError):
            SubscriptionNotPayableError("porque_si")


class PaymentGatewayNotConfiguredErrorTests(unittest.TestCase):
    def test_es_un_http_exception_503_con_code(self):
        exc = PaymentGatewayNotConfiguredError()

        self.assertIsInstance(exc, HTTPException)
        self.assertEqual(exc.status_code, 503)
        self.assertEqual(exc.code, "payment_gateway_not_configured")
        self.assertIsInstance(exc.detail, str)


class NuevosManejadoresTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(_app_minima(), raise_server_exceptions=False)

    def test_no_pagable_responde_409_con_detail_code_y_reason(self):
        for reason in SUBSCRIPTION_NOT_PAYABLE_REASONS:
            with self.subTest(reason):
                resp = self.client.get(f"/no-pagable/{reason}")

                self.assertEqual(resp.status_code, 409)
                self.assertEqual(
                    resp.json(),
                    {"detail": SubscriptionNotPayableError(reason).detail, "code": "subscription_not_payable", "reason": reason},
                )

    def test_sin_pasarela_responde_503_con_detail_y_code_sin_ningun_secreto(self):
        resp = self.client.get("/sin-pasarela")

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(set(resp.json()), {"detail", "code"})
        self.assertEqual(resp.json()["code"], "payment_gateway_not_configured")

    def test_create_app_registra_los_tres_manejadores(self):
        with mock.patch("app.core.db.initialize_database", lambda *a, **k: None):
            from app.main import app

        for clase in (SubscriptionBlockedError, SubscriptionNotPayableError, PaymentGatewayNotConfiguredError):
            with self.subTest(clase.__name__):
                self.assertIn(clase, app.exception_handlers)


if __name__ == "__main__":
    unittest.main()
