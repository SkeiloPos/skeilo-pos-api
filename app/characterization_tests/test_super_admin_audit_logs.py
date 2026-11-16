"""Tests de la nueva funcionalidad — spec 159, User Story 7: auditoría
administrativa (FR-033 a FR-038).

No son characterization tests: `audit_router.py` es comportamiento nuevo de
esta spec. Invoca la función de endpoint directamente (mismo patrón que
`test_super_admin_users_list.py`/`test_super_admin_user_detail.py`).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_super_admin_audit_logs -v
"""
import datetime as dt
import unittest
from uuid import uuid4

from app.characterization_tests import plan_fixtures as fx
from app.api.v1.super_admin import audit_router
from app.models.admin_audit_log import AdminAuditLog


class TestListAuditLogs(unittest.TestCase):
    def _call(self, db, **kw):
        kw.setdefault("page", 1)
        kw.setdefault("size", 20)
        kw.setdefault("affected_user_id", None)
        kw.setdefault("actor_user_id", None)
        kw.setdefault("tenant_id", None)
        kw.setdefault("action", None)
        kw.setdefault("date_from", None)
        kw.setdefault("date_to", None)
        return audit_router.list_audit_logs(db=db, **kw)

    def test_vacio_sin_ningun_registro_es_no_data(self):
        db = fx.new_session()
        page = self._call(db)
        self.assertEqual(page["total"], 0)
        self.assertEqual(page["empty_reason"], "no_data")

    def test_lista_todos_mas_reciente_primero(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Pizzeria")
        admin = fx.make_user(db, t, name="Super Admin")
        affected = fx.make_user(db, t, name="Afectado")
        db.add(AdminAuditLog(
            actor_type="admin", actor_user_id=admin.id, actor_name_snapshot=admin.name,
            affected_user_id=affected.id, affected_user_email_snapshot=affected.email,
            tenant_id=t.id, action="role_changed",
            created_at=dt.datetime(2026, 1, 1),
        ))
        db.add(AdminAuditLog(
            actor_type="system", action="blocked_security",
            affected_user_id=affected.id, affected_user_email_snapshot=affected.email,
            created_at=dt.datetime(2026, 2, 1),
        ))
        db.commit()

        page = self._call(db)
        self.assertEqual(page["total"], 2)
        self.assertIsNone(page["empty_reason"])
        self.assertEqual(page["items"][0].action, "blocked_security")
        self.assertEqual(page["items"][1].action, "role_changed")
        self.assertEqual(page["items"][1].tenant_name, "Pizzeria")
        self.assertIsNone(page["items"][0].tenant_name)

    def test_distingue_actor_type_sistema_vs_administrador(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Cafeteria")
        admin = fx.make_user(db, t)
        db.add(AdminAuditLog(actor_type="admin", actor_user_id=admin.id, action="suspended"))
        db.add(AdminAuditLog(actor_type="system", action="blocked_security"))
        db.commit()

        page = self._call(db)
        actor_types = {item.actor_type for item in page["items"]}
        self.assertEqual(actor_types, {"admin", "system"})

    def test_filtro_combinable_affected_user_y_action(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Panaderia")
        user_a = fx.make_user(db, t)
        user_b = fx.make_user(db, t)
        db.add(AdminAuditLog(actor_type="admin", affected_user_id=user_a.id, action="suspended"))
        db.add(AdminAuditLog(actor_type="admin", affected_user_id=user_a.id, action="role_changed"))
        db.add(AdminAuditLog(actor_type="admin", affected_user_id=user_b.id, action="suspended"))
        db.commit()

        page = self._call(db, affected_user_id=user_a.id, action="suspended")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0].affected_user_id, user_a.id)
        self.assertEqual(page["items"][0].action, "suspended")

    def test_filtro_por_actor_user_id(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Heladeria")
        admin1 = fx.make_user(db, t)
        admin2 = fx.make_user(db, t)
        db.add(AdminAuditLog(actor_type="admin", actor_user_id=admin1.id, action="suspended"))
        db.add(AdminAuditLog(actor_type="admin", actor_user_id=admin2.id, action="activated"))
        db.commit()

        page = self._call(db, actor_user_id=admin1.id)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0].actor_user_id, admin1.id)

    def test_filtro_por_tenant_id(self):
        db = fx.new_session()
        t1 = fx.make_tenant(db, name="Negocio 1")
        t2 = fx.make_tenant(db, name="Negocio 2")
        db.add(AdminAuditLog(actor_type="admin", tenant_id=t1.id, action="role_changed"))
        db.add(AdminAuditLog(actor_type="admin", tenant_id=t2.id, action="role_changed"))
        db.commit()

        page = self._call(db, tenant_id=t1.id)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0].tenant_id, t1.id)

    def test_filtro_por_rango_de_fechas(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Restaurante")
        db.add(AdminAuditLog(actor_type="system", action="blocked_security", created_at=dt.datetime(2026, 1, 15)))
        db.add(AdminAuditLog(actor_type="system", action="blocked_security", created_at=dt.datetime(2026, 2, 15)))
        db.add(AdminAuditLog(actor_type="system", action="blocked_security", created_at=dt.datetime(2026, 3, 15)))
        db.commit()

        page = self._call(
            db,
            date_from=dt.date(2026, 2, 1),
            date_to=dt.date(2026, 2, 28),
        )
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0].created_at.replace(tzinfo=None), dt.datetime(2026, 2, 15))

    def test_filtros_combinados_sin_coincidencias_es_no_results(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Buñueleria")
        db.add(AdminAuditLog(actor_type="admin", action="suspended", affected_user_id=uuid4()))
        db.commit()

        page = self._call(db, action="deleted")
        self.assertEqual(page["total"], 0)
        self.assertEqual(page["empty_reason"], "no_results")

    def test_sobrevive_a_un_usuario_eliminado(self):
        """FR-038: la bitácora es consultable de forma independiente al
        registro eliminado -- `affected_user_id=None` tras un borrado no
        rompe la consulta ni la respuesta."""
        db = fx.new_session()
        db.add(AdminAuditLog(
            actor_type="admin", action="deleted", affected_user_id=None,
            affected_user_email_snapshot="borrado@example.invalid",
        ))
        db.commit()

        page = self._call(db)
        self.assertEqual(page["total"], 1)
        self.assertIsNone(page["items"][0].affected_user_id)
        self.assertEqual(page["items"][0].affected_user_email_snapshot, "borrado@example.invalid")

    def test_paginacion(self):
        db = fx.new_session()
        for _ in range(3):
            db.add(AdminAuditLog(actor_type="system", action="blocked_security"))
        db.commit()

        page = self._call(db, page=1, size=2)
        self.assertEqual(page["total"], 3)
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual(page["pages"], 2)

        page2 = self._call(db, page=2, size=2)
        self.assertEqual(len(page2["items"]), 1)


if __name__ == "__main__":
    unittest.main()
