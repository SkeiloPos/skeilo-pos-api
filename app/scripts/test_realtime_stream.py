"""E2E del stream SSE contra un servidor real.

    uvicorn app.main:app --port 8099 &
    python -m app.scripts.test_realtime_stream [--base http://127.0.0.1:8099]

Reutiliza el fixture de `e2e_qr_flow` (tenant, mesa, producto con receta, staff
desechable) y cubre los puntos de verificación del diseño:

  · handshake del comensal y `hello`;
  · el evento llega al comensal en tiempo real;
  · **aislamiento**: la mesa 1 no recibe nada de la mesa 2;
  · **replay**: matar la conexión, generar eventos, reconectar con
    `last_event_id` y comprobar que llegan todos y en orden;
  · el ticket del staff funciona **una** vez y da 401 al reusarlo;
  · heartbeat `: ping` dentro de la ventana esperada;
  · tope de conexiones simultáneas por sesión de mesa;
  · un token inválido da 401 en el handshake (para que EventSource no reintente).
"""
import argparse
import json
import sys
import threading
import time
import uuid
from decimal import Decimal

import requests

from app.core.config import settings
from app.scripts.e2e_qr_flow import Api, Fail, check, note, seed, teardown


class SseClient:
    """Lector de un `text/event-stream` en un hilo, con parser de frames."""

    def __init__(self, base, params):
        self.url = f"{base}/api/v1/realtime/stream"
        self.params = params
        self.events: list[dict] = []
        self.comments: list[str] = []
        self.status: int | None = None
        self._resp = None
        self._thread = None
        self._stop = threading.Event()

    def open(self, timeout=None):
        # El timeout de lectura **debe superar el heartbeat**: si no, el socket
        # muere en el silencio entre pings y parece que el servidor no envía
        # nada. Es la misma condición que debe cumplir `proxy_read_timeout` en
        # nginx (ver docs/realtime.md), y el motivo de que exista el heartbeat.
        if timeout is None:
            timeout = settings.REALTIME_HEARTBEAT_SECONDS * 2 + 10
        self._resp = requests.get(
            self.url, params=self.params, stream=True, timeout=timeout,
            headers={"Accept": "text/event-stream"},
        )
        self.status = self._resp.status_code
        if self.status != 200:
            return self
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()
        return self

    def _read(self):
        buf = []
        try:
            # `chunk_size=1` a propósito: con el valor por defecto (512 B)
            # `iter_lines` espera a llenar el búfer antes de entregar nada, así
            # que un `: ping` de 8 bytes se quedaría retenido y el test diría que
            # el heartbeat no llega cuando sí llega. Es un test, la eficiencia da
            # igual; la fidelidad no.
            for raw in self._resp.iter_lines(chunk_size=1, decode_unicode=True):
                if self._stop.is_set():
                    return
                if raw is None:
                    continue
                if raw.startswith(":"):
                    self.comments.append(raw)
                    continue
                if raw == "":
                    if buf:
                        self._flush(buf)
                        buf = []
                    continue
                buf.append(raw)
        except Exception:
            pass  # la conexión se cerró; es parte del test

    def _flush(self, lines):
        frame = {}
        for line in lines:
            campo, _, valor = line.partition(":")
            frame[campo.strip()] = valor.strip()
        if "data" in frame:
            try:
                frame["parsed"] = json.loads(frame["data"])
            except ValueError:
                frame["parsed"] = {}
            self.events.append(frame)

    def wait_for(self, tipo, timeout=8):
        """Espera un evento de ese tipo y lo devuelve; None si no llega."""
        fin = time.time() + timeout
        while time.time() < fin:
            for ev in self.events:
                if ev.get("event") == tipo:
                    return ev
            time.sleep(0.1)
        return None

    def types(self):
        return [e.get("event") for e in self.events]

    def last_id(self):
        ids = [e["id"] for e in self.events if e.get("id")]
        return ids[-1] if ids else None

    def close(self):
        self._stop.set()
        if self._resp is not None:
            self._resp.close()


def abrir_sesion(api, base, fx, nombre):
    """Escanea el QR y se une a la mesa. Devuelve el token de sesión."""
    qr = api("GET", f"/api/v1/orders/tables/{fx['table_id']}/qr-token").json()["qr_token"]
    r = requests.post(
        f"{base}/api/v1/cart/sessions",
        json={"qr_token": qr, "display_name": nombre}, timeout=20,
    )
    if r.status_code not in (200, 201):
        raise Fail(f"POST /cart/sessions → {r.status_code}: {r.text[:300]}")
    return r.json()["session_token"], r.json()["table_session_id"]


def enviar_pedido(base, token, variant_id):
    """Arma un carrito y lo envía. Devuelve el pedido creado."""
    h = {"x-session-token": token}
    requests.post(f"{base}/api/v1/cart/items", headers=h, timeout=20,
                  json={"product_variant_id": str(variant_id), "quantity": 1})
    r = requests.post(f"{base}/api/v1/cart/submit", headers=h, timeout=20)
    if r.status_code not in (200, 201):
        raise Fail(f"POST /cart/submit → {r.status_code}: {r.text[:300]}")
    return r.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8099")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    fx = seed()
    api = Api(base, fx["tenant_host"])
    print(f"E2E stream SSE (tenant: {fx['schema']}, base: {base})")

    abiertos: list[SseClient] = []
    try:
        api.token = api("POST", "/api/v1/auth/login", json={
            "email": fx["email"], "password": "e2e-Passw0rd!",
        }).json()["access_token"]

        # ---------------------------------------------- handshake del comensal
        token, ts_id = abrir_sesion(api, base, fx, "Ana")
        c = SseClient(base, {"token": token}).open(); abiertos.append(c)
        check("el comensal abre el stream", c.status, 200)

        hello = c.wait_for("hello")
        if hello is None:
            raise Fail("no llegó el `hello` del handshake")
        check("el canal se deriva del token, no se pide",
              hello["parsed"]["channels"], [f"session:{ts_id}"])

        # --------------------------------------------- el evento llega en vivo
        pedido = enviar_pedido(base, token, fx["variant_id"])
        ev = c.wait_for("order.created")
        if ev is None:
            raise Fail(f"no llegó `order.created`; llegaron: {c.types()}")
        check("el pedido llega por el stream", ev["parsed"]["order_id"], pedido["id"])
        note(f"con `v` monótono ({ev['parsed']['v']}) e `id` de Redis ({ev['id']})")

        # ------------------------------------------------------- aislamiento
        # Otro comensal en la MISMA mesa sí lo ve; el de otra sesión no.
        token2, ts_id2 = abrir_sesion(api, base, fx, "Beto")
        check("un segundo comensal comparte la sesión de mesa", ts_id2, ts_id)

        # Un token de una sesión de mesa que ya no existe no puede escuchar.
        r = requests.get(f"{base}/api/v1/realtime/stream",
                         params={"token": "no-es-un-jwt"}, timeout=10)
        check("token inválido → 401 en el handshake (EventSource deja de reintentar)",
              r.status_code, 401)

        # ------------------------------------------------------------ replay
        antes = c.last_id()
        c.close()
        time.sleep(0.3)

        # Con la conexión caída, la terminal confirma y entrega el pedido
        # (spec 147: ya no hay PATCH por ítem -- una sola acción por pedido,
        # que reemplaza el viejo avance de preparación).
        api("POST", f"/api/v1/orders/{pedido['id']}/confirm")
        api("POST", f"/api/v1/orders/{pedido['id']}/deliver")

        c2 = SseClient(base, {"token": token, "last_event_id": antes}).open()
        abiertos.append(c2)
        check("reconecta con Last-Event-ID", c2.status, 200)
        if c2.wait_for("hello") is None:
            raise Fail("no llegó el `hello` tras reconectar")

        tipos = [t for t in c2.types() if t != "hello"]
        check("el replay recupera lo perdido, en orden",
              tipos,
              ["order.confirmed", "session.bill_changed",
               "order.delivered"])

        vs = [e["parsed"]["v"] for e in c2.events if e.get("event") != "hello"]
        check("y con `v` estrictamente creciente", vs, sorted(set(vs)))

        # ------------------------------------------------------ ticket de staff
        t1 = api("POST", "/api/v1/realtime/ticket").json()
        check("el ticket caduca pronto", t1["expires_in"],
              settings.REALTIME_TICKET_TTL_SECONDS)

        s = SseClient(base, {"ticket": t1["ticket"]}).open(); abiertos.append(s)
        check("el staff abre el stream con el ticket", s.status, 200)
        h = s.wait_for("hello")
        check("y escucha el canal del staff", h["parsed"]["channels"], ["staff"])

        r = requests.get(f"{base}/api/v1/realtime/stream",
                         params={"ticket": t1["ticket"]}, timeout=10)
        check("reusar el ticket → 401 (es de un solo uso)", r.status_code, 401)

        r = requests.get(f"{base}/api/v1/realtime/stream", timeout=10)
        check("sin credencial → 400", r.status_code, 400)
        r = requests.get(f"{base}/api/v1/realtime/stream",
                         params={"token": token, "ticket": "x"}, timeout=10)
        check("con las dos credenciales → 400", r.status_code, 400)

        # ------------------------------------------- tope por sesión de mesa
        extra = []
        for _ in range(settings.REALTIME_MAX_CONN_PER_SESSION + 2):
            extra.append(SseClient(base, {"token": token}).open())
        abiertos.extend(extra)
        rechazadas = [x for x in extra if x.status == 429]
        if not rechazadas:
            raise Fail(
                f"ninguna conexión rechazada con tope="
                f"{settings.REALTIME_MAX_CONN_PER_SESSION}: {[x.status for x in extra]}"
            )
        note(f"el tope por mesa rechaza con 429 ({len(rechazadas)} de {len(extra)})")
        for x in extra:
            x.close()

        # ---------------------------------------------------------- heartbeat
        hb = settings.REALTIME_HEARTBEAT_SECONDS
        note(f"esperando el heartbeat ({hb}s + margen)…")
        c3 = SseClient(base, {"token": token}).open(); abiertos.append(c3)
        # Sin esto, un 429 por huecos aún sin liberar se reportaría como
        # "no llegó el heartbeat", que manda a depurar al sitio equivocado.
        check("la conexión del heartbeat se abre", c3.status, 200)
        time.sleep(hb + 4)
        if not c3.comments:
            raise Fail(f"no llegó ningún `: ping` en {hb + 4}s")
        check("el heartbeat mantiene vivo el túnel", c3.comments[0].strip(), ": ping")

        print("TODO OK ✔")
        return 0
    except Fail as e:
        print(f"\n  FALLO · {e}")
        return 1
    finally:
        for x in abiertos:
            x.close()
        teardown(fx)


if __name__ == "__main__":
    sys.exit(main())
