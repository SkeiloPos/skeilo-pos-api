"""Tests de la nueva funcionalidad — spec 159, User Story 2: indicadores
generales de usuarios del panel Super Admin (FR-008/FR-009).

No son characterization tests: `GET /super-admin/users/stats` es comportamiento
nuevo de esta spec. Invoca la función de endpoint directamente (mismo patrón
que `test_super_admin_users_list.py`).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_super_admin_users_stats -v
"""
import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from app.characterization_tests import plan_fixtures as fx
from app.api.v1.super_admin import router


class TestSuperAdminUsersStats(unittest.TestCase):
    def test_cuenta_total_activos_suspendidos(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Pizzeria")
        fx.make_user(db, t, name="Ana", status="active")
        fx.make_user(db, t, name="Beto", status="active")
        fx.make_user(db, t, name="Caro", status="suspended")
        fx.make_user(db, t, name="Dani", status="blocked_security")
        db.commit()

        stats = router.get_users_stats(db=db)
        self.assertEqual(stats.total, 4)
        self.assertEqual(stats.active, 2)
        self.assertEqual(stats.suspended, 1)

    def test_blocked_security_no_cuenta_como_suspendido(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Panaderia")
        fx.make_user(db, t, status="blocked_security")
        db.commit()

        stats = router.get_users_stats(db=db)
        self.assertEqual(stats.suspended, 0)

    def test_invitaciones_pendientes_no_cuentan_en_total(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Cafeteria")
        fx.make_user(db, t, status="active")
        fx.make_user_invitation(db, t)
        db.commit()

        stats = router.get_users_stats(db=db)
        self.assertEqual(stats.total, 1)

    def test_nuevos_del_mes_respeta_el_limite_del_mes_calendario(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Heladeria")

        # "Ahora" fijo: 2026-03-15 12:00 America/Bogota == 2026-03-15 17:00 UTC.
        fixed_now = datetime(2026, 3, 15, 17, 0, tzinfo=ZoneInfo("UTC"))

        with mock.patch("app.api.v1.super_admin.router.utc_now", return_value=fixed_now):
            # Justo antes de la medianoche local del 1 de marzo (sigue siendo
            # febrero en Bogotá) -- NO debe contar.
            fx.make_user(
                db, t, status="active",
                created_at=datetime(2026, 3, 1, 4, 59, 0),
            )
            # Justo después de la medianoche local del 1 de marzo -- SÍ cuenta.
            fx.make_user(
                db, t, status="active",
                created_at=datetime(2026, 3, 1, 5, 0, 1),
            )
            # Dentro del mes, sin ambigüedad.
            fx.make_user(
                db, t, status="active",
                created_at=datetime(2026, 3, 10, 12, 0, 0),
            )
            # Mes anterior, sin ambigüedad.
            fx.make_user(
                db, t, status="active",
                created_at=datetime(2026, 2, 28, 12, 0, 0),
            )
            db.commit()

            stats = router.get_users_stats(db=db)

        self.assertEqual(stats.new_this_month, 2)


if __name__ == "__main__":
    unittest.main()
