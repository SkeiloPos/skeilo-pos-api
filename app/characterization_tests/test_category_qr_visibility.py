"""Tests de la nueva funcionalidad — spec 128-categoria-visible-menu-qr, US1:
`visible_en_qr` en `PATCH /categories/{id}` — restricción de rol acotada al
campo (FR-005) y guarda de categoría inactiva (FR-006), ejercitado llamando
directamente al handler del router (mismo patrón que
`test_categories_presentations.py`/`test_category_display_order.py`).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_category_qr_visibility -v
"""
import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from app.characterization_tests import fixtures as fx
from app.api.v1.categories.router import update_category
from app.api.v1.categories.schemas import CategoryUpdate


def _user(role_name: str):
    return SimpleNamespace(role=SimpleNamespace(name=role_name))


# spec 128 (US3): update_category() ahora publica menu.updated tras el
# commit cuando visible_en_qr viene en el payload -- necesita tenant.id.
_TENANT = SimpleNamespace(id=1)


class UpdateCategoryQrVisibilityRoleTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.cat = fx.make_category(self.db, active=True, visible_en_qr=True)
        self.db.commit()

    def test_admin_puede_cambiar_visible_en_qr(self):
        updated = update_category(
            self.cat.id, CategoryUpdate(visible_en_qr=False), self.db, _user("ADMIN"), _TENANT,
        )
        self.assertEqual(updated.visible_en_qr, False)

    def test_cajero_no_puede_cambiar_visible_en_qr_403(self):
        with self.assertRaises(HTTPException) as ctx:
            update_category(
                self.cat.id, CategoryUpdate(visible_en_qr=False), self.db, _user("CASHIER"),
            )
        self.assertEqual(ctx.exception.status_code, 403)
        self.db.rollback()

    def test_cajero_sigue_pudiendo_cambiar_otros_campos_spec_127_decision_3(self):
        """research.md (Decisión 3): la restricción nueva es exclusiva de
        `visible_en_qr` -- el resto del endpoint sigue sin restricción de
        rol, como ya estaba antes de esta spec."""
        updated = update_category(
            self.cat.id,
            CategoryUpdate(name="Con licor (renombrada)", display_order=5),
            self.db,
            _user("CASHIER"),
        )
        self.assertEqual(updated.name, "Con licor (renombrada)")
        self.assertEqual(updated.display_order, 5)

    def test_usuario_sin_rol_no_puede_cambiar_visible_en_qr_403(self):
        with self.assertRaises(HTTPException) as ctx:
            update_category(
                self.cat.id,
                CategoryUpdate(visible_en_qr=False),
                self.db,
                SimpleNamespace(role=None),
            )
        self.assertEqual(ctx.exception.status_code, 403)
        self.db.rollback()


class UpdateCategoryQrVisibilityInactiveGuardTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.cat = fx.make_category(self.db, active=False, visible_en_qr=True)
        self.db.commit()

    def test_categoria_inactiva_rechaza_cambio_de_visible_en_qr_422(self):
        with self.assertRaises(HTTPException) as ctx:
            update_category(
                self.cat.id, CategoryUpdate(visible_en_qr=False), self.db, _user("ADMIN"),
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.db.rollback()

    def test_categoria_inactiva_sigue_permitiendo_activarla_en_la_misma_llamada(self):
        """Activar y fijar visible_en_qr a la vez no debe chocar con la
        guarda (FR-006 solo bloquea una categoría que SIGUE inactiva)."""
        updated = update_category(
            self.cat.id,
            CategoryUpdate(active=True, visible_en_qr=False),
            self.db,
            _user("ADMIN"),
            _TENANT,
        )
        self.assertEqual(updated.active, True)
        self.assertEqual(updated.visible_en_qr, False)


if __name__ == "__main__":
    unittest.main()
