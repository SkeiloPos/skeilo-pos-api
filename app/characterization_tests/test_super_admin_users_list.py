"""Tests de la nueva funcionalidad — spec 159, User Story 1: listado
centralizado de usuarios del panel Super Admin (FR-001 a FR-007, FR-041).

No son characterization tests: el listado unificado (usuarios + invitaciones
pendientes, research.md Decisión 3) es comportamiento nuevo de esta spec.

Invoca la función de endpoint directamente (mismo patrón que
`test_super_admin_payment_catalog.py`): `Depends(get_current_super_admin)`
solo se resuelve en una request ASGI real, así que aquí se pasa la sesión
directamente.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_super_admin_users_list -v
"""
import unittest
from datetime import date, timedelta

from app.characterization_tests import plan_fixtures as fx
from app.api.v1.super_admin import router


class TestSuperAdminUsersList(unittest.TestCase):
    def _list(self, db, **kwargs):
        kwargs.setdefault("page", 1)
        kwargs.setdefault("size", 20)
        kwargs.setdefault("search", None)
        kwargs.setdefault("status", None)
        kwargs.setdefault("role_id", None)
        kwargs.setdefault("plan_id", None)
        kwargs.setdefault("registered_from", None)
        kwargs.setdefault("registered_to", None)
        kwargs.setdefault("sort_by", "created_at")
        kwargs.setdefault("sort_dir", "desc")
        return router.list_all_users(db=db, **kwargs)

    def test_lista_usuarios_de_varios_negocios_en_una_sola_pagina(self):
        db = fx.new_session()
        t1 = fx.make_tenant(db, name="Pizzeria Uno")
        t2 = fx.make_tenant(db, name="Panaderia Dos")
        fx.make_user(db, t1, name="Ana", email="ana@example.invalid")
        fx.make_user(db, t2, name="Beto", email="beto@example.invalid")
        db.commit()

        page = self._list(db)
        self.assertEqual(page["total"], 2)
        self.assertIsNone(page["empty_reason"])

    def test_busqueda_coincide_por_nombre_correo_o_negocio(self):
        db = fx.new_session()
        t1 = fx.make_tenant(db, name="Pizzeria Uno")
        t2 = fx.make_tenant(db, name="Panaderia Dos")
        fx.make_user(db, t1, name="Ana", email="ana@example.invalid")
        fx.make_user(db, t2, name="Beto", email="beto@example.invalid")
        db.commit()

        by_name = self._list(db, search="ana@example")
        self.assertEqual(by_name["total"], 1)
        self.assertEqual(by_name["items"][0].name, "Ana")

        by_tenant = self._list(db, search="Panaderia")
        self.assertEqual(by_tenant["total"], 1)
        self.assertEqual(by_tenant["items"][0].name, "Beto")

    def test_filtros_de_estado_rol_y_fecha_son_combinables(self):
        db = fx.new_session()
        tenant = fx.make_tenant(db, name="Negocio")
        role_admin = fx.make_role(db, name="ADMIN")
        role_cashier = fx.make_role(db, name="CASHIER")
        active_admin = fx.make_user(
            db, tenant, name="Activo Admin", role=role_admin, status="active",
        )
        fx.make_user(db, tenant, name="Suspendido Cajero", role=role_cashier, status="suspended")
        db.commit()

        result = self._list(db, status=["active"], role_id=active_admin.role_id)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0].name, "Activo Admin")

        old_date = (date.today() - timedelta(days=365)).isoformat()
        result_by_date = self._list(db, registered_from=old_date)
        self.assertEqual(result_by_date["total"], 2)

    def test_orden_por_nombre_y_por_fecha_de_registro(self):
        db = fx.new_session()
        tenant = fx.make_tenant(db, name="Negocio")
        fx.make_user(db, tenant, name="Zeta")
        fx.make_user(db, tenant, name="Alfa")
        db.commit()

        asc = self._list(db, sort_by="name", sort_dir="asc")
        self.assertEqual([u.name for u in asc["items"]], ["Alfa", "Zeta"])

        desc = self._list(db, sort_by="name", sort_dir="desc")
        self.assertEqual([u.name for u in desc["items"]], ["Zeta", "Alfa"])

    def test_paginacion_respeta_page_y_size(self):
        db = fx.new_session()
        tenant = fx.make_tenant(db, name="Negocio")
        for i in range(5):
            fx.make_user(db, tenant, name=f"user-{i}")
        db.commit()

        page1 = self._list(db, page=1, size=2, sort_by="name", sort_dir="asc")
        page2 = self._list(db, page=2, size=2, sort_by="name", sort_dir="asc")
        self.assertEqual(page1["total"], 5)
        self.assertEqual(page1["pages"], 3)
        self.assertEqual(len(page1["items"]), 2)
        self.assertNotEqual(
            [u.id for u in page1["items"]], [u.id for u in page2["items"]],
        )

    def test_invitacion_pendiente_aparece_en_el_listado_unificado(self):
        """research.md Decisión 3: una `UserInvitation` pendiente se proyecta
        como una fila más del listado, con `status=invitation_pending`,
        `memberships_count=0` y `last_login_at=None`."""
        db = fx.new_session()
        tenant = fx.make_tenant(db, name="Negocio")
        fx.make_user(db, tenant, name="Usuario real")
        fx.make_user_invitation(db, tenant, name="Invitado pendiente")
        db.commit()

        result = self._list(db)
        self.assertEqual(result["total"], 2)
        invitation_row = next(u for u in result["items"] if u.name == "Invitado pendiente")
        self.assertEqual(invitation_row.status.value, "invitation_pending")
        self.assertEqual(invitation_row.memberships_count, 0)
        self.assertIsNone(invitation_row.last_login_at)

    def test_filtrar_por_invitation_pending_excluye_usuarios_reales(self):
        db = fx.new_session()
        tenant = fx.make_tenant(db, name="Negocio")
        fx.make_user(db, tenant, name="Usuario real")
        fx.make_user_invitation(db, tenant, name="Invitado pendiente")
        db.commit()

        result = self._list(db, status=["invitation_pending"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0].name, "Invitado pendiente")

    def test_estado_vacio_sin_datos_de_plataforma(self):
        db = fx.new_session()
        db.commit()

        result = self._list(db)
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["empty_reason"], "no_data")

    def test_estado_vacio_sin_resultados_con_filtro_activo(self):
        db = fx.new_session()
        tenant = fx.make_tenant(db, name="Negocio")
        fx.make_user(db, tenant, name="Alguien")
        db.commit()

        result = self._list(db, search="no-existe-nadie-asi")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["empty_reason"], "no_results")

    def test_multi_negocio_muestra_memberships_count_y_preview(self):
        db = fx.new_session()
        tenant_principal = fx.make_tenant(db, name="Principal")
        tenant_secundario = fx.make_tenant(db, name="Secundario")
        user = fx.make_user(db, tenant_principal, name="Multi")
        fx.make_tenant_membership(db, user, tenant_principal)
        fx.make_tenant_membership(db, user, tenant_secundario)
        db.commit()

        result = self._list(db, search="Multi")
        self.assertEqual(result["items"][0].memberships_count, 2)
        self.assertIsNotNone(result["items"][0].memberships_preview)

    def test_lista_roles_para_el_selector_de_filtro(self):
        db = fx.new_session()
        fx.make_role(db, name="ADMIN")
        fx.make_role(db, name="CASHIER")
        db.commit()

        roles = router.list_roles(db=db)
        self.assertEqual(sorted(r.name for r in roles), ["ADMIN", "CASHIER"])

    def test_last_login_at_se_deriva_de_la_sesion_mas_reciente(self):
        db = fx.new_session()
        tenant = fx.make_tenant(db, name="Negocio")
        user = fx.make_user(db, tenant, name="Con sesion")
        fx.make_user_session(db, user)
        db.commit()

        result = self._list(db, search="Con sesion")
        self.assertIsNotNone(result["items"][0].last_login_at)


if __name__ == "__main__":
    unittest.main()
