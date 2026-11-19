"""Tests de la nueva funcionalidad — spec 161-suscripcion-pago-wompi, Historias 3 y 4 (FR-014, FR-015,
FR-019; contrato `wompi-webhook.md`): la firma de integridad y la URL del Web Checkout, y el checksum de
los eventos de Wompi.

No son characterization tests — comportamiento enteramente nuevo.

**Vectores.** La firma de integridad se prueba contra el ejemplo de la documentación de Wompi, que se
verificó con SHA-256 (cadena `sk8-438k4-xmxm392-sn2m2490000COPprod_integrity_Z5mMke9x…` →
`37c8407747e5…`). El checksum de EVENTOS **no** se prueba con el ejemplo numérico de la documentación:
con la cadena y el secreto que publica, SHA-256 da otro valor (`research.md` Decisión 1), así que ese
ejemplo no es un vector fiable. Aquí la fórmula —tal como la describe el texto de la documentación— se
recalcula de forma independiente en la prueba, y su confirmación definitiva requiere un evento real de
Sandbox (tarea pendiente, FR-041).

    python -m unittest app.characterization_tests.test_subscription_wompi -v
"""
from __future__ import annotations

import hashlib
from hashlib import sha256
import re
import unittest
import uuid
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

from app.characterization_tests import plan_fixtures  # noqa: F401  - variables de entorno inertes antes de importar app.*
from app.core import wompi

# Ejemplo de la documentación de Wompi (sin `expiration-time`): referencia, monto en centavos, moneda, secreto.
DOC_REFERENCIA = "sk8-438k4-xmxm392-sn2m2"
DOC_CENTAVOS = 490000
DOC_SECRETO = "prod_integrity_Z5mMke9x0k8gpErbDqwrJXMqsI6SFli6"
DOC_FIRMA = "37c8407747e595535433ef8f6a811d853cd943046624a0ec04662b17bbf33bf5"


def _sha256(texto: str) -> str:
    return hashlib.sha256(texto.encode()).hexdigest()


# ======================================================================
# Historia 3 — el link
# ======================================================================


class ReferenciaTests(unittest.TestCase):
    def test_tiene_el_formato_skp_mas_32_hex_y_36_caracteres(self):
        ref = wompi.build_reference(uuid.UUID("8f0c6f0e-1a4b-4a43-9d51-2c4d6c1f9e10"))

        self.assertEqual(ref, "SKP-8f0c6f0e1a4b4a439d512c4d6c1f9e10")
        self.assertEqual(len(ref), 36)
        self.assertRegex(ref, r"^SKP-[0-9a-f]{32}$")  # alfanumérica y guion: lo que Wompi recomienda

    def test_es_determinista_respecto_al_pago_y_distinta_entre_pagos(self):
        a, b = uuid.uuid4(), uuid.uuid4()

        self.assertEqual(wompi.build_reference(a), wompi.build_reference(a))
        self.assertNotEqual(wompi.build_reference(a), wompi.build_reference(b))


class CentavosTests(unittest.TestCase):
    def test_convierte_pesos_a_centavos_enteros(self):
        for monto, esperado in (
            (Decimal("89900.00"), 8_990_000),
            (Decimal("89900.50"), 8_990_050),
            (Decimal("0.01"), 1),
            (Decimal("1"), 100),
            (Decimal("4900000"), 490_000_000),
        ):
            with self.subTest(monto=monto):
                valor = wompi.amount_in_cents(monto)
                self.assertEqual(valor, esperado)
                self.assertIsInstance(valor, int)

    def test_no_pasa_por_float(self):
        # 0.1 + 0.2 != 0.3 en coma flotante; con Decimal el cálculo es exacto.
        self.assertEqual(wompi.amount_in_cents(Decimal("0.10") + Decimal("0.20")), 30)

    def test_un_monto_con_fraccion_de_centavo_se_rechaza(self):
        with self.assertRaises(ValueError):
            wompi.amount_in_cents(Decimal("10.005"))


class ExpiracionTests(unittest.TestCase):
    def test_formato_iso_8601_utc_con_milisegundos_a_cero(self):
        self.assertEqual(wompi.format_expiration(datetime(2026, 11, 18, 15, 30, 0)), "2026-11-18T15:30:00.000Z")

    def test_descarta_los_microsegundos_en_vez_de_redondear(self):
        self.assertEqual(wompi.format_expiration(datetime(2026, 11, 18, 15, 30, 59, 999_999)), "2026-11-18T15:30:59.000Z")


class FirmaDeIntegridadTests(unittest.TestCase):
    def test_reproduce_el_vector_de_la_documentacion_de_wompi(self):
        firma = wompi.integrity_signature(DOC_REFERENCIA, DOC_CENTAVOS, "COP", None, DOC_SECRETO)

        self.assertEqual(firma, DOC_FIRMA)

    def test_sin_expiracion_no_agrega_nada_a_la_cadena(self):
        self.assertEqual(
            wompi.integrity_signature("REF", 100, "COP", None, "sec"), _sha256("REF" + "100" + "COP" + "sec")
        )

    def test_con_expiracion_va_entre_la_moneda_y_el_secreto(self):
        exp = "2026-11-18T15:30:00.000Z"

        self.assertEqual(
            wompi.integrity_signature("REF", 100, "COP", exp, "sec"), _sha256("REF" + "100" + "COP" + exp + "sec")
        )

    def test_la_expiracion_cambia_la_firma(self):
        base = wompi.integrity_signature("REF", 100, "COP", None, "sec")
        con_exp = wompi.integrity_signature("REF", 100, "COP", "2026-11-18T15:30:00.000Z", "sec")
        otra_exp = wompi.integrity_signature("REF", 100, "COP", "2026-11-18T15:31:00.000Z", "sec")

        self.assertEqual(len({base, con_exp, otra_exp}), 3)

    def test_cualquier_dato_alterado_cambia_la_firma(self):
        ref = wompi.integrity_signature("REF", 100, "COP", None, "sec")

        self.assertNotEqual(ref, wompi.integrity_signature("REF2", 100, "COP", None, "sec"))
        self.assertNotEqual(ref, wompi.integrity_signature("REF", 101, "COP", None, "sec"))
        self.assertNotEqual(ref, wompi.integrity_signature("REF", 100, "USD", None, "sec"))
        self.assertNotEqual(ref, wompi.integrity_signature("REF", 100, "COP", None, "otro"))

    def test_es_sha256_hexadecimal_en_minusculas(self):
        self.assertRegex(wompi.integrity_signature("REF", 100, "COP", None, "sec"), r"^[0-9a-f]{64}$")


class UrlDelCheckoutTests(unittest.TestCase):
    def _url(self, **kw):
        datos = dict(
            base_url="https://checkout.wompi.co/p/",
            public_key="pub_test_abc",
            reference="SKP-8f0c6f0e1a4b4a439d512c4d6c1f9e10",
            amount_in_cents=8_990_000,
            currency="COP",
            signature="f" * 64,
            redirect_url="https://heladeria.skeilopos.com/",
            expiration="2026-11-18T15:30:00.000Z",
        )
        datos.update(kw)
        return wompi.build_checkout_url(**datos)

    def test_usa_la_base_del_checkout_y_lleva_todos_los_parametros(self):
        url = self._url()
        partes = urlsplit(url)
        params = dict(parse_qsl(partes.query))

        self.assertEqual(f"{partes.scheme}://{partes.netloc}{partes.path}", "https://checkout.wompi.co/p/")
        self.assertEqual(
            params,
            {
                "public-key": "pub_test_abc",
                "currency": "COP",
                "amount-in-cents": "8990000",
                "reference": "SKP-8f0c6f0e1a4b4a439d512c4d6c1f9e10",
                "signature:integrity": "f" * 64,
                "redirect-url": "https://heladeria.skeilopos.com/",
                "expiration-time": "2026-11-18T15:30:00.000Z",
            },
        )

    def test_el_nombre_signature_integrity_va_codificado_como_en_un_formulario_get(self):
        self.assertIn("signature%3Aintegrity=", self._url())

    def test_la_expiracion_de_la_url_es_exactamente_la_que_se_firmo(self):
        exp = wompi.format_expiration(datetime(2026, 11, 18, 15, 30, 0))
        firma = wompi.integrity_signature("SKP-X", 100, "COP", exp, "sec")

        params = dict(parse_qsl(urlsplit(self._url(expiration=exp, signature=firma)).query))

        self.assertEqual(params["expiration-time"], exp)
        self.assertEqual(params["signature:integrity"], _sha256("SKP-X" + "100" + "COP" + exp + "sec"))

    def test_la_base_se_respeta_aunque_cambie_por_configuracion(self):
        url = self._url(base_url="https://checkout.sandbox.example/p/")

        self.assertTrue(url.startswith("https://checkout.sandbox.example/p/?"))

    def test_el_redirect_con_caracteres_especiales_se_codifica_y_se_recupera_igual(self):
        redirect = "https://x.skeilopos.com/plan?a=1&b=2"

        params = dict(parse_qsl(urlsplit(self._url(redirect_url=redirect)).query))

        self.assertEqual(params["redirect-url"], redirect)

    def test_sin_expiracion_no_incluye_el_parametro(self):
        self.assertNotIn("expiration-time", self._url(expiration=None))


class PasarelaConfiguradaTests(unittest.TestCase):
    def test_exige_llave_publica_y_secreto_de_integridad(self):
        for public, integrity, esperado in (
            ("pub_test_x", "test_integrity_x", True),
            (None, "test_integrity_x", False),
            ("pub_test_x", None, False),
            (None, None, False),
        ):
            with self.subTest(public=public, integrity=integrity):
                cfg = SimpleNamespace(WOMPI_PUBLIC_KEY=public, WOMPI_INTEGRITY_SECRET=integrity)
                self.assertIs(wompi.gateway_configured(cfg), esperado)

    def test_una_cadena_vacia_cuenta_como_sin_configurar(self):
        """`.env.example` trae `WOMPI_PUBLIC_KEY=` vacío: llega como cadena vacía, no como `None`."""
        cfg = SimpleNamespace(WOMPI_PUBLIC_KEY="", WOMPI_INTEGRITY_SECRET="")

        self.assertIs(wompi.gateway_configured(cfg), False)

    def test_el_secreto_de_eventos_no_hace_falta_para_generar_un_link(self):
        cfg = SimpleNamespace(WOMPI_PUBLIC_KEY="pub_test_x", WOMPI_INTEGRITY_SECRET="test_integrity_x", WOMPI_EVENTS_SECRET=None)

        self.assertIs(wompi.gateway_configured(cfg), True)
