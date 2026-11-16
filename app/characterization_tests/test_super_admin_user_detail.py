"""Tests de la nueva funcionalidad — spec 159, User Story 3: ficha individual
de usuario del panel Super Admin (FR-010 a FR-015).

No son characterization tests: la ficha (`users_router.py`) es comportamiento
nuevo de esta spec. Invoca las funciones de endpoint directamente (mismo
patrón que `test_super_admin_users_list.py`).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_super_admin_user_detail -v
"""
import unittest
from uuid import uuid4

from fastapi import HTTPException

from app.characterization_tests import plan_fixtures as fx
from app.api.v1.super_admin import users_router
from app.models.admin_audit_log import AdminAuditLog


class TestGetUserDetail(unittest.TestCase):
    def test_devuelve_informacion_general(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Pizzeria")
        user = fx.make_user(db, t, name="Ana", status="active")
        db.commit()

        detail = users_router.get_user_detail(user.id, db=db)
        self.assertEqual(detail.name, "Ana")
        self.assertEqual(detail.status, "active")
        self.assertIsNone(detail.last_login_at)

    def test_ultimo_acceso_es_la_sesion_mas_reciente(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Cafeteria")
        user = fx.make_user(db, t)
        import datetime as dt
        fx.make_user_session(db, user, created_at=dt.datetime(2026, 1, 1))
        fx.make_user_session(db, user, created_at=dt.datetime(2026, 3, 1))
        db.commit()

        detail = users_router.get_user_detail(user.id, db=db)
        self.assertEqual(detail.last_login_at.replace(tzinfo=None), dt.datetime(2026, 3, 1))

    def test_404_si_no_existe(self):
        db = fx.new_session()
        with self.assertRaises(HTTPException) as ctx:
            users_router.get_user_detail(uuid4(), db=db)
        self.assertEqual(ctx.exception.status_code, 404)


class TestGetUserActivity(unittest.TestCase):
    def test_lista_solo_eventos_del_usuario_mas_reciente_primero(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Panaderia")
        user = fx.make_user(db, t)
        otro = fx.make_user(db, t)
        db.add(AdminAuditLog(actor_type="system", affected_user_id=user.id, action="blocked_security"))
        db.add(AdminAuditLog(actor_type="admin", affected_user_id=user.id, action="role_changed"))
        db.add(AdminAuditLog(actor_type="admin", affected_user_id=otro.id, action="suspended"))
        db.commit()

        page = users_router.get_user_activity(user.id, page=1, size=20, db=db)
        self.assertEqual(page["total"], 2)
        actions = [item.action for item in page["items"]]
        self.assertEqual(set(actions), {"blocked_security", "role_changed"})

    def test_vacio_sin_historial(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Heladeria")
        user = fx.make_user(db, t)
        db.commit()

        page = users_router.get_user_activity(user.id, page=1, size=20, db=db)
        self.assertEqual(page["total"], 0)
        self.assertEqual(page["items"], [])


class TestGetUserMemberships(unittest.TestCase):
    def test_usuario_con_dos_negocios_y_roles_distintos(self):
        db = fx.new_session()
        t1 = fx.make_tenant(db, name="Pizzeria Uno")
        t2 = fx.make_tenant(db, name="Panaderia Dos")
        user = fx.make_user(db, t1)
        rol_admin = fx.make_role(db, "ADMIN")
        rol_cajero = fx.make_role(db, "CAJERO")
        fx.make_tenant_membership(db, user, t1, role=rol_admin)
        fx.make_tenant_membership(db, user, t2, role=rol_cajero)
        db.commit()

        memberships = users_router.get_user_memberships(user.id, db=db)
        self.assertEqual(len(memberships), 2)
        by_tenant = {m.tenant_name: m.role_name for m in memberships}
        self.assertEqual(by_tenant["Pizzeria Uno"], "ADMIN")
        self.assertEqual(by_tenant["Panaderia Dos"], "CAJERO")

    def test_vacio_sin_negocios(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Cafeteria")
        user = fx.make_user(db, t)
        db.commit()

        memberships = users_router.get_user_memberships(user.id, db=db)
        self.assertEqual(memberships, [])

    def test_404_si_no_existe(self):
        db = fx.new_session()
        with self.assertRaises(HTTPException):
            users_router.get_user_memberships(uuid4(), db=db)


class TestGetUserSecurity(unittest.TestCase):
    def test_lista_solo_sesiones_activas(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Pizzeria")
        user = fx.make_user(db, t, failed_login_attempts=2)
        import datetime as dt
        fx.make_user_session(db, user, device_label="Chrome en Windows")
        fx.make_user_session(db, user, device_label="Safari en iPhone", revoked_at=dt.datetime(2026, 1, 1))
        db.commit()

        security = users_router.get_user_security(user.id, db=db)
        self.assertEqual(len(security.active_sessions), 1)
        self.assertEqual(security.active_sessions[0].device_label, "Chrome en Windows")
        self.assertEqual(security.failed_login_attempts, 2)
        self.assertIsNone(security.security_blocked_at)

    def test_vacio_sin_sesiones_activas(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Panaderia")
        user = fx.make_user(db, t)
        db.commit()

        security = users_router.get_user_security(user.id, db=db)
        self.assertEqual(security.active_sessions, [])


if __name__ == "__main__":
    unittest.main()
