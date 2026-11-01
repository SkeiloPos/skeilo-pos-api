"""Tests de la nueva funcionalidad — spec 141-modulo-inventario-unificado: el
servidor es la única autoridad de acceso por módulo y lo rechaza con un 403
`MODULE_NOT_IN_PLAN` (FR-005…FR-011, SC-001…SC-003; contrato
`modulo-no-incluido-api.md`).

No son characterization tests — comportamiento enteramente nuevo (A-151). Dos
bloques:

- `ModuleNotInPlanErrorTests`: la excepción y las funciones de `plan_limits`
  llamadas directamente, sin `TestClient` (mismo estilo que
  `test_plan_module_access.py`).
- `ModuleNotInPlanHttpTests`: la aplicación real (`app.main.app`, con el handler
  registrado en `create_app()` y el CORS real) vía `TestClient`, con `get_db`,
  `get_tenant` y `get_current_user` sustituidas — mismo patrón que
  `test_inventory_export.py`.

    python -m unittest app.characterization_tests.test_module_not_in_plan_error -v
"""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.characterization_tests import fixtures  # noqa: F401  - variables de entorno inertes antes de importar app.main
from app.characterization_tests import plan_fixtures as fx
from app.core.db import get_db, get_tenant
from app.core.dependencies import get_current_user, require_tenant_admin
from app.core.exceptions import ModuleNotInPlanError
from app.core.models import Base
from app.core.plan_limits import ensure_module_access, has_module_access, require_module_access
from app.models.inventory_item import InventoryItem
from app.models.purchase import Purchase
from app.models.supplier import Supplier


def _tenant_con(db: Session, **plan_kw):
    plan = fx.make_plan(db, **plan_kw)
    tenant = fx.make_tenant(db, plan=plan)
    return plan, tenant


# ======================================================================
# La excepción y las funciones de plan_limits, sin HTTP
# ======================================================================


class ModuleNotInPlanErrorTests(unittest.TestCase):
    # ---------------------------------------------------------- (a) la excepción

    def test_ensure_module_access_sin_modulo_lanza_la_excepcion_con_code_y_module(self):
        db = fx.new_session()
        _, tenant = _tenant_con(db, inventario_access=False)
        db.commit()

        with self.assertRaises(ModuleNotInPlanError) as ctx:
            ensure_module_access(db, tenant, "inventario")

        exc = ctx.exception
        self.assertIsInstance(exc, HTTPException)  # los `except HTTPException` existentes la siguen atrapando
        self.assertEqual(exc.status_code, 403)
        self.assertEqual(exc.module, "inventario")
        self.assertEqual(exc.code, "MODULE_NOT_IN_PLAN")
        self.assertIsInstance(exc.detail, str)  # FR-007
        self.assertEqual(exc.detail, "Tu plan actual no incluye el módulo de inventario.")

    def test_ensure_module_access_con_modulo_no_lanza(self):
        db = fx.new_session()
        _, tenant = _tenant_con(db, inventario_access=True)
        db.commit()

        ensure_module_access(db, tenant, "inventario")  # no lanza

    def test_el_mismo_error_sirve_a_todos_los_modulos(self):
        """FR-008: promociones usa la misma forma que inventario."""
        db = fx.new_session()
        _, tenant = _tenant_con(db, inventario_access=True, promociones_access=False)
        db.commit()

        with self.assertRaises(ModuleNotInPlanError) as ctx:
            ensure_module_access(db, tenant, "promociones")
        self.assertEqual(ctx.exception.module, "promociones")
        self.assertEqual(ctx.exception.code, "MODULE_NOT_IN_PLAN")
        self.assertEqual(ctx.exception.detail, "Tu plan actual no incluye el módulo de promociones.")

    # ---------------------------------------------------------- (b) precedencia

    def test_plan_vencido_gana_sobre_modulo_ausente_y_no_lleva_code(self):
        """FR-009, US2.5: un plan vencido se rechaza antes de mirar el módulo, con
        el mismo 403 de siempre y **sin** `code`."""
        db = fx.new_session()
        plan = fx.make_plan(db, inventario_access=False)
        tenant = fx.make_tenant(
            db, plan=plan, ciclo_facturacion="mensual", plan_iniciado_en=datetime(2020, 1, 1)
        )
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            ensure_module_access(db, tenant, "inventario")

        self.assertNotIsInstance(ctx.exception, ModuleNotInPlanError)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "Tu plan venció. Debe renovarse para seguir usando el sistema.")
        self.assertFalse(hasattr(ctx.exception, "code"))

    # ---------------------------------------------------------- (c) has_module_access

    def test_has_module_access_no_lanza_y_devuelve_booleano(self):
        db = fx.new_session()
        _, con_modulo = _tenant_con(db, inventario_access=True)
        _, sin_modulo = _tenant_con(db, inventario_access=False)
        db.commit()

        self.assertIs(has_module_access(db, con_modulo, "inventario"), True)
        self.assertIs(has_module_access(db, sin_modulo, "inventario"), False)

    def test_has_module_access_rechaza_claves_fuera_del_catalogo(self):
        db = fx.new_session()
        _, tenant = _tenant_con(db, inventario_access=True)
        db.commit()

        with self.assertRaises(ValueError):
            has_module_access(db, tenant, "compras")

    def test_ensure_module_access_rechaza_claves_fuera_del_catalogo(self):
        db = fx.new_session()
        _, tenant = _tenant_con(db, inventario_access=True)
        db.commit()

        with self.assertRaises(ValueError):
            ensure_module_access(db, tenant, "compras")

    # ---------------------------------------------------------- (d) cambio de plan

    def test_un_cambio_de_plan_surte_efecto_en_la_siguiente_llamada(self):
        """FR-005, US5.5, SC-003: no hay caché de plan; el servidor lee el plan
        vigente en cada petición."""
        db = fx.new_session()
        plan_con_modulo = fx.make_plan(db, inventario_access=True)
        plan_sin_modulo = fx.make_plan(db, inventario_access=False)
        tenant = fx.make_tenant(db, plan=plan_con_modulo)
        user = fx.make_user(db, tenant)
        db.commit()
        dependencia = require_module_access("inventario")

        dependencia(tenant=tenant, db=db, _user=user)  # con módulo: pasa

        tenant.plan_id = plan_sin_modulo.id
        db.commit()
        with self.assertRaises(ModuleNotInPlanError):
            dependencia(tenant=tenant, db=db, _user=user)  # la siguiente: ya no

        tenant.plan_id = plan_con_modulo.id
        db.commit()
        dependencia(tenant=tenant, db=db, _user=user)  # y al reasignarlo vuelve a pasar

    # ---------------------------------------------------------- (e) el rol es otro paso

    def test_el_rol_sigue_siendo_un_paso_aparte_y_no_lleva_code(self):
        """US2.6, FR-010: con el módulo incluido, un usuario sin el rol exigido
        se rechaza con su propio 403, sin `code` de módulo."""
        db = fx.new_session()
        _, tenant = _tenant_con(db, inventario_access=True)
        cajero = fx.make_user(db, tenant, role_name="CASHIER")
        db.commit()

        require_module_access("inventario")(tenant=tenant, db=db, _user=cajero)  # el módulo no lo frena

        with self.assertRaises(HTTPException) as ctx:
            require_tenant_admin(user=cajero)
        self.assertNotIsInstance(ctx.exception, ModuleNotInPlanError)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertFalse(hasattr(ctx.exception, "code"))


# ======================================================================
# El handler real, la aplicación real
# ======================================================================

_TABLE_NAMES = [
    "tenants", "plans", "roles", "users",
    "inventory_items", "inventory_movements", "unit_measures", "unit_measure_groups",
    "suppliers", "supplier_items", "purchases", "purchase_items", "inventory_import_jobs",
    "promotions",
]


def _new_http_session() -> Session:
    """`check_same_thread=False`: `TestClient` ejecuta el handler en otro hilo
    (mismo ajuste que `test_inventory_export.py`)."""
    tables = [t for t in Base.metadata.tables.values() if t.name in _TABLE_NAMES]
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    conn = engine.connect().execution_options(schema_translate_map={"tenant": None, "shared": None})
    Base.metadata.create_all(bind=conn, tables=tables)
    conn.commit()
    return Session(bind=conn)


def _aplicacion_real():
    # `app.main` ejecuta `create_app()` al importarse y esta llama a `initialize_database()`, que abre
    # PostgreSQL y migra. Se sustituye por una función vacía ANTES de importar (mismo motivo que
    # `test_contrato_instantes_api.py`).
    with mock.patch("app.core.db.initialize_database", lambda *a, **k: None):
        from app.main import app
    return app


class ModuleNotInPlanHttpTests(unittest.TestCase):
    ORIGEN_PERMITIDO = "https://heladeria.skeilopos.com"

    def setUp(self):
        self.app = _aplicacion_real()
        self.db = _new_http_session()
        self.addCleanup(self.app.dependency_overrides.clear)

    def _cliente(self, tenant, user) -> TestClient:
        self.app.dependency_overrides[get_db] = lambda: self.db
        self.app.dependency_overrides[get_tenant] = lambda: tenant
        self.app.dependency_overrides[get_current_user] = lambda: user
        return TestClient(self.app, raise_server_exceptions=False)

    def _conteos(self) -> tuple[int, int, int]:
        return tuple(
            self.db.execute(select(func.count()).select_from(modelo)).scalar_one()
            for modelo in (InventoryItem, Supplier, Purchase)
        )

    def _sin_modulo(self, **plan_kw):
        _, tenant = _tenant_con(self.db, **plan_kw)
        admin = fx.make_user(self.db, tenant, role_name="ADMIN")
        self.db.commit()
        return tenant, admin

    # ---------------------------------------------------------- cuerpo exacto

    def test_inventario_sin_modulo_responde_403_con_detail_code_y_module(self):
        tenant, admin = self._sin_modulo(inventario_access=False, promociones_access=True)

        resp = self._cliente(tenant, admin).get("/api/v1/inventory/suppliers")

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(
            resp.json(),
            {
                "detail": "Tu plan actual no incluye el módulo de inventario.",
                "code": "MODULE_NOT_IN_PLAN",
                "module": "inventario",
            },
        )

    def test_promociones_sin_modulo_responde_el_mismo_cuerpo_con_su_modulo(self):
        """FR-008: el mecanismo es único para todos los módulos."""
        tenant, admin = self._sin_modulo(inventario_access=True, promociones_access=False)

        resp = self._cliente(tenant, admin).get("/api/v1/promotions")

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(
            resp.json(),
            {
                "detail": "Tu plan actual no incluye el módulo de promociones.",
                "code": "MODULE_NOT_IN_PLAN",
                "module": "promociones",
            },
        )

    # ---------------------------------------------------------- 403 antes que 422, sin tocar datos

    def test_un_post_con_cuerpo_invalido_recibe_403_y_no_422(self):
        """FR-006: la dependencia es de router y corre antes de resolver el cuerpo."""
        tenant, admin = self._sin_modulo(inventario_access=False)
        antes = self._conteos()

        resp = self._cliente(tenant, admin).post("/api/v1/inventory/items", json={"cuerpo": "invalido"})

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "MODULE_NOT_IN_PLAN")
        self.assertEqual(self._conteos(), antes)

    def test_un_rechazo_no_lee_ni_escribe_datos_del_negocio(self):
        """US2.2: con insumos, proveedores y compras ya guardados, el rechazo no los toca."""
        tenant, admin = self._sin_modulo(inventario_access=False)
        self.db.add_all([Supplier(name="Proveedor de prueba"), Supplier(name="Otro proveedor")])
        self.db.commit()
        antes = self._conteos()
        self.assertEqual(antes, (0, 2, 0))
        cliente = self._cliente(tenant, admin)

        for ruta in ("/api/v1/inventory/suppliers", "/api/v1/inventory/purchases", "/api/v1/inventory/items"):
            self.assertEqual(cliente.get(ruta).status_code, 403, ruta)
        self.assertEqual(self._conteos(), antes)

    # ---------------------------------------------------------- precedencias

    def test_plan_vencido_responde_403_sin_code(self):
        """FR-009: el plan vencido conserva su propio rechazo."""
        plan = fx.make_plan(self.db, inventario_access=False)
        tenant = fx.make_tenant(
            self.db, plan=plan, ciclo_facturacion="mensual", plan_iniciado_en=datetime(2020, 1, 1)
        )
        admin = fx.make_user(self.db, tenant, role_name="ADMIN")
        self.db.commit()

        resp = self._cliente(tenant, admin).get("/api/v1/inventory/suppliers")

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json(), {"detail": "Tu plan venció. Debe renovarse para seguir usando el sistema."})

    def test_el_modulo_se_comprueba_antes_que_el_rol(self):
        """Contrato § 2: orden 3 (módulo) antes que orden 4 (rol)."""
        _, tenant = _tenant_con(self.db, inventario_access=False)
        cajero = fx.make_user(self.db, tenant, role_name="CASHIER")
        self.db.commit()

        resp = self._cliente(tenant, cajero).get("/api/v1/inventory/items/export")

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "MODULE_NOT_IN_PLAN")

    def test_con_modulo_el_rechazo_por_rol_no_lleva_code(self):
        """FR-010, US2.6."""
        _, tenant = _tenant_con(self.db, inventario_access=True)
        cajero = fx.make_user(self.db, tenant, role_name="CASHIER")
        self.db.commit()

        resp = self._cliente(tenant, cajero).get("/api/v1/inventory/items/export")

        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("code", resp.json())

    # ---------------------------------------------------------- CORS

    def test_el_rechazo_lleva_las_cabeceras_cors_del_origen_permitido(self):
        """El front lee el cuerpo del 403 desde otro origen: sin CORS no podría leer el `code`."""
        tenant, admin = self._sin_modulo(inventario_access=False)

        resp = self._cliente(tenant, admin).get(
            "/api/v1/inventory/suppliers", headers={"Origin": self.ORIGEN_PERMITIDO}
        )

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.headers.get("access-control-allow-origin"), self.ORIGEN_PERMITIDO)
        self.assertEqual(resp.json()["code"], "MODULE_NOT_IN_PLAN")

    # ---------------------------------------------------------- camino positivo (Q3, US2.3, SC-002)

    def test_compras_y_proveedores_no_exigen_un_indicador_propio(self):
        """Un plan con `inventario_access=True` y sin ningún indicador de Compras
        (ni siquiera existe ya): proveedores y compras responden 200."""
        tenant, admin = self._sin_modulo(inventario_access=True, promociones_access=False)
        cliente = self._cliente(tenant, admin)

        self.assertEqual(cliente.get("/api/v1/inventory/suppliers").status_code, 200)
        compras = cliente.get("/api/v1/inventory/purchases")
        self.assertEqual(compras.status_code, 200)
        self.assertEqual(compras.json()["total"], 0)

    # ---------------------------------------------------------- el handler está registrado en la app real

    def test_la_aplicacion_real_registra_el_handler_del_modulo(self):
        self.assertIn(ModuleNotInPlanError, self.app.exception_handlers)


if __name__ == "__main__":
    unittest.main()
