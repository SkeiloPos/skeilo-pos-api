"""Tests de la nueva funcionalidad — spec 033-planes-suscripcion-tenant,
Historia de Usuario 6: el Tenant Admin consulta su plan, su consumo y su
vencimiento (FR-013).

No son characterization tests — comportamiento enteramente nuevo. Ejercita
`GET /plan` directamente como función Python (mismo patrón que el resto de
este paquete).

    python -m unittest app.characterization_tests.test_plan_summary -v
"""
import unittest
from datetime import timedelta

from app.characterization_tests import plan_fixtures as fx
from app.api.v1.plan import router as plan_router
from app.core.timezone import utc_now


class TestPlanSummary(unittest.TestCase):
    # ---------------------------------------------------------- Acceptance Scenario 1

    def test_resumen_incluye_plan_consumo_y_modulos_en_una_sola_respuesta(self):
        db = fx.new_session()
        plan = fx.make_plan(db, name="Básico", mesas_limit=5, inventario_access=True)
        tenant = fx.make_tenant(db, plan=plan)
        for _ in range(4):
            fx.make_dining_table(db)
        user = fx.make_user(db, tenant)
        db.commit()

        summary = plan_router.get_plan_summary(db=db, tenant=tenant, _=user)

        self.assertEqual(summary["plan_name"], "Básico")
        self.assertEqual(summary["resources"]["mesas"], {"used": 4, "limit": 5})
        self.assertTrue(summary["modules"]["inventario"])
        self.assertFalse(summary["modules"]["promociones"])
        self.assertFalse(summary["modules"]["cocina"])
        # spec 149: `modules` trae tres claves — inventario, promociones y cocina.
        self.assertEqual(set(summary["modules"]), {"inventario", "promociones", "cocina"})

    # ---------------------------------------------------------- Acceptance Scenario 2

    def test_limite_ilimitado_se_expone_como_limit_null(self):
        db = fx.new_session()
        plan = fx.make_plan(db, productos_limit=None)
        tenant = fx.make_tenant(db, plan=plan)
        user = fx.make_user(db, tenant)
        db.commit()

        summary = plan_router.get_plan_summary(db=db, tenant=tenant, _=user)

        self.assertIsNone(summary["resources"]["productos"]["limit"])

    # ---------------------------------------------------------- Acceptance Scenario 3

    def test_vencimiento_presente_y_vencido_false_cuando_no_ha_pasado(self):
        db = fx.new_session()
        plan = fx.make_plan(db)
        tenant = fx.make_tenant(db, plan=plan, ciclo_facturacion="mensual")
        user = fx.make_user(db, tenant)
        db.commit()

        summary = plan_router.get_plan_summary(db=db, tenant=tenant, _=user)

        self.assertIsNotNone(summary["plan_vence_en"])
        self.assertFalse(summary["vencido"])

    # ---------------------------------------------------------- Acceptance Scenario 4

    def test_sin_vencimiento_devuelve_null(self):
        db = fx.new_session()
        plan = fx.make_plan(db)
        tenant = fx.make_tenant(db, plan=plan)  # ciclo_facturacion=None
        user = fx.make_user(db, tenant)
        db.commit()

        summary = plan_router.get_plan_summary(db=db, tenant=tenant, _=user)

        self.assertIsNone(summary["plan_vence_en"])
        self.assertIsNone(summary["ciclo_facturacion"])
        self.assertFalse(summary["vencido"])

    def test_vencido_true_cuando_ya_paso(self):
        db = fx.new_session()
        plan = fx.make_plan(db)
        tenant = fx.make_tenant(
            db, plan=plan, ciclo_facturacion="mensual",
            plan_iniciado_en=utc_now().replace(tzinfo=None) - timedelta(days=45),
        )
        user = fx.make_user(db, tenant)
        db.commit()

        summary = plan_router.get_plan_summary(db=db, tenant=tenant, _=user)

        self.assertTrue(summary["vencido"])

    # ---------------------------------------------------------- research.md Decisión 7

    def test_accesible_para_cualquier_rol_no_solo_admin(self):
        """El endpoint usa get_current_user, no require_tenant_admin — un
        CASHIER también debe poder consultarlo (necesario para el guard de
        navegación del frontend, que actúa para cualquier rol)."""
        db = fx.new_session()
        plan = fx.make_plan(db)
        tenant = fx.make_tenant(db, plan=plan)
        cashier = fx.make_user(db, tenant, role_name="CASHIER")
        db.commit()

        summary = plan_router.get_plan_summary(db=db, tenant=tenant, _=cashier)
        self.assertIsNotNone(summary)

    # ---------------------------------------------------------- spec 154: FR-004

    def test_resumen_no_expone_modalidad_pago_qr_en_ningun_nivel(self):
        """FR-004: ningún rol de administración del tenant puede consultar
        `modalidad_pago_qr` — no viaja en `GET /plan`, a diferencia de
        `inventario_access`/`promociones_access`/`cocina_access`."""
        import json

        db = fx.new_session()
        plan = fx.make_plan(db, modalidad_pago_qr="PAY_BEFORE_RECEIVING")
        tenant = fx.make_tenant(db, plan=plan)
        user = fx.make_user(db, tenant)
        db.commit()

        summary = plan_router.get_plan_summary(db=db, tenant=tenant, _=user)

        self.assertNotIn("modalidad_pago_qr", json.dumps(summary, default=str))


if __name__ == "__main__":
    unittest.main()
