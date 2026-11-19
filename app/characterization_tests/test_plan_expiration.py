"""Tests de la nueva funcionalidad — spec 033-planes-suscripcion-tenant,
Historia de Usuario 5: el sistema bloquea automáticamente a un tenant cuyo
plan vence sin renovarse (FR-019/FR-020/FR-021).

No son characterization tests — comportamiento enteramente nuevo.

Este fichero se escribe ANTES de `ensure_plan_not_expired` (T048) a
propósito (tasks.md, Nota de alcance mínimo): debe FALLAR contra el código
de las Historias 1-4 tal cual, porque el bloqueo por vencimiento todavía no
existe — es la prueba de que esta historia agrega comportamiento real, no
solo documentación.

    python -m unittest app.characterization_tests.test_plan_expiration -v
"""
import unittest
from datetime import datetime, timedelta
from unittest import mock

from fastapi import HTTPException

from app.characterization_tests import plan_fixtures as fx
from app.core.exceptions import SubscriptionBlockedError
from app.core.plan_limits import ensure_plan_not_expired, enforce_plan_limit, require_module_access
from app.core.timezone import utc_now


def _vencido_tenant(db, **plan_kw):
    plan = fx.make_plan(db, mesas_limit=None, inventario_access=True, **plan_kw)
    tenant = fx.make_tenant(
        db, plan=plan, ciclo_facturacion="mensual",
        plan_iniciado_en=utc_now().replace(tzinfo=None) - timedelta(days=45),
    )
    return tenant


class TestPlanExpirationBlocksResourceLimits(unittest.TestCase):
    # ---------------------------------------------------------- Acceptance Scenario 1

    def test_tenant_vencido_bloquea_creacion_de_recurso_con_cupo_disponible(self):
        db = fx.new_session()
        tenant = _vencido_tenant(db)  # mesas_limit=None (ilimitado) — solo el vencimiento debe bloquear
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            enforce_plan_limit(db, tenant, "mesas")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("venci", ctx.exception.detail.lower())

    # ---------------------------------------------------------- Acceptance Scenario 2

    def test_tenant_vencido_deniega_acceso_a_modulo_incluido(self):
        db = fx.new_session()
        tenant = _vencido_tenant(db)  # inventario_access=True — solo el vencimiento debe bloquear
        user = fx.make_user(db, tenant)
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            require_module_access("inventario")(tenant=tenant, db=db, _user=user)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("venci", ctx.exception.detail.lower())

    # ---------------------------------------------------------- Acceptance Scenario 3

    def test_vencimiento_no_borra_datos_existentes(self):
        db = fx.new_session()
        tenant = _vencido_tenant(db)
        fx.make_dining_table(db)
        db.commit()

        with self.assertRaises(HTTPException):
            enforce_plan_limit(db, tenant, "mesas")

        from sqlalchemy import func, select
        from app.models.dining_table import DiningTable

        count = db.execute(select(func.count()).select_from(DiningTable)).scalar_one()
        self.assertEqual(count, 1)  # sigue existiendo, nada se borró

    # ---------------------------------------------------------- Acceptance Scenario 4

    def test_sin_vencimiento_nunca_bloquea_por_este_motivo(self):
        db = fx.new_session()
        plan = fx.make_plan(db, mesas_limit=None, inventario_access=True)
        tenant = fx.make_tenant(db, plan=plan)  # ciclo_facturacion=None -> plan_vence_en=None
        user = fx.make_user(db, tenant)
        db.commit()

        enforce_plan_limit(db, tenant, "mesas")
        require_module_access("inventario")(tenant=tenant, db=db, _user=user)

    # ---------------------------------------------------------- Acceptance Scenario 5

    def test_renovar_levanta_el_bloqueo_de_inmediato(self):
        db = fx.new_session()
        tenant = _vencido_tenant(db)
        db.commit()

        with self.assertRaises(HTTPException):
            enforce_plan_limit(db, tenant, "mesas")

        # Renovar es el mismo PATCH que US2 ya prueba — aquí solo se simula
        # su efecto de datos (reiniciar plan_iniciado_en/plan_vence_en),
        # que es lo único que ensure_plan_not_expired consulta.
        from app.core.plan_limits import calculate_plan_vencimiento

        tenant.plan_iniciado_en = utc_now().replace(tzinfo=None)
        tenant.plan_vence_en = calculate_plan_vencimiento(tenant.plan_iniciado_en, "mensual")
        db.commit()

        enforce_plan_limit(db, tenant, "mesas")  # ya no bloquea


# ======================================================================
# spec 161-suscripcion-pago-wompi, Historia de Usuario 1 (FR-005, FR-006, FR-007; A-161):
# el vencimiento ya no bloquea de inmediato. Hay 5 días (120 h exactas) de gracia en los que todo
# opera; agotada la gracia, `ensure_plan_not_expired` responde `SubscriptionBlockedError`.
# Las pruebas de arriba (45 días vencido) siguen siendo válidas: están muy pasadas de la gracia.
# ======================================================================

#: Instante fijo para probar las fronteras exactas sin depender del reloj.
_AHORA_FIJO = datetime(2026, 12, 6, 12, 0, 0)


def _tenant_que_vence(db, vence, **plan_kw):
    plan = fx.make_plan(db, mesas_limit=None, inventario_access=True, **plan_kw)
    tenant = fx.make_tenant(db, plan=plan, ciclo_facturacion="mensual")
    tenant.plan_iniciado_en = vence - timedelta(days=30)
    tenant.plan_vence_en = vence
    db.flush()
    return tenant


class TestGraciaAntesDelBloqueo(unittest.TestCase):
    def _reloj_en(self, instante):
        """Fija el "ahora" que lee el cálculo del estado de la suscripción."""
        patcher = mock.patch("app.core.subscription.utc_now_naive", return_value=instante)
        patcher.start()
        self.addCleanup(patcher.stop)

    # ---------------------------------------------------------- durante la gracia todo opera

    def test_recien_vencido_no_bloquea_ni_la_creacion_ni_los_modulos(self):
        db = fx.new_session()
        tenant = _tenant_que_vence(db, _AHORA_FIJO - timedelta(hours=1))
        user = fx.make_user(db, tenant)
        db.commit()
        self._reloj_en(_AHORA_FIJO)

        enforce_plan_limit(db, tenant, "mesas")
        require_module_access("inventario")(tenant=tenant, db=db, _user=user)

    def test_en_el_instante_del_vencimiento_empieza_la_gracia_y_no_bloquea(self):
        db = fx.new_session()
        tenant = _tenant_que_vence(db, _AHORA_FIJO)
        db.commit()
        self._reloj_en(_AHORA_FIJO)

        enforce_plan_limit(db, tenant, "mesas")

    def test_a_119h_59m_59s_de_vencido_sigue_sin_bloquear(self):
        db = fx.new_session()
        tenant = _tenant_que_vence(db, _AHORA_FIJO - timedelta(hours=120) + timedelta(seconds=1))
        user = fx.make_user(db, tenant)
        db.commit()
        self._reloj_en(_AHORA_FIJO)

        enforce_plan_limit(db, tenant, "mesas")
        require_module_access("inventario")(tenant=tenant, db=db, _user=user)

    # ---------------------------------------------------------- agotada la gracia, bloquea

    def test_a_120_horas_exactas_de_vencido_bloquea_la_creacion(self):
        db = fx.new_session()
        tenant = _tenant_que_vence(db, _AHORA_FIJO - timedelta(hours=120))
        db.commit()
        self._reloj_en(_AHORA_FIJO)

        with self.assertRaises(SubscriptionBlockedError) as ctx:
            enforce_plan_limit(db, tenant, "mesas")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_a_120_horas_exactas_de_vencido_bloquea_los_modulos_con_el_code_estable(self):
        db = fx.new_session()
        tenant = _tenant_que_vence(db, _AHORA_FIJO - timedelta(hours=120))
        user = fx.make_user(db, tenant)
        db.commit()
        self._reloj_en(_AHORA_FIJO)

        with self.assertRaises(SubscriptionBlockedError) as ctx:
            require_module_access("inventario")(tenant=tenant, db=db, _user=user)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.code, "subscription_blocked")
        self.assertIn("venci", ctx.exception.detail.lower())

    def test_ensure_plan_not_expired_directo_sigue_la_misma_regla(self):
        db = fx.new_session()
        en_gracia = _tenant_que_vence(db, _AHORA_FIJO - timedelta(hours=119))
        bloqueado = _tenant_que_vence(db, _AHORA_FIJO - timedelta(hours=121))
        db.commit()
        self._reloj_en(_AHORA_FIJO)

        ensure_plan_not_expired(en_gracia)  # no lanza
        with self.assertRaises(SubscriptionBlockedError):
            ensure_plan_not_expired(bloqueado)

    def test_sigue_siendo_un_http_exception_para_los_except_existentes(self):
        db = fx.new_session()
        tenant = _tenant_que_vence(db, _AHORA_FIJO - timedelta(days=10))
        db.commit()
        self._reloj_en(_AHORA_FIJO)

        with self.assertRaises(HTTPException) as ctx:
            enforce_plan_limit(db, tenant, "mesas")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("venci", ctx.exception.detail.lower())

    # ---------------------------------------------------------- renovar levanta el bloqueo

    def test_un_pago_que_mueve_el_vencimiento_levanta_el_bloqueo_de_inmediato(self):
        """FR-009: el estado se recalcula en cada llamada, sin cachés; en cuanto `plan_vence_en` cambia
        (lo que hará el webhook al aprobar un pago), la siguiente operación pasa."""
        db = fx.new_session()
        tenant = _tenant_que_vence(db, _AHORA_FIJO - timedelta(days=10))
        db.commit()
        self._reloj_en(_AHORA_FIJO)
        with self.assertRaises(SubscriptionBlockedError):
            enforce_plan_limit(db, tenant, "mesas")

        tenant.plan_iniciado_en = _AHORA_FIJO
        tenant.plan_vence_en = _AHORA_FIJO + timedelta(days=30)
        db.commit()

        enforce_plan_limit(db, tenant, "mesas")  # ya no bloquea

    def test_sin_vencimiento_nunca_bloquea_aunque_pase_el_tiempo(self):
        db = fx.new_session()
        plan = fx.make_plan(db, mesas_limit=None)
        tenant = fx.make_tenant(db, plan=plan)  # sin ciclo: plan_vence_en NULL
        db.commit()
        self._reloj_en(_AHORA_FIJO + timedelta(days=3650))

        enforce_plan_limit(db, tenant, "mesas")


if __name__ == "__main__":
    unittest.main()
