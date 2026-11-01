"""Tests de la extensión de logging operativo — spec 074-auditoria-ordenes,
FR-015 … FR-021 (User Story 4, SC-006 … SC-008).

No son characterization tests: `OperationalLogMiddleware` y el efecto colateral
de actor/tenant en `request.state` son comportamiento enteramente nuevo. Se
verifican contra `spec.md`/`data-model.md` § Extensión y
`contracts/operational-log-entry.md` del repositorio `pos-specs`.

Tres niveles, como fija `research.md` § 11:

  - **Humo de las 3 dependencias compartidas** (`get_tenant`,
    `get_current_user`, `get_session_context`): siguen devolviendo exactamente
    lo mismo que antes a quien las invoca. Es la salvaguarda concreta del
    Principio II: esas 3 funciones son la puerta de autenticación/resolución de
    tenant de casi todo el backend, y el side-effect que se les agregó no puede
    cambiar ni su valor de retorno ni cuándo fallan.
  - **Del middleware**, atravesando una app ASGI real vía `TestClient` (mismo
    patrón que `super_admin_http_fixtures.py`, spec 068): el filtrado por
    método/prefijo, el nivel de severidad según el status, la ausencia total
    del cuerpo, y que una falla del logging nunca degrada la respuesta real.
  - **De convivencia** (FR-020/FR-021): una de las 8 rutas ya auditadas emite
    las dos entidades, correlacionadas por el mismo `request_id`.

Todos requieren `ENVIRONMENT="prod"` parcheado: fuera de prod el emisor es un
no-op deliberado, igual que el de la auditoría de orden (research.md § 2).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_operational_log -v
"""
from __future__ import annotations

import unittest
import uuid
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request as StarletteRequest
from starlette.testclient import TestClient

from app.characterization_tests import auth_fixtures as af
from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import checkout
from app.api.v1.orders.schemas import CancelIn
from app.core import db as core_db
from app.core import qr_context
from app.core.config import settings
from app.core.db import get_tenant
from app.core.dependencies import get_current_user
from app.core.error_middleware import OperationalLogMiddleware, current_request_id
from app.core.models import Base
from app.core.order_audit import OrderAuditEventType
from app.core.qr_context import get_session_context
from app.core.utils import create_access_token, decode_token

PRECIO = Decimal("18000")

#: Marcador que viaja en el cuerpo de las peticiones de prueba: si apareciera en
#: cualquier atributo de la entrada, FR-018 estaría incumplido.
CUERPO_MARCADOR = "marcador-secreto-del-cuerpo-que-nunca-debe-viajar"

SUPER_ADMIN_PREFIX = "/api/v1/super-admin"


def _prod():
    """El gate de entorno (research.md § 2) hace del emisor un no-op fuera de
    prod; estos tests lo levantan, igual que los de la auditoría de orden."""
    return mock.patch.object(settings, "ENVIRONMENT", "prod")


def _fake_sentry():
    """Parchea el `sentry_sdk` que ve `error_middleware` — el punto de salida
    real, exactamente como `test_order_audit_log.py` parchea el suyo."""
    return mock.patch("app.core.error_middleware.sentry_sdk")


def _entradas(fake) -> list[tuple[str, str, dict]]:
    """`(nivel, template, attributes)` de cada llamada capturada a
    `sentry_sdk.logger.*`, en el orden en que se emitieron."""
    salida = []
    for nivel in ("info", "warning", "error"):
        for llamada in getattr(fake.logger, nivel).call_args_list:
            salida.append((nivel, llamada.args[0], llamada.kwargs["attributes"]))
    return salida


def _request_double(headers: dict | None = None) -> StarletteRequest:
    """`Request` real de Starlette (no un doble): lo que se está verificando es
    justamente que estampar en `request.state` no rompe nada, así que el objeto
    tiene que ser el de verdad."""
    crudas = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return StarletteRequest(
        {
            "type": "http",
            "method": "POST",
            "path": "/lo-que-sea",
            "headers": crudas,
            "query_string": b"",
            "state": {},
        }
    )


# ======================================================================
# T050 — las 3 dependencias compartidas no cambiaron para quien las usa
# ======================================================================


class DependenciasCompartidasSinCambioTests(unittest.TestCase):
    """T050 (Principio II, research.md § 11): `get_tenant`/`get_current_user`/
    `get_session_context` devuelven exactamente lo mismo que antes de T044-T046.

    El side-effect en `request.state` es *adicional*: ni sustituye ni altera el
    valor de retorno, y las 3 siguen siendo invocables sin `Request` (hay tests
    ya existentes que las llaman así, en proceso, sin FastAPI de por medio)."""

    # ------------------------------------------------------------ get_tenant

    def _con_tenant(self, tenant):
        """Parchea el `with_db` que usa `get_tenant` para devolver `tenant` sin
        necesitar el Postgres real del schema compartido."""

        @contextmanager
        def _fake_with_db(_schema):
            yield SimpleNamespace(
                query=lambda _modelo: SimpleNamespace(
                    filter=lambda *_a, **_k: SimpleNamespace(one_or_none=lambda: tenant)
                )
            )

        return mock.patch.object(core_db, "with_db", _fake_with_db)

    def test_get_tenant_devuelve_el_mismo_tenant_y_ademas_lo_estampa(self):
        tenant = SimpleNamespace(id=7, name="Bar de prueba", schema="t7")
        req = _request_double({"x-tenant-host": "bar.localhost:4200"})

        with self._con_tenant(tenant):
            resultado = get_tenant(req)

        # Lo que importa: el mismo objeto, no una copia ni un envoltorio.
        self.assertIs(resultado, tenant)
        # Y, además, el efecto colateral nuevo.
        self.assertEqual(req.state.tenant_id, 7)

    def test_get_tenant_sigue_fallando_igual_sin_cabecera(self):
        """El side-effect no adelanta ni retrasa ninguna de sus dos fallas."""
        req = _request_double({})

        with self.assertRaises(HTTPException) as ctx:
            get_tenant(req)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIsNone(getattr(req.state, "tenant_id", None))

    def test_get_tenant_sigue_devolviendo_404_si_el_host_no_existe(self):
        req = _request_double({"x-tenant-host": "inexistente.localhost"})

        with self._con_tenant(None):
            with self.assertRaises(HTTPException) as ctx:
                get_tenant(req)

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIsNone(getattr(req.state, "tenant_id", None))

    # ------------------------------------------------------ get_current_user

    def _claims(self, user, tenant) -> dict:
        token = create_access_token(
            {
                "email": user.email,
                "uid": str(user.id),
                "tenant_id": tenant.id,
                "is_super_admin": False,
                "role": user.role_name,
                "must_change_password": user.must_change_password,
            }
        )
        return decode_token(token)

    def test_get_current_user_sin_request_devuelve_el_mismo_user_de_siempre(self):
        """Contrato que ya usan tests existentes (`test_auth_session_revocation`):
        invocarla directamente, sin `Request`, sigue funcionando igual."""
        db = af.new_session()
        tenant = af.make_tenant(db)
        user = af.make_user(db, tenant=tenant, password="claveOriginal1")
        db.commit()

        resultado = get_current_user(
            token_data=self._claims(user, tenant), db=db, tenant=tenant
        )

        self.assertIs(resultado, user)

    def test_get_current_user_con_request_devuelve_lo_mismo_y_estampa_el_actor(self):
        db = af.new_session()
        tenant = af.make_tenant(db)
        user = af.make_user(db, tenant=tenant, password="claveOriginal1")
        db.commit()
        req = _request_double()

        resultado = get_current_user(
            token_data=self._claims(user, tenant), db=db, tenant=tenant, req=req
        )

        self.assertIs(resultado, user)
        self.assertEqual(req.state.actor_id, str(user.id))
        self.assertEqual(req.state.actor_type, "staff")

    def test_get_current_user_sigue_rechazando_una_cuenta_inactiva(self):
        """El estampado nunca ocurre antes de la validación: una petición que
        falla la autenticación no deja actor en `request.state`."""
        db = af.new_session()
        tenant = af.make_tenant(db)
        user = af.make_user(db, tenant=tenant, password="claveOriginal1")
        claims = self._claims(user, tenant)
        user.active = False
        db.commit()
        req = _request_double()

        with self.assertRaises(HTTPException) as ctx:
            get_current_user(token_data=claims, db=db, tenant=tenant, req=req)

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIsNone(getattr(req.state, "actor_id", None))

    # --------------------------------------------------- get_session_context

    def _con_session_context(self, ctx):
        @contextmanager
        def _fake_open(_token):
            yield ctx

        return mock.patch.object(qr_context, "open_session_context", _fake_open)

    def test_get_session_context_sin_request_entrega_el_mismo_contexto(self):
        ctx = SimpleNamespace(participant=SimpleNamespace(id=uuid.uuid4()))

        with self._con_session_context(ctx):
            entregados = list(get_session_context(x_session_token="tok"))

        self.assertEqual(len(entregados), 1)
        self.assertIs(entregados[0], ctx)

    def test_get_session_context_con_request_entrega_lo_mismo_y_estampa(self):
        ctx = SimpleNamespace(participant=SimpleNamespace(id=uuid.uuid4()))
        req = _request_double()

        with self._con_session_context(ctx):
            entregados = list(get_session_context(x_session_token="tok", req=req))

        self.assertIs(entregados[0], ctx)
        self.assertEqual(req.state.actor_id, str(ctx.participant.id))
        self.assertEqual(req.state.actor_type, "comensal")


# ======================================================================
# T051-T054 — el middleware, atravesando una app ASGI real
# ======================================================================


def _build_app() -> FastAPI:
    """App mínima con el middleware nuevo montado como en `app.main`.

    No se usa `app.main.create_app()` a propósito, por el mismo motivo que
    `super_admin_http_fixtures.py`: exige Postgres/Redis reales desde su primera
    línea. Lo que se verifica aquí es el comportamiento del middleware, que no
    depende de ningún router de negocio en particular.
    """
    app = FastAPI()
    app.add_middleware(OperationalLogMiddleware)

    def _estampa_actor(request: Request):
        """Simula lo que hacen `get_current_user`/`get_tenant` como efecto
        colateral (T044-T046), para verificar que el middleware lee de ahí."""
        request.state.actor_id = "actor-de-prueba"
        request.state.actor_type = "staff"
        request.state.tenant_id = 7

    @app.get("/api/v1/payment-methods")
    def listar():
        return {"items": []}

    @app.patch("/api/v1/payment-methods/{method_id}")
    def editar(method_id: uuid.UUID, body: dict):
        return {"ok": True, "id": str(method_id)}

    @app.post("/api/v1/categories", dependencies=[Depends(_estampa_actor)])
    def crear_categoria(body: dict):
        return {"ok": True}

    @app.post("/api/v1/control/error-de-negocio")
    def error_de_negocio(body: dict):
        raise HTTPException(status_code=422, detail="dato inválido")

    @app.post("/api/v1/control/falla-tecnica")
    def falla_tecnica(body: dict):
        raise RuntimeError("boom")

    @app.post(f"{SUPER_ADMIN_PREFIX}/tenants")
    def super_admin_crear(body: dict):
        return {"ok": True}

    return app


class OperationalLogMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.app = _build_app()
        # `raise_server_exceptions=False`: hace falta para el caso 5xx, donde la
        # excepción no manejada debe terminar como respuesta 500 igual que en
        # producción (ServerErrorMiddleware), no reventar el test.
        self.client = TestClient(self.app, raise_server_exceptions=False)

    # ------------------------------------------------------------ T052 (US4)

    def test_peticion_mutativa_fuera_de_ordenes_emite_una_entrada_completa(self):
        """T052/FR-015-FR-019: `PATCH` a una ruta mutativa cualquiera → una
        entrada con método/ruta/status/duración/`request_id`, y `route` es el
        patrón registrado, no la URL con el id real (research.md § 10)."""
        method_id = uuid.uuid4()

        with _prod(), _fake_sentry() as fake:
            resp = self.client.patch(
                f"/api/v1/payment-methods/{method_id}",
                json={"name": CUERPO_MARCADOR},
            )

        self.assertEqual(resp.status_code, 200)
        entradas = _entradas(fake)
        self.assertEqual(len(entradas), 1)
        nivel, template, attrs = entradas[0]

        self.assertEqual(nivel, "info")
        self.assertEqual(template, "PATCH /api/v1/payment-methods/{method_id}")
        self.assertEqual(attrs["method"], "PATCH")
        # El patrón, nunca el UUID real: si fuera la URL resuelta, cada petición
        # tendría un valor distinto y no se podría agregar en Sentry.
        self.assertEqual(attrs["route"], "/api/v1/payment-methods/{method_id}")
        self.assertNotIn(str(method_id), attrs["route"])
        self.assertEqual(attrs["status"], 200)
        self.assertIsInstance(attrs["duration_ms"], float)
        self.assertGreaterEqual(attrs["duration_ms"], 0.0)
        # `request_id` presente en el 100% de las entradas, y es un UUID.
        uuid.UUID(attrs["request_id"])

    def test_los_atributos_son_exactamente_los_del_contrato(self):
        """`contracts/operational-log-entry.md`: la lista de campos es cerrada.
        Ningún atributo extra se cuela sin actualizar el contrato."""
        with _prod(), _fake_sentry() as fake:
            self.client.patch(
                f"/api/v1/payment-methods/{uuid.uuid4()}", json={"name": "x"}
            )

        _, _, attrs = _entradas(fake)[0]
        self.assertEqual(
            set(attrs), {"method", "route", "status", "duration_ms", "request_id"}
        )

    def test_actor_y_tenant_viajan_cuando_se_resolvieron_y_se_omiten_si_no(self):
        """`data-model.md` § Extensión: ausentes, nunca `None`, cuando la
        petición no pasó por ninguna de las 3 dependencias que los resuelven."""
        with _prod(), _fake_sentry() as fake:
            self.client.post("/api/v1/categories", json={"name": "Bebidas"})
        _, _, con_actor = _entradas(fake)[0]

        self.assertEqual(con_actor["actor_id"], "actor-de-prueba")
        self.assertEqual(con_actor["actor_type"], "staff")
        self.assertEqual(con_actor["tenant_id"], 7)

        with _prod(), _fake_sentry() as fake:
            self.client.patch(
                f"/api/v1/payment-methods/{uuid.uuid4()}", json={"name": "x"}
            )
        _, _, sin_actor = _entradas(fake)[0]

        # Omitidos del diccionario, no enviados como `None`.
        for clave in ("actor_id", "actor_type", "tenant_id"):
            self.assertNotIn(clave, sin_actor)

    # ------------------------------------------------------------ T051 nivel

    def test_el_nivel_de_severidad_corresponde_al_status(self):
        """T051/research.md § 9: `<400` info, `400-499` warning, `>=500` error."""
        with _prod(), _fake_sentry() as fake:
            self.client.patch(
                f"/api/v1/payment-methods/{uuid.uuid4()}", json={"name": "x"}
            )
            self.client.post("/api/v1/control/error-de-negocio", json={"a": 1})
            self.client.post("/api/v1/control/falla-tecnica", json={"a": 1})

        por_nivel = {nivel: attrs for nivel, _t, attrs in _entradas(fake)}
        self.assertEqual(set(por_nivel), {"info", "warning", "error"})
        self.assertEqual(por_nivel["info"]["status"], 200)
        self.assertEqual(por_nivel["warning"]["status"], 422)
        self.assertEqual(por_nivel["error"]["status"], 500)

    def test_una_falla_tecnica_deja_su_entrada_y_sigue_siendo_un_500(self):
        """La excepción no manejada no se convierte en respuesta aquí: sigue
        llegando a `ServerErrorMiddleware` exactamente como hoy."""
        with _prod(), _fake_sentry() as fake:
            resp = self.client.post("/api/v1/control/falla-tecnica", json={"a": 1})

        self.assertEqual(resp.status_code, 500)
        nivel, template, attrs = _entradas(fake)[0]
        self.assertEqual(nivel, "error")
        self.assertEqual(template, "POST /api/v1/control/falla-tecnica")
        self.assertEqual(attrs["status"], 500)

    # ------------------------------------------------------- T051 sin cuerpo

    def test_ningun_atributo_contiene_el_cuerpo_de_la_peticion_ni_de_la_respuesta(self):
        """T051/FR-018: sin excepción y sin lista de campos sensibles — la
        entidad simplemente no tiene dónde alojar un cuerpo."""
        cuerpo = {
            "password": CUERPO_MARCADOR,
            "nombre": CUERPO_MARCADOR,
            "anidado": {"comprobante": CUERPO_MARCADOR},
        }

        with _prod(), _fake_sentry() as fake:
            resp = self.client.patch(
                f"/api/v1/payment-methods/{uuid.uuid4()}", json=cuerpo
            )

        self.assertEqual(resp.status_code, 200)
        _, template, attrs = _entradas(fake)[0]
        self.assertNotIn(CUERPO_MARCADOR, repr(attrs))
        self.assertNotIn(CUERPO_MARCADOR, template)
        for prohibida in ("body", "payload", "request_body", "response_body", "json"):
            self.assertNotIn(prohibida, attrs)

    # ------------------------------------------------- T051 no bloqueante

    def test_una_excepcion_del_logging_no_se_propaga_ni_altera_la_respuesta(self):
        """T051/regla 13 de `data-model.md`: la petición real ya terminó; un
        fallo al construir o enviar la entrada no puede degradarla."""
        method_id = uuid.uuid4()

        with _prod(), _fake_sentry() as fake:
            fake.logger.info.side_effect = RuntimeError("Sentry caído")
            resp = self.client.patch(
                f"/api/v1/payment-methods/{method_id}", json={"name": "x"}
            )

        # La respuesta real, intacta: mismo status y mismo cuerpo.
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "id": str(method_id)})
        # Y se intentó emitir (la falla ocurrió dentro, no antes).
        self.assertEqual(fake.logger.info.call_count, 1)

    def test_fuera_de_prod_no_se_emite_nada(self):
        """Mismo gate de entorno que el resto del código que toca sentry_sdk."""
        with mock.patch.object(settings, "ENVIRONMENT", "dev"), _fake_sentry() as fake:
            resp = self.client.patch(
                f"/api/v1/payment-methods/{uuid.uuid4()}", json={"name": "x"}
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_entradas(fake), [])

    # -------------------------------------------------------------- T053 GET

    def test_una_lectura_no_produce_ninguna_entrada(self):
        """T053/FR-016: `GET`/`HEAD`/`OPTIONS` — incluido el polling de tiempo
        real — nunca generan esta entidad."""
        with _prod(), _fake_sentry() as fake:
            resp = self.client.get("/api/v1/payment-methods")
            self.client.head("/api/v1/payment-methods")
            self.client.options("/api/v1/payment-methods")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_entradas(fake), [])

    # ------------------------------------------------------- T054 super-admin

    def test_una_mutacion_en_super_admin_no_produce_ninguna_entrada(self):
        """T054/FR-015: ese prefijo conserva su propio mecanismo
        (`RequestIdMiddleware`/`register_error_handlers`), sin cambios — este
        middleware ni siquiera estampa un `request_id` ahí."""
        with _prod(), _fake_sentry() as fake:
            resp = self.client.post(
                f"{SUPER_ADMIN_PREFIX}/tenants", json={"name": CUERPO_MARCADOR}
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_entradas(fake), [])


# ======================================================================
# T055 — convivencia con la auditoría de orden (FR-020/FR-021)
# ======================================================================


def _sesion_multihilo():
    """Como `orders_fixtures.new_session()`, pero con `check_same_thread=False`.

    `TestClient` corre la app ASGI en un hilo distinto al del test (mismo motivo
    documentado en `super_admin_http_fixtures.new_session`), y SQLite rechaza
    por defecto usar una conexión desde otro hilo. No hace falta relajar esto en
    `orders_fixtures.new_session()`: ningún test existente pasa por `TestClient`.
    """
    fx._patch_sqlite_incompatible_server_defaults()
    fx._remove_partial_unique_indexes()
    tablas = [t for t in Base.metadata.tables.values() if t.name in fx._TABLE_NAMES]
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    conn = engine.connect().execution_options(schema_translate_map={"tenant": None})
    Base.metadata.create_all(bind=conn, tables=tablas)
    conn.commit()
    return Session(bind=conn)


class AuditoriaYLogOperativoConvivenTests(unittest.TestCase):
    """T055/FR-020/FR-021: una de las 8 rutas ya auditadas produce **ambas**
    entidades — el evento `order.cancelled` y la entrada operativa — con el
    mismo `request_id`. Ninguna reemplaza a la otra."""

    def setUp(self):
        self.db = _sesion_multihilo()
        table = fx.make_dining_table(self.db)
        ts = fx.make_table_session(self.db, table=table)
        participant = fx.make_participant(self.db, table_session=ts)
        category = fx.make_category(self.db)
        product = fx.make_product(self.db, category=category)
        variant = fx.make_variant(self.db, product=product, price=PRECIO)
        insumo = fx.make_inventory_item(self.db, current_stock=Decimal("1000"))
        fx.make_recipe_item(self.db, variant, insumo, quantity=Decimal("1"))
        self.order = fx.make_customer_order(
            self.db, ts, participant=participant, status="pendiente"
        )
        fx.make_order_item(self.db, self.order, variant, estado_cocina="pendiente")
        self.db.commit()

        self.user = SimpleNamespace(
            id=uuid.uuid4(), name="Cajero", role_name="CASHIER", tenant_id=7
        )
        self.client = TestClient(self._build_app())

    def _build_app(self) -> FastAPI:
        """Reproduce la ruta real `POST /orders/{order_id}/cancel` tal como la
        monta `app/api/v1/orders/router.py`: mismo patrón de ruta, misma función
        de servicio, mismo `request_id` sacado del `Request`."""
        app = FastAPI()
        app.add_middleware(OperationalLogMiddleware)
        db, user = self.db, self.user

        @app.post("/api/v1/orders/{order_id}/cancel")
        def cancel_order(order_id: uuid.UUID, body: dict, request: Request):
            checkout.cancel_order(
                db,
                order_id,
                CancelIn(motivo=body["motivo"]),
                user,
                tenant_id=7,
                request_id=current_request_id(request),
            )
            return {"ok": True}

        return app

    def test_la_misma_peticion_produce_las_dos_entidades_con_el_mismo_request_id(self):
        with _prod(), _fake_sentry() as fake_sentry, mock.patch.object(
            checkout, "record_order_audit_event"
        ) as rec:
            resp = self.client.post(
                f"/api/v1/orders/{self.order.id}/cancel",
                json={"motivo": "el cliente se arrepintió"},
            )

        self.assertEqual(resp.status_code, 200)

        # (1) El evento de auditoría de orden, con su `request_id` (FR-021).
        eventos = [c.kwargs for c in rec.call_args_list]
        cancelados = [
            e for e in eventos
            if e["event_type"] is OrderAuditEventType.ORDER_CANCELLED
        ]
        self.assertEqual(len(cancelados), 1)
        self.assertEqual(cancelados[0]["order_id"], self.order.id)
        request_id_auditoria = cancelados[0]["request_id"]
        self.assertIsNotNone(request_id_auditoria)

        # (2) La entrada operativa, que no reemplaza a la anterior (FR-020).
        entradas = _entradas(fake_sentry)
        self.assertEqual(len(entradas), 1)
        nivel, template, attrs = entradas[0]
        self.assertEqual(nivel, "info")
        self.assertEqual(template, "POST /api/v1/orders/{order_id}/cancel")
        self.assertEqual(attrs["route"], "/api/v1/orders/{order_id}/cancel")

        # (3) Y las dos correlacionan por el mismo `request_id`.
        self.assertEqual(attrs["request_id"], request_id_auditoria)

    def test_la_entrada_operativa_no_lleva_el_motivo_de_la_cancelacion(self):
        """El motivo es parte del cuerpo: viaja en el evento de auditoría de
        negocio (que sí lo cura), nunca en la entrada operativa (FR-018)."""
        with _prod(), _fake_sentry() as fake_sentry, mock.patch.object(
            checkout, "record_order_audit_event"
        ):
            self.client.post(
                f"/api/v1/orders/{self.order.id}/cancel",
                json={"motivo": CUERPO_MARCADOR},
            )

        _, template, attrs = _entradas(fake_sentry)[0]
        self.assertNotIn(CUERPO_MARCADOR, repr(attrs))
        self.assertNotIn(CUERPO_MARCADOR, template)


if __name__ == "__main__":
    unittest.main()
