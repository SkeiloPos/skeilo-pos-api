"""Tests de la nueva funcionalidad — spec 033-planes-suscripcion-tenant,
Historia de Usuario 1: el Super Admin define planes con límites, accesos y
precios (FR-001/FR-002/FR-007/FR-014/FR-016).

No son characterization tests ("CONGELA comportamiento actual"): los planes
de suscripción son comportamiento enteramente nuevo (Constitución, Principio
IV/X) — se verifican contra `spec.md`, no contra un comportamiento heredado.

Invoca las funciones de endpoint directamente como funciones Python (mismo
patrón que `test_super_admin_payment_catalog.py`): el guard
`Depends(get_current_super_admin)` vive en el router padre
(`super_admin/router.py`), no en el cuerpo de estas funciones, así que no es
observable llamándolas directamente — se verifica estructuralmente por estar
montadas bajo ese mismo router (mismo criterio que 032).

    python -m unittest app.characterization_tests.test_super_admin_plans -v
"""
import unittest

from fastapi import HTTPException

from app.characterization_tests import plan_fixtures as fx
from app.api.v1.super_admin import plans_router as router
from app.api.v1.super_admin.schemas import PlanCreate, PlanResponse, PlanUpdate
from app.core.plan_limits import enforce_plan_limit


class TestSuperAdminPlans(unittest.TestCase):
    # ---------------------------------------------------------- Acceptance Scenario 1

    def test_crear_plan_con_caracteristicas_queda_disponible(self):
        """FR-001/FR-002: un plan nuevo con nombre, descripción y algunas
        características queda registrado; el resto de límites en su default
        (0) y accesos en `false` sin haberlos enviado."""
        db = fx.new_session()
        created = router.create_plan(
            PlanCreate(name="Básico", description="Para negocios pequeños.", mesas_limit=5, inventario_access=False),
            db,
        )
        self.assertEqual(created.mesas_limit, 5)
        self.assertEqual(created.cajas_limit, 0)
        self.assertEqual(created.usuarios_limit, 0)
        self.assertFalse(created.inventario_access)
        self.assertFalse(hasattr(created, "compras_access"))  # spec 141: el módulo ya no existe

        listed = router.list_plans(db)
        self.assertEqual([p.name for p in listed], ["Básico"])

    def test_crear_plan_con_nombre_duplicado_es_409(self):
        db = fx.new_session()
        fx.make_plan(db, name="Pro")
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            router.create_plan(PlanCreate(name="Pro"), db)
        self.assertEqual(ctx.exception.status_code, 409)

    # ---------------------------------------------------------- Acceptance Scenario 2

    def test_editar_limite_de_plan_aplica_de_inmediato_a_tenants_existentes(self):
        """FR-014: subir mesas_limit de 5 a 8 se refleja en la siguiente
        validación de un tenant con ese plan, sin tocar el tenant."""
        db = fx.new_session()
        plan = fx.make_plan(db, mesas_limit=5)
        tenant = fx.make_tenant(db, plan=plan)
        for _ in range(5):
            fx.make_dining_table(db)
        db.commit()

        with self.assertRaises(HTTPException):
            enforce_plan_limit(db, tenant, "mesas")

        router.update_plan(plan.id, PlanUpdate(mesas_limit=8), db)

        enforce_plan_limit(db, tenant, "mesas")  # ya no bloquea, sin tocar el tenant

    # ---------------------------------------------------------- Acceptance Scenario 3

    def test_marcar_limite_como_ilimitado_nunca_vuelve_a_bloquear(self):
        """FR-007: `mesas_limit=null` explícito es distinto de omitirlo — un
        PATCH con `null` desbloquea, no lo deja en 0."""
        db = fx.new_session()
        plan = fx.make_plan(db, mesas_limit=5)
        tenant = fx.make_tenant(db, plan=plan)
        for _ in range(5):
            fx.make_dining_table(db)
        db.commit()

        router.update_plan(plan.id, PlanUpdate(mesas_limit=None), db)

        for _ in range(10):
            fx.make_dining_table(db)
            enforce_plan_limit(db, tenant, "mesas")  # nunca lanza

    # ---------------------------------------------------------- Acceptance Scenario 4

    def test_crear_plan_con_precios_independientes(self):
        """FR-016: un plan puede definir precio mensual, anual, ambos, o
        ninguno — independientes entre sí."""
        db = fx.new_session()
        created = router.create_plan(
            PlanCreate(name="Pro", precio_mensual="89000.00", precio_anual="890000.00"), db,
        )
        self.assertEqual(str(created.precio_mensual), "89000.00")
        self.assertEqual(str(created.precio_anual), "890000.00")

        solo_mensual = router.create_plan(PlanCreate(name="Starter", precio_mensual="19900.00"), db)
        self.assertIsNotNone(solo_mensual.precio_mensual)
        self.assertIsNone(solo_mensual.precio_anual)

    # ---------------------------------------------------------- spec 141: sin `compras_access`

    def test_la_respuesta_de_un_plan_no_contiene_compras_access(self):
        """FR-022, A-152: `compras_access` desaparece de crear/listar/editar."""
        db = fx.new_session()
        created = router.create_plan(PlanCreate(name="Con inventario", inventario_access=True), db)

        for plan in (created, *router.list_plans(db)):
            cuerpo = PlanResponse.model_validate(plan).model_dump()
            self.assertNotIn("compras_access", cuerpo)
            self.assertTrue(cuerpo["inventario_access"])
            self.assertIn("promociones_access", cuerpo)
        self.assertNotIn("compras_access", PlanResponse.model_fields)

    def test_un_payload_con_compras_access_se_acepta_e_ignora_sin_error_de_validacion(self):
        """Research H5: Pydantic ignora campos extra, así que un front viejo (o un
        script) que aún envíe `compras_access` no recibe 422 ni cambia el plan."""
        db = fx.new_session()
        entrada = PlanCreate.model_validate({"name": "Viejo", "inventario_access": False, "compras_access": True})
        self.assertFalse(hasattr(entrada, "compras_access"))

        created = router.create_plan(entrada, db)
        self.assertFalse(created.inventario_access)  # `compras_access` no se traduce en nada: se ignora

        cambio = PlanUpdate.model_validate({"compras_access": True})
        self.assertEqual(cambio.model_dump(exclude_unset=True), {})
        updated = router.update_plan(created.id, cambio, db)
        self.assertFalse(updated.inventario_access)

    # ---------------------------------------------------------- spec 149: cocina_access

    def test_crear_plan_con_cocina_access_true_queda_persistido(self):
        db = fx.new_session()
        created = router.create_plan(PlanCreate(name="Con cocina", cocina_access=True), db)
        self.assertTrue(created.cocina_access)

    def test_crear_plan_sin_enviar_cocina_access_queda_en_false(self):
        """FR-003: una característica omitida se guarda en su default — `false`
        para accesos de módulo, igual que `inventario_access`/`promociones_access`."""
        db = fx.new_session()
        created = router.create_plan(PlanCreate(name="Sin cocina"), db)
        self.assertFalse(created.cocina_access)

    def test_editar_cocina_access_de_true_a_false_se_refleja_en_el_listado(self):
        db = fx.new_session()
        plan = fx.make_plan(db, cocina_access=True)
        db.commit()

        router.update_plan(plan.id, PlanUpdate(cocina_access=False), db)

        listed = {p.id: p for p in router.list_plans(db)}
        self.assertFalse(listed[plan.id].cocina_access)

    def test_get_plan_de_un_tenant_refleja_cocina_access_vigente(self):
        db = fx.new_session()
        plan = fx.make_plan(db, cocina_access=True)
        tenant = fx.make_tenant(db, plan=plan)
        user = fx.make_user(db, tenant)
        db.commit()

        from app.api.v1.plan import router as plan_router

        summary = plan_router.get_plan_summary(db=db, tenant=tenant, _=user)
        self.assertTrue(summary["modules"]["cocina"])


if __name__ == "__main__":
    unittest.main()
