"""Tests de la nueva funcionalidad — spec 159, User Story 4: administrar el
estado y los datos de un usuario (FR-016 a FR-022, FR-025 a FR-027 parcial).

No son characterization tests: editar/activar/suspender/enviar recuperación
son comportamiento nuevo de esta spec. Invoca las funciones de endpoint
directamente (mismo patrón que `test_super_admin_user_detail.py`).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_super_admin_user_actions -v
"""
import asyncio
import unittest
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException

from app.characterization_tests import plan_fixtures as fx
from app.characterization_tests import super_admin_http_fixtures as sa_fx
from app.api.v1.super_admin import users_router
from app.api.v1.super_admin.schemas import SuspendUserRequest, UserUpdate
from app.models.admin_audit_log import AdminAuditLog


class _FakeRequest:
    def __init__(self, ip: str | None = "10.0.0.1"):
        self.client = SimpleNamespace(host=ip) if ip is not None else None


class TestUpdateUser(unittest.TestCase):
    def test_edita_nombre_y_registra_profile_updated(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Pizzeria")
        actor = sa_fx.make_super_admin(db)
        user = fx.make_user(db, t, name="Ana", status="active")
        db.commit()

        result = users_router.update_user(
            user.id, UserUpdate(name="Ana Maria"), _FakeRequest(), db=db, actor=actor
        )
        self.assertEqual(result.name, "Ana Maria")

        log = db.query(AdminAuditLog).filter_by(affected_user_id=user.id).one()
        self.assertEqual(log.action, "profile_updated")
        self.assertEqual(log.actor_type, "admin")

    def test_cambia_rol_y_registra_role_changed(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Cafeteria")
        actor = sa_fx.make_super_admin(db)
        user = fx.make_user(db, t, status="active")
        nuevo_rol = fx.make_role(db, name="CAJERO")
        db.commit()

        result = users_router.update_user(
            user.id, UserUpdate(role_id=nuevo_rol.id), _FakeRequest(), db=db, actor=actor
        )
        self.assertEqual(result.id, user.id)

        log = db.query(AdminAuditLog).filter_by(affected_user_id=user.id).one()
        self.assertEqual(log.action, "role_changed")
        self.assertEqual(log.detail["rol_nuevo"], "CAJERO")

    def test_asigna_negocio_adicional_por_membresia(self):
        db = fx.new_session()
        t1 = fx.make_tenant(db, name="Negocio Uno")
        t2 = fx.make_tenant(db, name="Negocio Dos")
        actor = sa_fx.make_super_admin(db)
        user = fx.make_user(db, t1, status="active")
        rol_t2 = fx.make_role(db, name="MESERO")
        db.commit()

        users_router.update_user(
            user.id,
            UserUpdate(membership_tenant_id=t2.id, membership_role_id=rol_t2.id),
            _FakeRequest(),
            db=db,
            actor=actor,
        )

        memberships = users_router.get_user_memberships(user.id, db=db)
        self.assertEqual(len(memberships), 1)
        self.assertEqual(memberships[0].tenant_name, "Negocio Dos")

    def test_409_si_usuario_anonimizado(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Panaderia")
        actor = sa_fx.make_super_admin(db)
        user = fx.make_user(db, t, status="anonymized")
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            users_router.update_user(
                user.id, UserUpdate(name="X"), _FakeRequest(), db=db, actor=actor
            )
        self.assertEqual(ctx.exception.status_code, 409)


class TestActivateUser(unittest.TestCase):
    def test_activa_pendiente_de_activacion(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Pizzeria")
        actor = sa_fx.make_super_admin(db)
        user = fx.make_user(db, t, status="pending_activation", active=False)
        db.commit()

        result = asyncio.run(
            users_router.activate_user(user.id, _FakeRequest(), db=db, actor=actor)
        )
        self.assertEqual(result.status, "active")
        db.refresh(user)
        self.assertTrue(user.active)
        self.assertIsNotNone(user.activated_at)

    def test_reactiva_suspendido_y_limpia_motivo(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Cafeteria")
        actor = sa_fx.make_super_admin(db)
        user = fx.make_user(
            db, t, status="suspended", active=False, suspended_reason="prueba"
        )
        import datetime as dt
        user.suspended_at = dt.datetime(2026, 1, 1)
        db.commit()

        result = asyncio.run(
            users_router.activate_user(user.id, _FakeRequest(), db=db, actor=actor)
        )
        self.assertEqual(result.status, "active")
        db.refresh(user)
        self.assertIsNone(user.suspended_at)
        self.assertIsNone(user.suspended_reason)

    def test_desbloquea_bloqueado_por_seguridad(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Panaderia")
        actor = sa_fx.make_super_admin(db)
        user = fx.make_user(
            db, t, status="blocked_security", active=False, failed_login_attempts=5
        )
        import datetime as dt
        user.security_blocked_at = dt.datetime(2026, 1, 1)
        db.commit()

        result = asyncio.run(
            users_router.activate_user(user.id, _FakeRequest(), db=db, actor=actor)
        )
        self.assertEqual(result.status, "active")
        db.refresh(user)
        self.assertIsNone(user.security_blocked_at)
        self.assertEqual(user.failed_login_attempts, 0)

    def test_409_si_anonimizado(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Negocio")
        actor = sa_fx.make_super_admin(db)
        user = fx.make_user(db, t, status="anonymized")
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(
                users_router.activate_user(user.id, _FakeRequest(), db=db, actor=actor)
            )
        self.assertEqual(ctx.exception.status_code, 409)


class TestSuspendUser(unittest.TestCase):
    def test_suspende_con_motivo(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Pizzeria")
        actor = sa_fx.make_super_admin(db)
        user = fx.make_user(db, t, status="active")
        db.commit()

        result = users_router.suspend_user(
            user.id,
            SuspendUserRequest(reason="Fraude reportado"),
            _FakeRequest(),
            db=db,
            actor=actor,
        )
        self.assertEqual(result.status, "suspended")
        db.refresh(user)
        self.assertFalse(user.active)
        self.assertEqual(user.suspended_reason, "Fraude reportado")
        self.assertIsNotNone(user.suspended_at)

        log = db.query(AdminAuditLog).filter_by(affected_user_id=user.id).one()
        self.assertEqual(log.action, "suspended")

    def test_400_si_falta_motivo(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Cafeteria")
        actor = sa_fx.make_super_admin(db)
        user = fx.make_user(db, t, status="active")
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            users_router.suspend_user(
                user.id, SuspendUserRequest(reason="   "), _FakeRequest(), db=db, actor=actor
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_revoca_sesiones_si_se_pide(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Panaderia")
        actor = sa_fx.make_super_admin(db)
        user = fx.make_user(db, t, status="active")
        s1 = fx.make_user_session(db, user)
        s2 = fx.make_user_session(db, user)
        db.commit()

        users_router.suspend_user(
            user.id,
            SuspendUserRequest(reason="motivo", revoke_sessions=True),
            _FakeRequest(),
            db=db,
            actor=actor,
        )
        db.refresh(s1)
        db.refresh(s2)
        self.assertIsNotNone(s1.revoked_at)
        self.assertIsNotNone(s2.revoked_at)

    def test_409_si_es_el_ultimo_super_admin_activo(self):
        db = fx.new_session()
        actor = sa_fx.make_super_admin(db)
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            users_router.suspend_user(
                actor.id,
                SuspendUserRequest(reason="motivo"),
                _FakeRequest(),
                db=db,
                actor=actor,
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_permite_suspender_super_admin_si_hay_otro_activo(self):
        db = fx.new_session()
        actor = sa_fx.make_super_admin(db)
        otro = sa_fx.make_super_admin(db)
        db.commit()

        result = users_router.suspend_user(
            otro.id, SuspendUserRequest(reason="motivo"), _FakeRequest(), db=db, actor=actor
        )
        self.assertEqual(result.status, "suspended")


class TestSendPasswordReset(unittest.TestCase):
    def test_encola_correo_y_registra_auditoria(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Pizzeria")
        actor = sa_fx.make_super_admin(db)
        user = fx.make_user(db, t, status="active")
        db.commit()

        import unittest.mock as mock

        with mock.patch("app.api.v1.super_admin.users_router.send_email_task") as task:
            result = users_router.send_password_reset(
                user.id, _FakeRequest(), db=db, actor=actor
            )
        self.assertIn(user.email, result["message"])
        task.delay.assert_called_once()

        log = db.query(AdminAuditLog).filter_by(affected_user_id=user.id).one()
        self.assertEqual(log.action, "password_reset_sent")

    def test_409_si_anonimizado(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Negocio")
        actor = sa_fx.make_super_admin(db)
        user = fx.make_user(db, t, status="anonymized")
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            users_router.send_password_reset(user.id, _FakeRequest(), db=db, actor=actor)
        self.assertEqual(ctx.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
