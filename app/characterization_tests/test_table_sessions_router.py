"""Characterization tests de los 7 endpoints de
`app/api/v1/table_sessions/router.py` (specs/016-caracterizacion-table-sessions,
Historia 3).

Invoca las funciones de endpoint directamente como funciones Python (research.md
§1): ningún endpoint de `table_sessions/router.py` abre su propio contexto (a
diferencia de `cart/router.py`), así que basta con pasar dobles mínimos
(`SimpleNamespace`, `table_sessions_fixtures.make_tenant_double`/
`make_user_double`) donde el endpoint recibiría `Depends(get_tenant)`/
`Depends(get_current_user)` — `Depends(...)` nunca se resuelve al llamar la
función directamente.

Reutiliza el comportamiento de `service.py` ya congelado en
`test_table_sessions_split_blindaje.py` y `test_table_sessions_service.py` como
línea base (spec.md lo declara explícitamente: esta historia depende de las dos
anteriores, que ya cubren `service.py`).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_table_sessions_router -v
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from app.characterization_tests import table_sessions_fixtures as fx
from app.api.v1.table_sessions import router as ts_router
from app.api.v1.table_sessions import service
from app.api.v1.table_sessions.schemas import (
    AssignmentsIn, CloseSessionIn, CloseSessionResponse, ItemAssignmentIn,
    ParticipantCreateIn, SessionBillResponse, TableSessionResponse,
)
from app.api.v1.sales.schemas import PaymentIn
from app.core.timezone import utc_now_naive

PRECIO = Decimal("10000")


def _status_code_for(endpoint_name: str) -> int:
    """Status code declarado en el decorador de la ruta (`@router.get/post/...`),
    para las aserciones de código de respuesta que esta Historia congela sin
    pasar por `fastapi.testclient` (research.md §1: `Depends` nunca se resuelve
    al invocar la función Python directamente, así que el único código de
    estado observable "de verdad" al no usar ASGI es el que quedó registrado en
    la ruta). `APIRoute.status_code` es `None` cuando el decorador no lo fija
    explícito (`add_participant`/`remove_participant` sí lo hacen; el resto no)
    — FastAPI resuelve ese `None` a 200 en tiempo de respuesta real."""
    for route in ts_router.router.routes:
        if getattr(route, "endpoint", None) is not None and route.endpoint.__name__ == endpoint_name:
            return route.status_code or 200
    raise AssertionError(f"No existe ninguna ruta para el endpoint {endpoint_name!r}")


class TestTableSessionsRouter(unittest.TestCase):
    # ------------------------------------------------------------- Helpers

    def _seed_billable_session(self):
        db = fx.new_session()
        table = fx.make_dining_table(db, status="ocupada")
        ts = fx.make_table_session(db, table=table)
        ana = fx.make_participant(db, table_session=ts, display_name="Ana", display_label="Ana")

        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        order = fx.make_customer_order(db, ts, status="abierta")
        item = fx.make_order_item(db, order, variant, participant_id=ana.id)

        register = fx.make_cash_register(db)
        shift = fx.make_cash_shift(db, register=register)
        method = fx.make_payment_method(db)

        db.commit()
        return dict(
            db=db, table=table, ts=ts, ana=ana, order=order, item=item,
            shift=shift, method=method,
            tenant=fx.make_tenant_double(id=1), user=fx.make_user_double(),
        )

    def _pago(self, method_id, amount=PRECIO):
        return PaymentIn(payment_method_id=method_id, amount=amount).model_dump(mode="json")

    # -------------------------------------------------- GET /table-sessions (T030)

    def test_list_sessions_endpoint(self):
        """CONGELA comportamiento actual: invocado directamente con
        `only_active=` explícito (nunca el default `Query(True, ...)` sin
        resolver, research.md §1), devuelve la lista de sesiones esperada."""
        db = fx.new_session()
        t1 = fx.make_dining_table(db)
        ts_active = fx.make_table_session(db, table=t1, status="active")
        t2 = fx.make_dining_table(db)
        fx.make_table_session(db, table=t2, status="closed")
        db.commit()
        user = fx.make_user_double()

        resp = ts_router.list_sessions(only_active=True, db=db, _=user)

        self.assertEqual({s.id for s in resp}, {ts_active.id})
        self.assertEqual(_status_code_for("list_sessions"), 200)

    # ----------------------------------------------- GET /table-sessions/{id} (T031)

    def test_get_session_endpoint_existente_y_404(self):
        """CONGELA comportamiento actual (spec.md Historia 3, escenario 1):
        sesión existente responde con `TableSessionResponse` incluyendo sus
        comensales; un id inexistente propaga la misma `HTTPException` 404 que
        `service.get_session` ya congeló."""
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        fx.make_participant(db, table_session=ts, display_name="Ana", display_label="Ana")
        db.commit()
        user = fx.make_user_double()

        got = ts_router.get_session(ts.id, db=db, _=user)
        self.assertEqual(got.id, ts.id)
        self.assertEqual(len(got.participants), 1)
        self.assertEqual(_status_code_for("get_session"), 200)

        from uuid import uuid4
        with self.assertRaises(HTTPException) as ctx:
            ts_router.get_session(uuid4(), db=db, _=user)
        self.assertEqual(ctx.exception.status_code, 404)

    # ------------------------------------- POST /participants (add_participant, T032)

    def test_add_participant_endpoint_nombre_vacio_422_y_valido_201(self):
        """CONGELA comportamiento actual (spec.md Historia 3, escenario 2): un
        `display_name` vacío o solo espacios responde 422 sin crear el comensal
        (`ParticipantCreateIn.min_length=1` lo bloquea antes de llegar al
        servicio para la cadena vacía; el servicio mismo rechaza la de solo
        espacios tras `.strip()`); un nombre válido responde 201 con
        `display_label` desambiguado."""
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        fx.make_participant(db, table_session=ts, display_name="Ana", display_label="Ana")
        db.commit()
        tenant = fx.make_tenant_double(id=1)
        user = fx.make_user_double()

        with self.assertRaises(Exception):
            ParticipantCreateIn(display_name="")

        with self.assertRaises(HTTPException) as ctx:
            ts_router.add_participant(
                ts.id, ParticipantCreateIn.model_construct(display_name="   "),
                db=db, tenant=tenant, _=user,
            )
        self.assertEqual(ctx.exception.status_code, 422)

        creado = ts_router.add_participant(
            ts.id, ParticipantCreateIn(display_name="Ana"), db=db, tenant=tenant, _=user,
        )
        self.assertEqual(creado.display_name, "Ana")
        self.assertEqual(creado.display_label, "Ana (2)")
        self.assertEqual(_status_code_for("add_participant"), 201)

    # --------------------------------------- DELETE /participants/{id} (T033)

    def test_remove_participant_endpoint_con_productos_asignados_409(self):
        """CONGELA comportamiento actual (spec.md Historia 3, escenario 3): un
        comensal con productos asignados responde 409 con el detalle de cuántos
        productos tiene asignados — congelando el contrato de error del
        endpoint (delegado íntegramente en `service.remove_participant`)."""
        s = self._seed_billable_session()

        with self.assertRaises(HTTPException) as ctx:
            ts_router.remove_participant(
                s["ts"].id, s["ana"].id, db=s["db"], tenant=s["tenant"], _=s["user"],
            )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("items", ctx.exception.detail)
        self.assertEqual(ctx.exception.detail["items"], 1)
        self.assertEqual(_status_code_for("remove_participant"), 204)

    # ----------------------------------------------- PUT /assignments (T034)

    def test_set_assignments_endpoint_responde_bill_recalculada(self):
        """CONGELA comportamiento actual (spec.md Historia 3, escenario 4): un
        lote de asignaciones válido responde con la `SessionBillResponse` ya
        recalculada, sin exigir una segunda llamada a `GET .../bill`."""
        s = self._seed_billable_session()
        beto = fx.make_participant(s["db"], table_session=s["ts"], display_name="Beto", display_label="Beto")
        s["db"].commit()

        body = AssignmentsIn(assignments=[
            ItemAssignmentIn(order_item_id=s["item"].id, participant_id=beto.id),
        ])

        resp = ts_router.set_assignments(
            s["ts"].id, body, db=s["db"], tenant=s["tenant"], _=s["user"],
        )

        self.assertIsInstance(resp, SessionBillResponse)
        por_comensal = {line.participant_id: line.subtotal for line in resp.split}
        self.assertEqual(por_comensal[beto.id], PRECIO)
        self.assertEqual(_status_code_for("set_assignments"), 200)

        item = s["db"].get(type(s["item"]), s["item"].id)
        self.assertEqual(item.participant_id, beto.id)

    # -------------------------------------------------------- GET /bill (T035)

    def test_session_bill_endpoint_delega_en_compute_bill(self):
        """CONGELA comportamiento actual: `GET .../bill` delega íntegramente en
        `service.compute_bill` y responde exactamente igual que la función ya
        congelada en `test_table_sessions_service.py`."""
        s = self._seed_billable_session()

        via_router = ts_router.session_bill(s["ts"].id, db=s["db"], _=s["user"])
        via_service = service.compute_bill(s["db"], s["ts"].id)

        self.assertEqual(via_router, via_service)
        self.assertEqual(_status_code_for("session_bill"), 200)

    # ------------------------------------------------------- POST /close (T036)

    def test_close_session_endpoint_unified_200(self):
        """CONGELA comportamiento actual (spec.md Historia 3, escenario 5): un
        `CloseSessionIn` válido para `billing_mode='unified'` responde con
        `CloseSessionResponse` (`table_session` ya `closed`, `sale_ids` con
        exactamente una venta)."""
        s = self._seed_billable_session()
        data = CloseSessionIn.model_validate({
            "cash_shift_id": str(s["shift"].id),
            "billing_mode": "unified",
            "payments": [self._pago(s["method"].id)],
        })

        # `notify_payment_completed` (spec 077) consulta `shared.tenants`,
        # tabla que este fixture no incluye (fuera de su alcance) — se
        # parchea para no interferir con lo que esta prueba congela.
        with mock.patch.object(service, "notify_payment_completed"):
            resp = ts_router.close_session(
                s["ts"].id, data, db=s["db"], user=s["user"], tenant=s["tenant"],
            )

        self.assertIsInstance(resp, CloseSessionResponse)
        self.assertEqual(resp.table_session.status, "closed")
        self.assertEqual(len(resp.sale_ids), 1)
        self.assertEqual(_status_code_for("close_session"), 200)


class TestSessionClosedNotifications(unittest.TestCase):
    """spec 089 (A-95, contracts/session-closed.md): TODO camino que cierra la sesión de mesa
    notifica al comensal con `session.closed` (después del commit), y los dos 401 de cierre llevan
    `X-Session-State: closed` sin tocar su cuerpo."""

    def _seed(self, *, comensal_cerrado=True):
        from app.characterization_tests import cart_fixtures as cf
        db = cf.new_session()
        table = cf.make_dining_table(db, status="ocupada")
        ts = cf.make_table_session(db, table=table)
        ana = cf.make_participant(db, table_session=ts, display_name="Ana")
        if comensal_cerrado:
            ana.status = "closed"
        db.commit()
        return db, table, ts, ana

    def _release(self, db, table):
        from app.api.v1.orders import router as orders_router
        return orders_router.release_table(
            table.id, db=db, user=SimpleNamespace(id=uuid.uuid4(), name="Cajero"),
            tenant=SimpleNamespace(id=7),
        )

    # ------------------------------------------------------------ release_table

    def test_release_table_publica_un_session_closed_released_tras_el_commit(self):
        db, table, ts, ana = self._seed()
        orden = []

        real_commit = db.commit

        def commit_espia():
            orden.append("commit")
            return real_commit()

        def publicar(*a, **kw):
            orden.append("evento")

        with mock.patch.object(db, "commit", commit_espia), \
                mock.patch("app.core.events.session_closed", side_effect=publicar) as ev, \
                mock.patch("app.core.events.table_status_changed"):
            self._release(db, table)

        ev.assert_called_once_with(
            7, table_session_id=ts.id, dining_table_id=table.id, reason="released"
        )
        self.assertEqual(orden, ["commit", "evento"])  # nunca antes del commit

    def test_release_table_con_ordenes_sin_cerrar_responde_409_y_no_emite(self):
        from app.characterization_tests import cart_fixtures as cf
        db, table, ts, ana = self._seed()
        cf.make_customer_order(db, ana, status="abierta")
        db.commit()

        with mock.patch("app.core.events.session_closed") as ev, \
                mock.patch("app.core.events.table_status_changed"):
            with self.assertRaises(HTTPException) as ctx:
                self._release(db, table)

        self.assertEqual(ctx.exception.status_code, 409)
        ev.assert_not_called()

    def test_release_table_de_una_mesa_sin_sesion_activa_no_emite(self):
        from app.characterization_tests import cart_fixtures as cf
        db = cf.new_session()
        table = cf.make_dining_table(db, status="ocupada")
        db.commit()

        with mock.patch("app.core.events.session_closed") as ev, \
                mock.patch("app.core.events.table_status_changed"):
            self._release(db, table)

        ev.assert_not_called()

    # --------------------------------------------- cierre automático de sesión vacía

    def test_leave_session_del_ultimo_comensal_notifica_empty(self):
        from app.api.v1.cart import service as cart_service
        db, table, ts, ana = self._seed(comensal_cerrado=False)

        with mock.patch("app.core.events.session_closed") as ev:
            cart_service.leave_session(db, ana, tenant_id=7)

        ev.assert_called_once_with(
            7, table_session_id=ts.id, dining_table_id=table.id, reason="empty"
        )

    def test_leave_session_con_pedidos_por_cobrar_no_cierra_ni_notifica(self):
        from app.api.v1.cart import service as cart_service
        from app.characterization_tests import cart_fixtures as cf
        db, table, ts, ana = self._seed(comensal_cerrado=False)
        cf.make_customer_order(db, ana, status="abierta")
        db.commit()

        with mock.patch("app.core.events.session_closed") as ev:
            cart_service.leave_session(db, ana, tenant_id=7)

        ev.assert_not_called()
        self.assertEqual(ts.status, "active")

    def test_leave_session_sin_tenant_conserva_el_comportamiento_de_siempre(self):
        from app.api.v1.cart import service as cart_service
        db, table, ts, ana = self._seed(comensal_cerrado=False)

        with mock.patch("app.core.events.session_closed") as ev:
            cart_service.leave_session(db, ana)

        ev.assert_not_called()
        self.assertEqual(ts.status, "closed")

    def test_cancel_my_order_del_ultimo_pedido_notifica_empty(self):
        from app.api.v1.cart import service as cart_service
        from app.characterization_tests import cart_fixtures as cf
        db, table, ts, ana = self._seed(comensal_cerrado=True)
        orden = cf.make_customer_order(db, ana, status="recibida")
        db.commit()

        def cancelar(*_a, **_kw):
            orden.status = "cancelada"  # lo que hace la cancelación real: deja de haber algo por cobrar
            return orden

        with mock.patch("app.core.events.session_closed") as ev, \
                mock.patch("app.api.v1.orders.checkout.cancel_order", side_effect=cancelar):
            cart_service.cancel_my_order(db, ana, orden.id, "me arrepentí", tenant_id=7)

        ev.assert_called_once_with(
            7, table_session_id=ts.id, dining_table_id=table.id, reason="empty"
        )

    def test_abandon_expired_notifica_empty_tras_el_commit(self):
        from app.core import qr_context
        db, table, ts, ana = self._seed(comensal_cerrado=False)

        with mock.patch("app.core.events.session_closed") as ev:
            qr_context._abandon_expired(db, ana, SimpleNamespace(id=7))

        ev.assert_called_once_with(
            7, table_session_id=ts.id, dining_table_id=table.id, reason="empty"
        )

    # ------------------------------------------ X-Session-State en los 401 de cierre

    @contextmanager
    def _open(self, db, participant, table_session, *, verify=None):
        """Abre el `open_session_context` REAL con el token, el tenant y la BD sustituidos."""
        from app.core import qr_context
        from app.core.qr_token import SessionClaims

        claims = SessionClaims(
            tenant_id=7, table_id=table_session.dining_table_id,
            participant_id=participant.id, table_session_id=table_session.id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        @contextmanager
        def fake_with_db(_schema):
            yield db

        with mock.patch.object(qr_context, "verify_session_token", verify or (lambda _t: claims)), \
                mock.patch.object(qr_context, "resolve_tenant_by_id", return_value=SimpleNamespace(id=7, schema="s")), \
                mock.patch.object(qr_context, "with_db", fake_with_db), \
                mock.patch("app.core.events.session_closed"):
            yield qr_context

    def _401(self, qr_context):
        with self.assertRaises(HTTPException) as ctx:
            with qr_context.open_session_context("tok"):
                pass
        self.assertEqual(ctx.exception.status_code, 401)
        return ctx.exception

    def test_comensal_cerrado_401_sesion_no_activa_con_cabecera_closed(self):
        db, table, ts, ana = self._seed(comensal_cerrado=True)
        with self._open(db, ana, ts) as qr:
            exc = self._401(qr)
        self.assertEqual(exc.detail, "Sesión no activa")  # el cuerpo no cambia
        self.assertEqual((exc.headers or {}).get("X-Session-State"), "closed")

    def test_sesion_cerrada_401_con_cabecera_closed_y_mismo_mensaje(self):
        db, table, ts, ana = self._seed(comensal_cerrado=False)
        ts.status = "closed"
        db.commit()
        with self._open(db, ana, ts) as qr:
            exc = self._401(qr)
        self.assertEqual(
            exc.detail, "La mesa ya no tiene esta sesión abierta. Vuelve a escanear el QR."
        )
        self.assertEqual((exc.headers or {}).get("X-Session-State"), "closed")

    def test_el_token_de_la_sesion_anterior_sigue_en_401_tras_reabrir_la_mesa(self):
        """FR-017: reabrir u ocupar la mesa NO revive la sesión: el token viejo apunta a otra
        `table_session`."""
        from app.characterization_tests import cart_fixtures as cf
        db, table, ts, ana = self._seed(comensal_cerrado=True)
        ts.status = "closed"
        db.commit()
        cf.make_table_session(db, table=table)  # la mesa se vuelve a ocupar: sesión nueva activa
        db.commit()
        with self._open(db, ana, ts) as qr:
            exc = self._401(qr)
        self.assertEqual((exc.headers or {}).get("X-Session-State"), "closed")

    def test_401_por_vencimiento_no_lleva_la_cabecera(self):
        from app.core.qr_token import SessionExpiredError
        db, table, ts, ana = self._seed(comensal_cerrado=False)

        def vencido(_t):
            raise SessionExpiredError("exp")

        with self._open(db, ana, ts, verify=vencido) as qr:
            exc = self._401(qr)
        self.assertEqual(exc.detail, "Sesión expirada. Vuelve a escanear el QR.")
        self.assertNotIn("X-Session-State", exc.headers or {})

    def test_401_por_inactividad_no_lleva_la_cabecera(self):
        db, table, ts, ana = self._seed(comensal_cerrado=False)
        # un segundo comensal abierto evita que `_abandon_expired` cierre la sesión
        from app.characterization_tests import cart_fixtures as cf
        cf.make_participant(db, table_session=ts, display_name="Beto")
        # Las columnas de instante se guardan en UTC: `datetime.now()` (hora local de la máquina) fallaba
        # con `TZ=Asia/Tokyo` (spec 138, FR-012).
        ana.expires_at = utc_now_naive() - timedelta(minutes=5)
        db.commit()
        with self._open(db, ana, ts) as qr:
            exc = self._401(qr)
        self.assertIn("inactividad", exc.detail)
        self.assertNotIn("X-Session-State", exc.headers or {})

    def test_401_por_duracion_maxima_no_lleva_la_cabecera(self):
        db, table, ts, ana = self._seed(comensal_cerrado=False)
        ts.opened_at = utc_now_naive() - timedelta(days=3)
        db.commit()
        with self._open(db, ana, ts) as qr:
            exc = self._401(qr)
        self.assertIn("duración máxima", exc.detail)
        self.assertNotIn("X-Session-State", exc.headers or {})

    def test_401_por_token_invalido_no_lleva_la_cabecera(self):
        from app.core.qr_token import SessionInvalidError
        db, table, ts, ana = self._seed(comensal_cerrado=False)

        def invalido(_t):
            raise SessionInvalidError("x")

        with self._open(db, ana, ts, verify=invalido) as qr:
            exc = self._401(qr)
        self.assertEqual(exc.detail, "Token de sesión inválido")
        self.assertNotIn("X-Session-State", exc.headers or {})

    # -------------------------- FR-016: con la sesión cerrada, TODA acción responde 401

    def test_con_la_sesion_cerrada_cada_accion_del_comensal_responde_401_incluso_con_cuentas_pendientes(self):
        """Todas las acciones del comensal (enviar pedido, iniciar pago, adjuntar comprobante,
        cancelar, actualizar/editar el carrito) pasan por `get_session_context` →
        `open_session_context`, que rechaza con 401 a un comensal cerrado sin excepciones por
        cuentas pendientes."""
        from app.api.v1.cart import router as cart_router
        from app.characterization_tests import cart_fixtures as cf
        from app.core.qr_context import get_session_context

        acciones = {
            ("POST", "/cart/submit"): "enviar pedido",
            ("POST", "/cart/orders/{order_id}/cancel"): "cancelar",
            ("POST", "/cart/orders/{order_id}/payment-attempts"): "iniciar pago",
            ("POST", "/cart/payment-receipt/presign"): "presign de comprobante",
            ("POST", "/cart/payment-attempts/{attempt_id}/receipt/presign"): "presign de comprobante",
            ("POST", "/cart/payment-attempts/{attempt_id}/receipt"): "adjuntar comprobante",
            ("POST", "/cart/items"): "agregar al carrito",
            ("PATCH", "/cart/items/{item_id}"): "actualizar/editar el carrito",
            ("DELETE", "/cart/items/{item_id}"): "quitar del carrito",
        }
        protegidas = {}
        for route in cart_router.router.routes:
            deps = {d.call for d in route.dependant.dependencies}
            for method in getattr(route, "methods", set()):
                protegidas[(method, route.path)] = get_session_context in deps
        faltan = [k for k in acciones if k not in protegidas]
        self.assertEqual(faltan, [], f"rutas de acción no encontradas: {faltan}")
        for key, nombre in acciones.items():
            with self.subTest(accion=nombre, ruta=key):
                self.assertTrue(protegidas[key], f"{nombre} no pasa por get_session_context")

        # Y ese contexto rechaza con 401 a un comensal cerrado que aún tiene una cuenta pendiente.
        db, table, ts, ana = self._seed(comensal_cerrado=True)
        cf.make_customer_order(db, ana, status="recibida")
        db.commit()
        with self._open(db, ana, ts) as qr:
            exc = self._401(qr)
        self.assertEqual((exc.headers or {}).get("X-Session-State"), "closed")


if __name__ == "__main__":
    unittest.main()
