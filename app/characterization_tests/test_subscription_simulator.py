"""Tests de la nueva funcionalidad — spec 161-suscripcion-pago-wompi: el simulador de eventos de Wompi
(`app/scripts/simulate_wompi_event.py`) produce eventos que el verificador REAL del webhook acepta, y los
envía como lo haría Wompi.

Como el simulador firma con la misma función que el servidor verifica, estas pruebas confirman el flujo, no la
fórmula de Wompi (pendiente de un evento real de Sandbox, FR-041).

    python -m unittest app.characterization_tests.test_subscription_simulator -v
"""
from __future__ import annotations

import io
import json
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer

from app.characterization_tests import plan_fixtures  # noqa: F401  - variables de entorno inertes antes de importar app.*
from app.core import wompi
from app.scripts import simulate_wompi_event as sim

SECRETO = "test_events_local"


class ConstruirEventoTests(unittest.TestCase):
    def test_el_verificador_real_acepta_el_evento(self):
        ev = sim.build_event("SKP-abc", amount_in_cents=8_990_000, secret=SECRETO)

        wompi.verify_event(ev, ev["signature"]["checksum"], SECRETO)  # no lanza

    def test_lleva_los_datos_pedidos(self):
        tx = sim.build_event("SKP-abc", amount_in_cents=123, secret=SECRETO, status="declined", transaction_id="TX-9")["data"]["transaction"]

        self.assertEqual(
            tx, {"id": "TX-9", "status": "DECLINED", "amount_in_cents": 123, "reference": "SKP-abc", "currency": "COP"}
        )

    def test_los_properties_son_los_tres_minimos_firmados_de_la_documentacion(self):
        ev = sim.build_event("SKP-abc", amount_in_cents=1, secret=SECRETO)

        self.assertEqual(ev["signature"]["properties"], list(wompi.REQUIRED_SIGNED_PROPERTIES))

    def test_el_checksum_va_en_mayusculas_como_en_la_documentacion_y_se_acepta_igual(self):
        ev = sim.build_event("SKP-abc", amount_in_cents=1, secret=SECRETO)

        self.assertEqual(ev["signature"]["checksum"], ev["signature"]["checksum"].upper())
        wompi.verify_event(ev, None, SECRETO)

    def test_tamper_produce_una_firma_que_el_verificador_rechaza_con_401(self):
        ev = sim.build_event("SKP-abc", amount_in_cents=1, secret=SECRETO, tamper=True)

        with self.assertRaises(wompi.WompiEventError) as ctx:
            wompi.verify_event(ev, None, SECRETO)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_firmado_con_otro_secreto_se_rechaza(self):
        ev = sim.build_event("SKP-abc", amount_in_cents=1, secret="otro")

        with self.assertRaises(wompi.WompiEventError):
            wompi.verify_event(ev, None, SECRETO)

    def test_sin_tx_id_se_genera_uno_distinto_por_evento_incluso_en_el_mismo_segundo(self):
        """Dos eventos del mismo segundo con el mismo id chocarían con la defensa «una transacción de Wompi no puede
        ligarse a dos pagos»: Wompi nunca repite el id de una transacción y el simulador tampoco debe."""
        ids = {sim.build_event("SKP-abc", amount_in_cents=1, secret=SECRETO, timestamp=100)["data"]["transaction"]["id"] for _ in range(50)}

        self.assertEqual(len(ids), 50)


class PostEventTests(unittest.TestCase):
    """Un servidor HTTP local de juguete que registra lo que recibe, para ver el envío tal como lo vería Wompi."""

    def setUp(self):
        self.recibido = {}
        recibido = self.recibido

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                recibido["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                recibido["checksum_header"] = self.headers.get("X-Event-Checksum")
                recibido["content_type"] = self.headers.get("Content-Type")
                self.send_response(recibido.get("responder", 200))
                self.end_headers()
                self.wfile.write(b'{"status": "processed"}')

            def log_message(self, *a):  # silencio
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/v1/webhooks/wompi"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_envia_el_cuerpo_json_y_la_cabecera_x_event_checksum(self):
        ev = sim.build_event("SKP-abc", amount_in_cents=1, secret=SECRETO)

        status, body = sim.post_event(self.url, ev)

        self.assertEqual((status, json.loads(body)), (200, {"status": "processed"}))
        self.assertEqual(self.recibido["body"], ev)
        self.assertEqual(self.recibido["checksum_header"], ev["signature"]["checksum"])
        self.assertEqual(self.recibido["content_type"], "application/json")

    def test_un_4xx_se_devuelve_como_respuesta_y_no_como_excepcion(self):
        self.recibido["responder"] = 401

        status, _ = sim.post_event(self.url, sim.build_event("SKP-abc", amount_in_cents=1, secret=SECRETO))

        self.assertEqual(status, 401)


class CliTests(unittest.TestCase):
    def _ejecutar(self, *argv):
        salida = io.StringIO()
        with redirect_stdout(salida):
            codigo = sim.main(list(argv))
        return codigo, salida.getvalue()

    def test_sin_post_imprime_el_evento_firmado_como_json_valido(self):
        codigo, salida = self._ejecutar("--reference", "SKP-abc", "--amount-cents", "8990000", "--secret", SECRETO)

        self.assertEqual(codigo, 0)
        ev = json.loads(salida)
        wompi.verify_event(ev, None, SECRETO)
        self.assertEqual(ev["data"]["transaction"]["status"], "APPROVED")

    def test_status_y_tx_id_se_pasan_al_evento(self):
        _, salida = self._ejecutar("--reference", "SKP-abc", "--amount-cents", "1", "--secret", SECRETO, "--status", "voided", "--tx-id", "TX-7")

        tx = json.loads(salida)["data"]["transaction"]
        self.assertEqual((tx["status"], tx["id"]), ("VOIDED", "TX-7"))

    def test_sin_secreto_termina_con_codigo_2(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WOMPI_EVENTS_SECRET", None)
            codigo, salida = self._ejecutar("--reference", "SKP-abc", "--amount-cents", "1")

        self.assertEqual(codigo, 2)
        self.assertIn("--secret", salida)

    def test_un_status_desconocido_lo_rechaza_argparse(self):
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stdout(io.StringIO()):
                import contextlib
                with contextlib.redirect_stderr(io.StringIO()):
                    sim.main(["--reference", "SKP-abc", "--amount-cents", "1", "--secret", SECRETO, "--status", "PAGADO"])

        self.assertEqual(ctx.exception.code, 2)

    def test_el_secreto_puede_venir_de_la_variable_de_entorno(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"WOMPI_EVENTS_SECRET": SECRETO}):
            parser_default = sim.main  # el default de --secret se lee al construir el parser, dentro de main
            codigo, salida = self._ejecutar("--reference", "SKP-abc", "--amount-cents", "1")

        self.assertEqual(codigo, 0)
        wompi.verify_event(json.loads(salida), None, SECRETO)

    def test_post_a_un_servidor_inalcanzable_no_se_traga_el_error(self):
        with self.assertRaises(Exception):
            sim.post_event("http://127.0.0.1:9/nada", sim.build_event("SKP-abc", amount_in_cents=1, secret=SECRETO), timeout=1)


if __name__ == "__main__":
    unittest.main()
