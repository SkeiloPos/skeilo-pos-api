"""Tests de notificaciones en tiempo real multi-tenant (spec
077-notificaciones-tiempo-real).

No son characterization tests: toda esta funcionalidad es comportamiento
**nuevo**, verificado contra `spec.md`/`data-model.md`/`contracts/` del
propio spec — mismo precedente que `test_operational_log.py` (spec 074).

La prueba central es la que exige el checklist de aceptación de la spec
(SC-005): "un evento del tenant A nunca llega a un suscriptor del tenant B".
Se demuestra en dos capas independientes, ambas ejercitando código de
producción real, sin mocks del mecanismo que se está verificando:

1. **Transporte** (`FanoutAislaPorTenantTests`): el `EventBus` en memoria de
   `app/core/event_bus.py` — el mismo que alimenta `/realtime/stream` — nunca
   reparte un evento del tenant A a un suscriptor registrado bajo el tenant
   B, aun cuando ambos usan el **mismo nombre de canal** (`staff`). El
   aislamiento no viene de que los canales no choquen entre tenants, sino de
   que el índice de suscriptores (`EventBus._subs`) está separado por
   `tenant_id` (research.md §1, §8).
2. **API REST** (`NotificacionesApiAislaEntreTenantsTests`): ni `GET
   /notifications` ni `POST /notifications/{id}/attend` de un tenant pueden
   ver o mutar una fila de otro — cada tenant vive en su propia sesión de
   base de datos (equivalente al schema-per-tenant real), y ningún endpoint
   acepta un identificador de tenant como parámetro (contracts/
   notifications-api.md § Aislamiento).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_notifications -v
"""
from __future__ import annotations

import asyncio
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.api.v1.notifications.router import router as notifications_router
from app.core.db import get_db
from app.core.dependencies import get_current_user
from app.core.event_bus import Event, EventBus, Subscription
from app.core.events import CH_STAFF
from app.core.models import Base
from app.core.notifications import dispatch
from app.core.notifications.summary import summarize
from app.models.notification_event import NotificationEvent
from app.models.plan import Plan  # noqa: F401 - registra la tabla "plans" (FK de Tenant) en Base.metadata

TENANT_A = 101
TENANT_B = 202


# `notification_events.payload` es `postgresql.JSONB` — mismo shim que ya
# registran `cart_fixtures.py`/`orders_fixtures.py`/etc: sin él, `create_all`
# falla con `CompileError` sobre SQLite antes de crear una sola tabla. No
# toca el modelo ni Postgres, solo cómo se compila para este test.
@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json_on_sqlite(element, compiler, **kw):  # pragma: no cover
    return "JSON"


#: `dispatch._retention_days()` hace `db.get(Tenant, tenant_id)` — sin la
#: tabla `tenants` (y `plans`, su FK NOT NULL) la sesión ni siquiera puede
#: ejecutar ese SELECT. No se siembra ninguna fila: el `tenant_id` de estos
#: tests no existe en ninguna, así que cae al fallback de 90 días, que es
#: justo lo que estos tests necesitan (no están probando retención).
_TABLE_NAMES = ["notification_events", "notification_channel_preferences", "tenants", "plans"]


def _new_notifications_session() -> Session:
    """Sesión SQLite en memoria — cada tenant de este módulo obtiene la suya,
    igual que en producción cada uno vive en su propio schema de Postgres
    (`schema_translate_map` colapsa aquí tanto `tenant` como `shared`)."""
    tables = [t for t in Base.metadata.tables.values() if t.name in _TABLE_NAMES]
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    conn = engine.connect().execution_options(
        schema_translate_map={"tenant": None, "shared": None}
    )
    Base.metadata.create_all(bind=conn, tables=tables)
    conn.commit()
    return Session(bind=conn)


def _make_notification(db: Session, **kw) -> NotificationEvent:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    kw.setdefault("event_type", "order.created")
    kw.setdefault("related_entity_type", "customer_order")
    kw.setdefault("related_entity_id", uuid.uuid4())
    kw.setdefault("payload", {"table_number": 4, "total": "45000"})
    kw.setdefault("purge_at", now + timedelta(days=90))
    obj = NotificationEvent(**kw)
    db.add(obj)
    db.commit()
    return obj


def _register(bus: EventBus, tenant_id: int, channels: frozenset[str]) -> Subscription:
    """Registra un suscriptor sin pasar por `EventBus.subscribe()`: ese
    método arranca un lector de Redis real por tenant (`_read_loop`), que
    este test no necesita — lo que se está verificando es el reparto en
    memoria (`_fanout`), no la lectura de Redis."""
    sub = Subscription(tenant_id=tenant_id, channels=channels, queue=asyncio.Queue(maxsize=100))
    bus._subs.setdefault(tenant_id, set()).add(sub)
    return sub


def _build_app(db: Session, user_id: uuid.UUID) -> FastAPI:
    """App mínima con el router real de notificaciones, "como" un tenant:
    `get_db` se sobreescribe a la sesión de ESE tenant y `get_current_user` a
    un usuario suyo — igual patrón que `super_admin_http_fixtures.build_app`.
    Ningún override toca `get_tenant`: al reemplazar `get_db` directamente,
    FastAPI nunca resuelve el árbol de dependencias original."""
    app = FastAPI()
    app.include_router(notifications_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=user_id)
    return app


# ======================================================================
# Capa 1 — transporte (EventBus en memoria, sin Redis)
# ======================================================================


class FanoutAislaPorTenantTests(unittest.TestCase):
    def test_un_evento_del_tenant_a_nunca_llega_al_suscriptor_del_tenant_b(self):
        """Mismo canal (`staff`) en ambos tenants a propósito: si el
        aislamiento dependiera de que los nombres de canal no choquen, este
        test lo pondría en evidencia."""

        async def _run():
            bus = EventBus()
            sub_a = _register(bus, TENANT_A, frozenset({CH_STAFF}))
            sub_b = _register(bus, TENANT_B, frozenset({CH_STAFF}))

            evento = Event(
                id="1-0", type="notification.created", channels=frozenset({CH_STAFF}),
                at="2026-09-07T00:00:00Z",
                data={"notification_id": str(uuid.uuid4()), "summary": "Mesa 4 · $45.000"},
                v=1,
            )
            # Directo a `_fanout`: es exactamente lo que hace el lector de
            # Redis por tenant tras un XREAD (`_read_loop`) — se ejercita el
            # reparto real sin necesitar Redis corriendo para el test.
            bus._fanout(TENANT_A, evento)

            self.assertEqual(sub_a.queue.qsize(), 1)
            self.assertEqual(sub_b.queue.qsize(), 0)

            recibido = sub_a.queue.get_nowait()
            self.assertEqual(recibido.data["summary"], "Mesa 4 · $45.000")

        asyncio.run(_run())

    def test_un_suscriptor_solo_recibe_los_canales_del_tenant_al_que_se_suscribio(self):
        """Complementario: aunque alguien lograra suscribirse con los canales
        "correctos", si quedó registrado bajo el `tenant_id` equivocado no
        recibe nada — el índice de reparto es por tenant, no por canal."""

        async def _run():
            bus = EventBus()
            sub_b = _register(bus, TENANT_B, frozenset({CH_STAFF}))

            evento_de_a = Event(
                id="2-0", type="notification.created", channels=frozenset({CH_STAFF}),
                at="2026-09-07T00:00:01Z", data={}, v=2,
            )
            bus._fanout(TENANT_A, evento_de_a)
            self.assertEqual(sub_b.queue.qsize(), 0)

            evento_de_b = Event(
                id="3-0", type="notification.created", channels=frozenset({CH_STAFF}),
                at="2026-09-07T00:00:02Z", data={}, v=3,
            )
            bus._fanout(TENANT_B, evento_de_b)
            self.assertEqual(sub_b.queue.qsize(), 1)

        asyncio.run(_run())


# ======================================================================
# Capa 2 — persistencia (`dispatch.py` escribe en el tenant correcto)
# ======================================================================


class DispatchPersisteSoloEnElTenantIndicadoTests(unittest.TestCase):
    def test_notify_order_created_no_escribe_en_la_sesion_de_otro_tenant(self):
        db_a = _new_notifications_session()
        db_b = _new_notifications_session()

        # `_retention_days`/canales no son el objeto de este test: se acepta
        # el fallback (90 días) sin consultar `shared.tenants`, y el canal
        # `in_app` intenta publicar por Redis real — mismo criterio fail-open
        # que en producción, así que un Redis inalcanzable en CI no rompe el
        # test (`events.publish` nunca lanza).
        dispatch.notify_order_created(
            db_a, TENANT_A,
            order_id=uuid.uuid4(), table_session_id=uuid.uuid4(), dining_table_id=uuid.uuid4(),
            table_number=4, customer_name="Ana", items_count=2, total=Decimal("30000"),
        )

        self.assertEqual(db_a.query(NotificationEvent).count(), 1)
        self.assertEqual(db_b.query(NotificationEvent).count(), 0)


# ======================================================================
# Capa 3 — API REST (GET /notifications, POST .../attend)
# ======================================================================


class NotificacionesApiAislaEntreTenantsTests(unittest.TestCase):
    def setUp(self):
        self.db_a = _new_notifications_session()
        self.db_b = _new_notifications_session()
        self.user_a = uuid.uuid4()
        self.user_b = uuid.uuid4()
        self.client_a = TestClient(_build_app(self.db_a, self.user_a))
        self.client_b = TestClient(_build_app(self.db_b, self.user_b))

        self.notif_a = _make_notification(self.db_a, payload={"table_number": 4, "total": "45000"})

    def test_get_notifications_del_tenant_b_no_incluye_nada_del_tenant_a(self):
        resp_b = self.client_b.get("/api/v1/notifications")
        self.assertEqual(resp_b.status_code, 200)
        self.assertEqual(resp_b.json()["items"], [])

        resp_a = self.client_a.get("/api/v1/notifications")
        self.assertEqual(resp_a.status_code, 200)
        ids = [item["id"] for item in resp_a.json()["items"]]
        self.assertEqual(ids, [str(self.notif_a.id)])

    def test_el_tenant_b_no_puede_marcar_atendida_una_notificacion_del_tenant_a(self):
        """T041: el `id` de una notificación es un UUID global, así que nada
        impide que el tenant B lo *intente* — lo que se verifica es que su
        propia sesión (su propio schema, en producción) no la contiene, y el
        endpoint responde 404, nunca 403 con detalle (contracts/
        notifications-api.md § Aislamiento: no filtrar por tenant debe ser
        indistinguible de "no existe")."""
        resp = self.client_b.post(f"/api/v1/notifications/{self.notif_a.id}/attend")
        self.assertEqual(resp.status_code, 404)

        # Y, del otro lado, sigue intacta: el intento fallido del tenant B no
        # la tocó.
        self.db_a.expire_all()
        fresh = self.db_a.get(NotificationEvent, self.notif_a.id)
        self.assertIsNone(fresh.attended_at)

    def test_el_tenant_a_si_puede_marcar_atendida_su_propia_notificacion(self):
        """Control positivo: el 404 de arriba es aislamiento, no un bug en el
        endpoint — el mismo `id`, desde el tenant correcto, funciona."""
        resp = self.client_a.post(f"/api/v1/notifications/{self.notif_a.id}/attend")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIsNotNone(body["attended_at"])
        self.assertEqual(body["attended_by_user_id"], str(self.user_a))


class SummarizePagoFormatoDeMonedaTests(unittest.TestCase):
    """Spec 125 (FR-005): el resumen de un `payment.completed` debe respetar
    el formato de moneda estándar ("$ 22.000"), no la interpolación cruda del
    valor ("$22000") que mostraba antes. No requiere base de datos: `summarize()`
    es una función pura sobre un `NotificationEvent` sin persistir."""

    def _event(self, payload: dict) -> NotificationEvent:
        return NotificationEvent(
            event_type="payment.completed",
            related_entity_type="sale",
            related_entity_id=uuid.uuid4(),
            payload=payload,
        )

    def test_formatea_el_total_con_punto_de_miles_y_espacio_tras_el_simbolo(self):
        self.assertEqual(
            summarize(self._event({"total": "22000"})),
            "Pago recibido · $ 22.000",
        )

    def test_acepta_total_como_decimal_no_solo_string(self):
        self.assertEqual(
            summarize(self._event({"total": Decimal("22000")})),
            "Pago recibido · $ 22.000",
        )

    def test_sin_total_cae_al_texto_generico(self):
        self.assertEqual(summarize(self._event({})), "Pago recibido")

    def test_order_created_no_se_toca_por_esta_spec_sigue_con_el_bug_conocido(self):
        """Fuera de alcance (research.md R2): mismo bug en `order.created`, no
        se corrige aquí — esta prueba deja constancia explícita de que sigue
        intacto, para que un cambio futuro lo encuentre documentado."""
        event = NotificationEvent(
            event_type="order.created",
            related_entity_type="customer_order",
            related_entity_id=uuid.uuid4(),
            payload={"table_number": 4, "items_count": 3, "total": "45000"},
        )
        self.assertEqual(summarize(event), "Mesa 4 · 3 ítems · $45000")


if __name__ == "__main__":
    unittest.main()
