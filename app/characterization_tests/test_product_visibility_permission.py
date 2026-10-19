"""Spec 129 (US4, FR-013): solo un rol administrativo puede cambiar la
visibilidad en QR de un producto o marcar/asociar una adición -- `PATCH
/products/{id}` (el único camino para tocar `visible_en_qr`/`is_topping`/
`topping_ids`, no hay un endpoint angosto como el de `available`, spec 093)
sigue dependiendo de `require_tenant_admin` sin cambios, igual que ya
documenta `test_products_cashier_forbidden.py` para el resto de los campos
de ese mismo endpoint.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_product_visibility_permission -v
"""
import unittest

from fastapi import HTTPException

from app.characterization_tests import product_availability_fixtures as fx
from app.core.dependencies import require_tenant_admin
from app.api.v1.products.service import ProductService
from app.api.v1.products.schemas import ProductUpdate


class ProductVisibilityPermissionTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.tenant = fx.make_tenant(self.db)

    def _user(self, role_name: str):
        role = fx.make_role(self.db, name=role_name)
        return fx.make_user(self.db, tenant=self.tenant, role=role)

    def test_cajero_no_alcanza_require_tenant_admin_para_editar_un_producto(self):
        """El router exige `require_tenant_admin` antes de llamar a
        `ProductService.update_product` (único camino para `visible_en_qr`/
        `is_topping`/`topping_ids`) -- un Cajero nunca llega a la lógica de
        negocio porque la dependencia ya lo rechaza con 403."""
        cajero = self._user("CASHIER")
        with self.assertRaises(HTTPException) as ctx:
            require_tenant_admin(cajero)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_admin_puede_cambiar_visible_en_qr_y_asociar_adiciones(self):
        admin = self._user("ADMIN")
        self.assertIs(require_tenant_admin(admin), admin)

        category = fx.make_category(self.db)
        producto = fx.make_product(self.db, category=category)
        self.db.commit()
        service = ProductService()

        updated = service.update_product(
            self.db, self.tenant, producto.id, ProductUpdate(visible_en_qr=False)
        )

        self.assertFalse(updated.visible_en_qr)


if __name__ == "__main__":
    unittest.main()
