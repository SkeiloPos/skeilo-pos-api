"""Tests de la nueva funcionalidad — spec 033-planes-suscripcion-tenant,
Historia de Usuario 4: el sistema deniega el acceso a módulos que el plan no
incluye (FR-008/FR-009). Reescrito por la spec 141 (A-150/A-151): el catálogo
de módulos pasa de tres a dos (`inventario` y `promociones`; Proveedores y
Compras viven dentro de `inventario`) y el rechazo lleva `code`/`module`.

No son characterization tests — comportamiento enteramente nuevo. Ejercita
`require_module_access(...)` directamente (mismo mecanismo que
`inventory/router.py`/`promotions/router.py` ya conectan a nivel de router)
para no depender de la resolución de `Depends()` de FastAPI.

    python -m unittest app.characterization_tests.test_plan_module_access -v
"""
import unittest

from fastapi import HTTPException

from app.characterization_tests import plan_fixtures as fx
from app.core.exceptions import ModuleNotInPlanError
from app.core.plan_limits import PLAN_MODULES, require_module_access


class TestPlanModuleAccess(unittest.TestCase):
    # ---------------------------------------------------------- Acceptance Scenario 1

    def test_modulo_no_incluido_deniega_con_mensaje_claro_y_codigo(self):
        """spec 141 (FR-006/FR-007/FR-008, US2.1): el rechazo es un 403 con
        `detail` de tipo `str`, `code` constante y el `module` requerido."""
        db = fx.new_session()
        plan = fx.make_plan(db, inventario_access=True, promociones_access=False)
        tenant = fx.make_tenant(db, plan=plan)
        user = fx.make_user(db, tenant)
        db.commit()

        # inventario sí incluido
        require_module_access("inventario")(tenant=tenant, db=db, _user=user)

        dependencia = require_module_access("promociones")
        with self.assertRaises(HTTPException) as ctx:
            dependencia(tenant=tenant, db=db, _user=user)
        self.assertIsInstance(ctx.exception, ModuleNotInPlanError)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIsInstance(ctx.exception.detail, str)
        self.assertEqual(ctx.exception.detail, "Tu plan actual no incluye el módulo de promociones.")
        self.assertEqual(ctx.exception.code, "MODULE_NOT_IN_PLAN")
        self.assertEqual(ctx.exception.module, "promociones")

    def test_el_rechazo_de_inventario_nombra_el_modulo_inventario(self):
        db = fx.new_session()
        plan = fx.make_plan(db, inventario_access=False, promociones_access=True)
        tenant = fx.make_tenant(db, plan=plan)
        user = fx.make_user(db, tenant)
        db.commit()

        dependencia = require_module_access("inventario")
        with self.assertRaises(ModuleNotInPlanError) as ctx:
            dependencia(tenant=tenant, db=db, _user=user)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "Tu plan actual no incluye el módulo de inventario.")
        self.assertEqual(ctx.exception.module, "inventario")

    # ---------------------------------------------------------- Acceptance Scenario 2

    def test_modulo_incluido_se_concede_sin_restriccion(self):
        db = fx.new_session()
        plan = fx.make_plan(db, promociones_access=True)
        tenant = fx.make_tenant(db, plan=plan)
        user = fx.make_user(db, tenant)
        db.commit()

        require_module_access("promociones")(tenant=tenant, db=db, _user=user)  # no lanza

    def test_modulo_no_configurado_queda_bloqueado_por_defecto(self):
        """FR-002: una característica de módulo no configurada al crear el
        plan se comporta como acceso denegado."""
        db = fx.new_session()
        plan = fx.make_plan(db)  # ningún módulo configurado
        tenant = fx.make_tenant(db, plan=plan)
        user = fx.make_user(db, tenant)
        db.commit()

        for module in ("inventario", "promociones"):
            dependencia = require_module_access(module)
            with self.assertRaises(HTTPException):
                dependencia(tenant=tenant, db=db, _user=user)

    # ---------------------------------------------------------- Catálogo (spec 141, FR-001)

    def test_el_catalogo_de_modulos_son_inventario_promociones_y_cocina(self):
        """Spec 149: el catálogo pasa de dos a tres entradas — se agrega
        `"cocina"` como característica de plan, sin enforcement de endpoint
        todavía (research.md Decisión 0)."""
        self.assertEqual(PLAN_MODULES, ("inventario", "promociones", "cocina"))

    def test_compras_ya_no_es_un_modulo_y_falla_al_construir_la_dependencia(self):
        """research D2: una clave fuera del catálogo es un error de programación
        y se detecta al **construir** la dependencia (al importar el router),
        no en la primera petición."""
        with self.assertRaises(ValueError):
            require_module_access("compras")

    def test_la_dependencia_expone_su_modulo(self):
        self.assertEqual(require_module_access("inventario").module_key, "inventario")
        self.assertEqual(require_module_access("promociones").module_key, "promociones")


if __name__ == "__main__":
    unittest.main()
