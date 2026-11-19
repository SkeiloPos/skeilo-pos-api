"""Tests de la nueva funcionalidad — spec 161-suscripcion-pago-wompi, Historia de Usuario 5 (FR-031, FR-037,
SC-009, A-164; contrato `notifications-audience.md`): una notificación puede ir dirigida a un ROL. Con
`audience_role = "ADMIN"` solo los ADMIN la ven —en el listado, en el canal en tiempo real y en el push—; con
`NULL` (todos los eventos de antes de la spec 161) sigue llegando a todo el personal, exactamente como hoy.

No son characterization tests — comportamiento enteramente nuevo; las pruebas congeladas de la spec 077
(`test_notifications.py`) no se tocan y siguen pasando.

    python -m unittest app.characterization_tests.test_notifications_audience -v
"""
from __future__ import annotations

import asyncio
import json
import unittest
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from starlette.testclient import TestClient

from app.api.v1.notifications.router import router as notifications_router
from app.api.v1.realtime import auth as realtime_auth
from app.characterization_tests import plan_fixtures as fx
from app.core import events
from app.core.config import settings
from app.core.db import get_db
from app.core.dependencies import get_current_user
from app.core.events import CH_STAFF, staff_role_channel
from app.core.notifications import dispatch
from app.core.notifications.channels.browser_push import BrowserPushChannel
from app.core.notifications.channels.in_app import InAppChannel
from app.core.notifications.summary import summarize
from app.core.timezone import utc_now_naive
from app.models.notification_event import NotificationEvent
from app.models.push_subscription import PushSubscription

BASE = datetime(2026, 12, 1, 10, 0, 0)


def _notificacion(db, *, audience_role=None, minutos=0, event_type=None, **kw) -> NotificationEvent:
    """Una notificación creada `minutos` después de `BASE`, para controlar el orden sin depender del reloj."""
    kw.setdefault("event_type", event_type or ("subscription.expiring" if audience_role else "order.created"))
    kw.setdefault("related_entity_type", "subscription" if audience_role else "customer_order")
    kw.setdefault("related_entity_id", uuid.uuid4())
    kw.setdefault("payload", {"table_number": 4, "total": "45000"})
    kw.setdefault("purge_at", BASE + timedelta(days=90))
    notif = NotificationEvent(audience_role=audience_role, created_at=BASE + timedelta(minutes=minutos), **kw)
    db.add(notif)
    db.commit()
    return notif


def _app(db, role_name):
    """App mínima con el router real de notificaciones, como un usuario del rol dado (patrón de
    `test_notifications.py`: se sobreescriben `get_db` y `get_current_user`)."""
    app = FastAPI()
    app.include_router(notifications_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4(), role_name=role_name)
    return app


# ======================================================================
# El modelo
# ======================================================================


class ModeloTests(unittest.TestCase):
    def test_audience_role_es_opcional_y_por_defecto_null_todo_el_personal(self):
        db = fx.new_session()
        notif = _notificacion(db)

        db.refresh(notif)

        self.assertIsNone(notif.audience_role)

    def test_guarda_el_rol_destinatario(self):
        db = fx.new_session()
        notif = _notificacion(db, audience_role="ADMIN")

        db.refresh(notif)

        self.assertEqual(notif.audience_role, "ADMIN")


# ======================================================================
# GET /notifications y POST /notifications/{id}/attend
# ======================================================================


class ListadoPorAudienciaTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session(multithreaded=True)  # TestClient atiende el endpoint en otro hilo
        self.todos_1 = _notificacion(self.db, minutos=1)
        self.admin_1 = _notificacion(self.db, audience_role="ADMIN", minutos=2)
        self.todos_2 = _notificacion(self.db, minutos=3, event_type="payment.completed")
        self.admin_2 = _notificacion(self.db, audience_role="ADMIN", minutos=4, event_type="subscription.grace_started")

    def _ids(self, role, **params):
        resp = TestClient(_app(self.db, role)).get("/api/v1/notifications", params=params)
        self.assertEqual(resp.status_code, 200)
        return {item["id"] for item in resp.json()["items"]}, resp.json()

    def test_el_admin_ve_todo(self):
        ids, body = self._ids("ADMIN")

        self.assertEqual(ids, {str(n.id) for n in (self.todos_1, self.admin_1, self.todos_2, self.admin_2)})
        self.assertEqual(body["total"], 4)

    def test_los_demas_roles_solo_ven_lo_dirigido_a_todos(self):
        for rol in ("CASHIER", "MESERO", "KITCHEN"):
            with self.subTest(rol):
                ids, body = self._ids(rol)

                self.assertEqual(ids, {str(self.todos_1.id), str(self.todos_2.id)})
                self.assertEqual(body["total"], 2)  # el total tampoco filtra la existencia de los avisos ocultos

    def test_un_usuario_sin_rol_conocido_solo_ve_lo_dirigido_a_todos(self):
        """Los tests de la spec 077 sustituyen `get_current_user` por un usuario sin `role_name`."""
        app = FastAPI()
        app.include_router(notifications_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4())

        resp = TestClient(app).get("/api/v1/notifications")

        self.assertEqual({i["id"] for i in resp.json()["items"]}, {str(self.todos_1.id), str(self.todos_2.id)})

    def test_la_paginacion_cuenta_solo_lo_visible(self):
        _, body = self._ids("CASHIER", page=1, size=1)

        self.assertEqual((body["total"], len(body["items"])), (2, 1))

    def test_solo_pendientes_respeta_la_audiencia(self):
        ids, _ = self._ids("CASHIER", only_pending="true")

        self.assertEqual(ids, {str(self.todos_1.id), str(self.todos_2.id)})

    def test_el_modo_recuperacion_after_id_respeta_la_audiencia(self):
        ids_admin, _ = self._ids("ADMIN", after_id=str(self.todos_1.id))
        ids_cajero, body = self._ids("CASHIER", after_id=str(self.todos_1.id))

        self.assertEqual(ids_admin, {str(n.id) for n in (self.admin_1, self.todos_2, self.admin_2)})
        self.assertEqual(ids_cajero, {str(self.todos_2.id)})
        self.assertIsNone(body["total"])  # modo recuperación: sin total

    def test_el_listado_ordena_del_mas_reciente_al_mas_antiguo(self):
        resp = TestClient(_app(self.db, "ADMIN")).get("/api/v1/notifications")

        self.assertEqual(
            [i["id"] for i in resp.json()["items"]],
            [str(n.id) for n in (self.admin_2, self.todos_2, self.admin_1, self.todos_1)],
        )

    def test_las_notificaciones_existentes_siguen_igual_para_todos_los_roles(self):
        """FR-037: `audience_role` NULL = el comportamiento de siempre; ningún rol pierde `order.created`."""
        for rol in ("ADMIN", "CASHIER", "MESERO", "KITCHEN"):
            with self.subTest(rol):
                ids, _ = self._ids(rol)

                self.assertIn(str(self.todos_1.id), ids)
                self.assertIn(str(self.todos_2.id), ids)


class AtenderPorAudienciaTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session(multithreaded=True)  # TestClient atiende el endpoint en otro hilo
        self.para_todos = _notificacion(self.db)
        self.para_admin = _notificacion(self.db, audience_role="ADMIN")

    def _atender(self, role, notif):
        return TestClient(_app(self.db, role)).post(f"/api/v1/notifications/{notif.id}/attend")

    def test_el_admin_atiende_un_aviso_dirigido_a_el(self):
        resp = self._atender("ADMIN", self.para_admin)

        self.assertEqual(resp.status_code, 200)
        self.db.refresh(self.para_admin)
        self.assertIsNotNone(self.para_admin.attended_at)

    def test_un_cajero_no_puede_atender_un_aviso_de_admin_y_recibe_404_no_403(self):
        resp = self._atender("CASHIER", self.para_admin)

        self.assertEqual(resp.status_code, 404)  # no se revela que existe
        self.db.refresh(self.para_admin)
        self.assertIsNone(self.para_admin.attended_at)

    def test_un_cajero_si_atiende_lo_dirigido_a_todos(self):
        resp = self._atender("CASHIER", self.para_todos)

        self.assertEqual(resp.status_code, 200)

    def test_atender_sigue_siendo_idempotente(self):
        self.assertEqual(self._atender("ADMIN", self.para_admin).status_code, 200)
        self.assertEqual(self._atender("ADMIN", self.para_admin).status_code, 200)


# ======================================================================
# Tiempo real (SSE)
# ======================================================================


class _RedisFalso:
    """Lo mínimo de Redis que usan `issue_ticket` y `resolve_staff` (`SET` con TTL y `GETDEL`)."""

    def __init__(self):
        self.datos = {}

    async def set(self, key, value, ex=None):
        self.datos[key] = value

    async def getdel(self, key):
        return self.datos.pop(key, None)


class CanalPorRolTests(unittest.TestCase):
    def setUp(self):
        self.redis = _RedisFalso()
        patcher = mock.patch.object(realtime_auth, "redis", self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _suscribir(self, role):
        async def flujo():
            ticket = await realtime_auth.issue_ticket(7, uuid.uuid4(), role)
            return await realtime_auth.resolve_staff(ticket)

        return asyncio.run(flujo())

    def test_el_nombre_del_canal_por_rol(self):
        self.assertEqual(staff_role_channel("ADMIN"), "staff:admin")
        self.assertEqual(staff_role_channel("CASHIER"), "staff:cashier")
        self.assertNotEqual(staff_role_channel("ADMIN"), CH_STAFF)

    def test_el_staff_se_suscribe_al_canal_comun_y_al_de_su_rol(self):
        for rol in ("ADMIN", "CASHIER", "MESERO", "KITCHEN"):
            with self.subTest(rol):
                sub = self._suscribir(rol)

                self.assertEqual(sub.channels, frozenset({CH_STAFF, staff_role_channel(rol)}))
                self.assertEqual((sub.tenant_id, sub.kind), (7, "staff"))

    def test_un_cajero_nunca_recibe_el_canal_del_admin(self):
        self.assertNotIn(staff_role_channel("ADMIN"), self._suscribir("CASHIER").channels)

    def test_un_ticket_sin_rol_conserva_solo_el_canal_comun(self):
        """Un ticket emitido por la versión anterior (30 s de vida) no trae `r`."""
        async def flujo():
            self.redis.datos["rt:ticket:viejo"] = json.dumps({"t": 7, "u": str(uuid.uuid4())})
            return await realtime_auth.resolve_staff("viejo")

        self.assertEqual(asyncio.run(flujo()).channels, frozenset({CH_STAFF}))

    def test_el_ticket_sigue_siendo_de_un_solo_uso(self):
        async def flujo():
            ticket = await realtime_auth.issue_ticket(7, uuid.uuid4(), "ADMIN")
            await realtime_auth.resolve_staff(ticket)
            return await realtime_auth.resolve_staff(ticket)

        with self.assertRaises(Exception) as ctx:
            asyncio.run(flujo())
        self.assertEqual(getattr(ctx.exception, "status_code", None), 401)

    def test_issue_ticket_sin_rol_sigue_funcionando_para_los_llamadores_anteriores(self):
        async def flujo():
            ticket = await realtime_auth.issue_ticket(7, uuid.uuid4())
            return await realtime_auth.resolve_staff(ticket)

        self.assertEqual(asyncio.run(flujo()).channels, frozenset({CH_STAFF}))


class CanalInAppTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()  # se conserva: si se recoge, la notificación queda desvinculada de su sesión

    def _canales_publicados(self, notif):
        with mock.patch.object(events, "publish") as publish:
            InAppChannel(7, mock.Mock()).send(notif)
        self.assertEqual(publish.call_count, 1)
        return list(publish.call_args.kwargs["channels"])

    def test_con_audiencia_se_publica_solo_en_el_canal_del_rol(self):
        notif = _notificacion(self.db, audience_role="ADMIN")

        self.assertEqual(self._canales_publicados(notif), ["staff:admin"])

    def test_sin_audiencia_se_publica_en_el_canal_comun_como_siempre(self):
        notif = _notificacion(self.db)

        self.assertEqual(self._canales_publicados(notif), [CH_STAFF])

    def test_el_tipo_de_evento_y_el_resumen_no_cambian(self):
        notif = _notificacion(self.db, audience_role="ADMIN", event_type="subscription.blocked", payload={})
        with mock.patch.object(events, "publish") as publish:
            InAppChannel(7, mock.Mock()).send(notif)

        kwargs = publish.call_args.kwargs
        self.assertEqual(kwargs["type"], "notification.created")
        self.assertEqual(kwargs["payload"]["event_type"], "subscription.blocked")


# ======================================================================
# Push del navegador
# ======================================================================


class PushPorRolTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        plan = fx.make_plan(self.db)
        self.tenant = fx.make_tenant(self.db, plan=plan)
        self.endpoints = {}
        for rol in ("ADMIN", "CASHIER", "MESERO"):
            user = fx.make_user(self.db, self.tenant, role_name=rol)
            endpoint = f"https://push.example.invalid/{rol.lower()}"
            self.db.add(PushSubscription(user_id=user.id, endpoint=endpoint, p256dh_key="p", auth_key="a", active=True))
            self.endpoints[rol] = endpoint
        self.db.commit()
        patcher = mock.patch.multiple(settings, VAPID_PRIVATE_KEY="k", VAPID_SUBJECT="mailto:x@example.invalid")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _destinos(self, notif):
        with mock.patch("app.core.notifications.channels.browser_push.webpush") as webpush:
            BrowserPushChannel(self.tenant.id, self.db).send(notif)
        return {call.kwargs["subscription_info"]["endpoint"] for call in webpush.call_args_list}

    def test_con_audiencia_admin_solo_se_envia_a_los_dispositivos_de_admins(self):
        notif = _notificacion(self.db, audience_role="ADMIN")

        self.assertEqual(self._destinos(notif), {self.endpoints["ADMIN"]})

    def test_sin_audiencia_se_envia_a_todos_como_siempre(self):
        notif = _notificacion(self.db)

        self.assertEqual(self._destinos(notif), set(self.endpoints.values()))

    def test_un_admin_con_varios_dispositivos_recibe_en_todos(self):
        admin = self.db.execute(
            __import__("sqlalchemy").select(PushSubscription).where(PushSubscription.endpoint == self.endpoints["ADMIN"])
        ).scalar_one()
        self.db.add(PushSubscription(user_id=admin.user_id, endpoint="https://push.example.invalid/admin-2", p256dh_key="p", auth_key="a", active=True))
        self.db.commit()

        destinos = self._destinos(_notificacion(self.db, audience_role="ADMIN"))

        self.assertEqual(destinos, {self.endpoints["ADMIN"], "https://push.example.invalid/admin-2"})

    def test_un_dispositivo_inactivo_no_recibe_aunque_sea_de_admin(self):
        sub = self.db.execute(
            __import__("sqlalchemy").select(PushSubscription).where(PushSubscription.endpoint == self.endpoints["ADMIN"])
        ).scalar_one()
        sub.active = False
        self.db.commit()

        self.assertEqual(self._destinos(_notificacion(self.db, audience_role="ADMIN")), set())


# ======================================================================
# dispatch._dispatch con audiencia
# ======================================================================


class DispatchConAudienciaTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        patcher = mock.patch.object(events, "publish")
        self.publish = patcher.start()
        self.addCleanup(patcher.stop)

    def test_dispatch_persiste_la_audiencia_y_publica_en_su_canal(self):
        notif = dispatch._dispatch(
            self.db, 7, event_type="subscription.expiring", related_entity_type="subscription",
            related_entity_id=uuid.uuid4(), payload={"dias_restantes": 2}, audience_role="ADMIN",
        )
        self.db.refresh(notif)

        self.assertEqual(notif.audience_role, "ADMIN")
        self.assertEqual(list(self.publish.call_args.kwargs["channels"]), ["staff:admin"])

    def test_los_eventos_de_pedidos_y_pagos_siguen_sin_audiencia_y_en_el_canal_comun(self):
        notif = dispatch.notify_order_created(
            self.db, 7, order_id=uuid.uuid4(), table_session_id=None, dining_table_id=None,
            table_number=3, customer_name=None, items_count=2, total=Decimal("45000"),
        )
        self.db.refresh(notif)

        self.assertIsNone(notif.audience_role)
        self.assertEqual(list(self.publish.call_args.kwargs["channels"]), [CH_STAFF])


# ======================================================================
# El texto de los tres avisos
# ======================================================================


class ResumenDeAvisosTests(unittest.TestCase):
    def _resumen(self, event_type, payload):
        notif = NotificationEvent(
            event_type=event_type, related_entity_type="subscription", related_entity_id=uuid.uuid4(),
            payload=payload, purge_at=BASE, audience_role="ADMIN",
        )
        return summarize(notif)

    def test_por_vencer_con_singular_y_plural(self):
        self.assertEqual(
            self._resumen("subscription.expiring", {"dias_restantes": 1}),
            "Tu suscripción vence en 1 día. Paga para evitar interrupciones.",
        )
        self.assertEqual(
            self._resumen("subscription.expiring", {"dias_restantes": 2}),
            "Tu suscripción vence en 2 días. Paga para evitar interrupciones.",
        )

    def test_inicio_de_gracia_con_la_fecha_limite_ya_en_la_zona_del_tenant(self):
        self.assertEqual(
            self._resumen("subscription.grace_started", {"fecha_limite_pago_local": "6 de diciembre"}),
            "Tu suscripción venció. Tienes hasta el 6 de diciembre para pagar sin interrupciones.",
        )

    def test_bloqueado(self):
        self.assertEqual(
            self._resumen("subscription.blocked", {}),
            "Tu suscripción está bloqueada. Paga tu plan para volver a operar.",
        )

    def test_sin_datos_en_el_payload_el_texto_sigue_siendo_util(self):
        self.assertIn("vence", self._resumen("subscription.expiring", {}))
        self.assertIn("venció", self._resumen("subscription.grace_started", {}))

    def test_los_resumenes_anteriores_no_cambian(self):
        self.assertEqual(self._resumen("order.created", {"table_number": 4, "items_count": 2, "total": "45000"}), "Mesa 4 · 2 ítems · $45000")
        self.assertEqual(self._resumen("tipo.desconocido", {}), "tipo.desconocido")


# ======================================================================
# El bootstrap (spec 100) llama a `list_notifications` por nombre de parámetro
# ======================================================================


class BootstrapRespetaLaAudienciaTests(unittest.TestCase):
    """`bootstrap/service.py` llama a `list_notifications(..., _=user)` como función Python. Renombrar ese parámetro
    rompió el bloque en silencio (el bootstrap degrada a `null` en vez de fallar): esta prueba lo protege, y de
    paso fija que el bloque también respeta la audiencia."""

    def setUp(self):
        from app.api.v1.bootstrap.service import build_bootstrap

        self.build_bootstrap = build_bootstrap
        self.db = fx.new_session()
        plan = fx.make_plan(self.db)
        self.tenant = fx.make_tenant(self.db, plan=plan)
        self.admin = fx.make_user(self.db, self.tenant, role_name="ADMIN")
        self.cajero = fx.make_user(self.db, self.tenant, role_name="CASHIER")
        self.db.commit()
        self.para_todos = _notificacion(self.db, minutos=1)
        self.para_admin = _notificacion(self.db, audience_role="ADMIN", minutos=2)

    def _ids(self, user):
        bloque = self.build_bootstrap(self.db, self.tenant, user)["notifications"]
        self.assertIsNotNone(bloque, "el bloque de notificaciones del bootstrap se degradó a null")
        return {str(item.id) for item in bloque.items}

    def test_el_bloque_de_notificaciones_no_se_degrada_a_null(self):
        self.assertIsNotNone(self.build_bootstrap(self.db, self.tenant, self.admin)["notifications"])

    def test_el_admin_ve_todo_en_el_bootstrap(self):
        self.assertEqual(self._ids(self.admin), {str(self.para_todos.id), str(self.para_admin.id)})

    def test_un_cajero_solo_ve_lo_dirigido_a_todos_en_el_bootstrap(self):
        self.assertEqual(self._ids(self.cajero), {str(self.para_todos.id)})


if __name__ == "__main__":
    unittest.main()
