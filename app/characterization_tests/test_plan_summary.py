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


# ======================================================================
# spec 161-suscripcion-pago-wompi, Historia de Usuario 1 (FR-002, FR-003, FR-004, A-163):
# `GET /plan` expone el estado de la suscripción. `vencido` conserva su nombre y tipo pero pasa a
# significar «bloqueada» (A-163): durante la gracia de 5 días el tenant NO está vencido para el
# frontend, que lo usa para ocultar módulos.
# ======================================================================


def _tenant_que_vence(db, plan, vence):
    """Tenant mensual con `plan_vence_en` fijado a mano (`make_tenant` lo calcula solo y no acepta
    `plan_vence_en` por `**kw`)."""
    tenant = fx.make_tenant(db, plan=plan, ciclo_facturacion="mensual")
    tenant.plan_iniciado_en = vence - timedelta(days=30)
    tenant.plan_vence_en = vence
    db.flush()
    return tenant


def _ahora():
    return utc_now().replace(tzinfo=None)


class TestSubscriptionFields(unittest.TestCase):
    """Cada tramo con un margen de ≥ 1 h respecto a su frontera: las fronteras exactas (a la
    hora, al minuto y al segundo) se prueban en `test_subscription_state.py`."""

    #: (descripción, vencimiento respecto a ahora, estado, días restantes, ¿vencido?)
    TRAMOS = (
        ("faltan 3 días", timedelta(days=3), "activa", 3, False),
        ("faltan 47 h", timedelta(hours=47), "por_vencer", 2, False),
        ("venció hace 1 h (gracia recién empezada)", -timedelta(hours=1), "en_gracia", 5, False),
        ("venció hace 119 h (último tramo de la gracia)", -timedelta(hours=119), "en_gracia", 1, False),
        ("venció hace 121 h (gracia agotada)", -timedelta(hours=121), "bloqueada", 0, True),
        ("venció hace 45 días", -timedelta(days=45), "bloqueada", 0, True),
    )

    def _resumen(self, vence):
        db = fx.new_session()
        plan = fx.make_plan(db)
        tenant = _tenant_que_vence(db, plan, vence) if vence is not None else fx.make_tenant(db, plan=plan)
        user = fx.make_user(db, tenant)
        db.commit()
        return plan_router.get_plan_summary(db=db, tenant=tenant, _=user), tenant

    def test_cada_tramo_informa_estado_dias_restantes_y_vencido(self):
        for descripcion, offset, estado, dias, vencido in self.TRAMOS:
            with self.subTest(descripcion):
                summary, _ = self._resumen(_ahora() + offset)

                self.assertEqual(summary["subscription_state"], estado)
                self.assertEqual(summary["dias_restantes"], dias)
                self.assertIs(summary["vencido"], vencido)

    def test_la_gracia_no_cuenta_como_vencido(self):
        """A-163: el frontend oculta módulos cuando `vencido` es verdadero; en gracia todo opera."""
        summary, _ = self._resumen(_ahora() - timedelta(days=2))

        self.assertEqual(summary["subscription_state"], "en_gracia")
        self.assertFalse(summary["vencido"])

    def test_fecha_limite_de_pago_es_el_vencimiento_mas_120_horas(self):
        for descripcion, offset, *_ in self.TRAMOS:
            with self.subTest(descripcion):
                summary, tenant = self._resumen(_ahora() + offset)

                self.assertEqual(summary["fecha_limite_pago"], tenant.plan_vence_en + timedelta(hours=120))

    def test_sin_vencimiento_siempre_activa_sin_dias_ni_fecha_limite(self):
        summary, _ = self._resumen(None)

        self.assertEqual(summary["subscription_state"], "activa")
        self.assertIsNone(summary["dias_restantes"])
        self.assertIsNone(summary["fecha_limite_pago"])
        self.assertIsNone(summary["plan_vence_en"])
        self.assertFalse(summary["vencido"])

    def test_los_campos_existentes_no_cambian_de_forma(self):
        """FR-004: los tres campos son aditivos; `plan_name`, `ciclo_facturacion`, `plan_vence_en`,
        `resources` y `modules` siguen donde estaban."""
        summary, tenant = self._resumen(_ahora() + timedelta(days=3))

        self.assertEqual(
            set(summary),
            {
                "plan_name", "ciclo_facturacion", "plan_vence_en", "vencido",
                "subscription_state", "dias_restantes", "fecha_limite_pago",
                "resources", "modules",
            },
        )
        self.assertEqual(summary["ciclo_facturacion"], "mensual")
        self.assertEqual(summary["plan_vence_en"], tenant.plan_vence_en)


class TestSubscriptionFieldsEnLaRespuesta(unittest.TestCase):
    """`response_model=PlanSummaryResponse` descarta todo lo que el esquema no declare: sin este bloque
    el resumen podría traer los campos nuevos y aun así no llegar al cliente."""

    def test_el_esquema_acepta_y_serializa_los_campos_nuevos(self):
        from app.api.v1.plan.schemas import PlanSummaryResponse

        db = fx.new_session()
        plan = fx.make_plan(db)
        tenant = _tenant_que_vence(db, plan, _ahora() - timedelta(hours=2))
        user = fx.make_user(db, tenant)
        db.commit()
        summary = plan_router.get_plan_summary(db=db, tenant=tenant, _=user)

        dump = PlanSummaryResponse.model_validate(summary).model_dump(mode="json")

        self.assertEqual(dump["subscription_state"], "en_gracia")
        self.assertEqual(dump["dias_restantes"], 5)
        self.assertFalse(dump["vencido"])
        # `UtcDatetime` serializa en UTC con offset explícito (spec 138).
        self.assertTrue(dump["fecha_limite_pago"].endswith("+00:00"))

    def test_el_esquema_rechaza_un_estado_fuera_del_catalogo(self):
        from pydantic import ValidationError

        from app.api.v1.plan.schemas import PlanSummaryResponse

        db = fx.new_session()
        plan = fx.make_plan(db)
        tenant = fx.make_tenant(db, plan=plan)
        user = fx.make_user(db, tenant)
        db.commit()
        summary = plan_router.get_plan_summary(db=db, tenant=tenant, _=user)
        summary["subscription_state"] = "pausada"

        with self.assertRaises(ValidationError):
            PlanSummaryResponse.model_validate(summary)

    def test_get_plan_por_http_devuelve_los_tres_campos(self):
        from unittest import mock

        from starlette.testclient import TestClient

        from app.characterization_tests import fixtures  # noqa: F401  - variables de entorno inertes antes de importar app.main
        from app.core.db import get_db, get_tenant
        from app.core.dependencies import get_current_user

        # `app.main` ejecuta `create_app()` al importarse y esta llama a `initialize_database()`, que abre
        # PostgreSQL; se sustituye por una función vacía ANTES de importar (mismo motivo que
        # `test_module_not_in_plan_error.py`).
        with mock.patch("app.core.db.initialize_database", lambda *a, **k: None):
            from app.main import app

        db = fx.new_session(multithreaded=True)  # TestClient atiende el endpoint en otro hilo
        plan = fx.make_plan(db)
        tenant = _tenant_que_vence(db, plan, _ahora() - timedelta(hours=121))
        cajero = fx.make_user(db, tenant, role_name="CASHIER")
        db.commit()
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_tenant] = lambda: tenant
        app.dependency_overrides[get_current_user] = lambda: cajero
        self.addCleanup(app.dependency_overrides.clear)

        resp = TestClient(app, raise_server_exceptions=False).get("/api/v1/plan")

        self.assertEqual(resp.status_code, 200)  # cualquier rol puede consultar el plan, también bloqueado
        body = resp.json()
        self.assertEqual(body["subscription_state"], "bloqueada")
        self.assertEqual(body["dias_restantes"], 0)
        self.assertTrue(body["vencido"])
        self.assertIsNotNone(body["fecha_limite_pago"])


if __name__ == "__main__":
    unittest.main()
