"""Tests de la nueva funcionalidad — spec 100-bootstrap-dashboard-staff:
arranque agregado del panel de staff (`GET /bootstrap`).

No son characterization tests — comportamiento enteramente nuevo. Ejercita
`GET /bootstrap` directamente como función Python (mismo patrón que el resto
de este paquete, p. ej. `test_plan_summary.py`), sin `TestClient`.

    python -m unittest app.characterization_tests.test_bootstrap -v
"""
import unittest
import uuid
from unittest.mock import patch

from app.api.v1.bootstrap import router as bootstrap_router
from app.api.v1.bootstrap import service as bootstrap_service
from app.api.v1.notifications import router as notifications_router
from app.api.v1.plan import router as plan_router
from app.characterization_tests import bootstrap_fixtures as fx
from app.core.models import User
from app.core.utils import generate_passwd_hash


class TestBootstrapCaminoFeliz(unittest.TestCase):
    """Acceptance Scenario 1 de US1, contrato § Caso 1: los 3 bloques son
    exactamente equivalentes a lo que hoy devuelven sus 3 endpoints
    individuales por separado."""

    def test_los_3_bloques_son_equivalentes_a_sus_endpoints_individuales(self):
        db = fx.new_session()
        plan = fx.make_plan(db, name="Básico", mesas_limit=5, inventario_access=True)
        tenant = fx.make_tenant(db, plan=plan)
        user = fx.make_user(db, tenant)
        fx.make_notification(db)
        db.commit()

        result = bootstrap_router.get_bootstrap(db=db, tenant=tenant, user=user)

        expected_plan = plan_router.get_plan_summary(db=db, tenant=tenant, _=user)
        expected_notifications = notifications_router.list_notifications(
            page=1, size=20, after_id=None, limit=100, only_pending=False, db=db, _=user,
        )

        self.assertEqual(result["tenant"], tenant)
        self.assertEqual(result["plan"], expected_plan)
        self.assertEqual(
            [n.id for n in result["notifications"].items],
            [n.id for n in expected_notifications.items],
        )
        self.assertEqual(result["notifications"].total, expected_notifications.total)
        self.assertEqual(result["notifications"].page, expected_notifications.page)
        self.assertEqual(result["notifications"].size, expected_notifications.size)


class TestBootstrapSuperAdmin(unittest.TestCase):
    """Acceptance Scenario 1 de US2, contrato § Caso 2: Super Admin (sin
    `tenant` ni `plan` asignados) no levanta ninguna excepción y recibe los
    3 bloques en `None` — nunca un `404`."""

    def test_super_admin_recibe_los_3_bloques_en_none(self):
        db = fx.new_session()
        role = fx.make_role(db, name="SUPER_ADMIN")
        super_admin = User(
            name="Super Admin",
            email=f"super-{uuid.uuid4()}@example.invalid",
            password_hash=generate_passwd_hash("x" * 10),
            role_id=role.id,
            tenant_id=None,
            active=True,
        )
        db.add(super_admin)
        db.commit()

        result = bootstrap_router.get_bootstrap(db=None, tenant=None, user=super_admin)

        self.assertEqual(result, {"tenant": None, "plan": None, "notifications": None})


class TestBootstrapDegradacionGranular(unittest.TestCase):
    """Acceptance Scenario 2 de US3, contrato § Caso 3, Edge Case 2 de
    spec.md: un fallo en una fuente interna no debe tumbar las otras — cada
    bloque se calcula en su propio `try/except` independiente.

    `tenant_block` (spec 100, US3) es una referencia directa al parámetro ya
    resuelto (`tenant_block = tenant`), sin ninguna consulta ni operación que
    pueda fallar dentro de `build_bootstrap` — por eso no hay aquí un caso
    "solo tenant falla": no existe ningún punto de falla real que forzar."""

    def _setup(self):
        db = fx.new_session()
        plan = fx.make_plan(db, name="Básico")
        tenant = fx.make_tenant(db, plan=plan)
        user = fx.make_user(db, tenant)
        fx.make_notification(db)
        db.commit()
        return db, tenant, user

    def test_solo_plan_falla_tenant_y_notifications_llegan_completos(self):
        db, tenant, user = self._setup()

        with patch.object(
            bootstrap_service.plan_service, "build_plan_summary", side_effect=RuntimeError("boom")
        ):
            result = bootstrap_router.get_bootstrap(db=db, tenant=tenant, user=user)

        self.assertEqual(result["tenant"], tenant)
        self.assertIsNone(result["plan"])
        self.assertEqual(len(result["notifications"].items), 1)

    def test_solo_notifications_falla_tenant_y_plan_llegan_completos(self):
        db, tenant, user = self._setup()

        with patch.object(
            bootstrap_service.notifications_router, "list_notifications", side_effect=RuntimeError("boom")
        ):
            result = bootstrap_router.get_bootstrap(db=db, tenant=tenant, user=user)

        self.assertEqual(result["tenant"], tenant)
        self.assertIsNotNone(result["plan"])
        self.assertIsNone(result["notifications"])

    def test_fallo_simultaneo_de_plan_y_notifications_sigue_devolviendo_200_con_tenant_completo(self):
        db, tenant, user = self._setup()

        with patch.object(
            bootstrap_service.plan_service, "build_plan_summary", side_effect=RuntimeError("boom")
        ), patch.object(
            bootstrap_service.notifications_router, "list_notifications", side_effect=RuntimeError("boom")
        ):
            result = bootstrap_router.get_bootstrap(db=db, tenant=tenant, user=user)

        self.assertEqual(result["tenant"], tenant)
        self.assertIsNone(result["plan"])
        self.assertIsNone(result["notifications"])


if __name__ == "__main__":
    unittest.main()
