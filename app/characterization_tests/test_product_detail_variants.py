"""Spec 093-cajero-carta-agotado (User Story 1, escenario 9): `GET /products/{id}`
(`ProductService.to_detail_response`) incluye las presentaciones activas con su precio.

Spec 103-detalle-producto-consolidado: a diferencia de lo que decía esta misma spec 093
cuando se creó, `to_detail_response` ahora SÍ incluye receta y grupos de opciones por
variante (embebidos, sin peticiones adicionales por tamaño) -- pero FR-018 de la spec
093 sigue vigente para el rol Cajero: nunca recibe receta, aunque sí grupos de opciones
(mismo criterio que ya aplican por separado `GET /variants/{id}/recipe`, con
`forbid_cashier`, y `GET /variants/{id}/option-groups`, sin esa restricción).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_product_detail_variants -v
"""
import unittest
from decimal import Decimal

from app.characterization_tests import product_availability_fixtures as fx
from app.api.v1.products.service import ProductService


class ProductDetailVariantsTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.service = ProductService()
        self.tenant = fx.make_tenant(self.db)

    def _user(self, role_name: str):
        role = fx.make_role(self.db, name=role_name)
        return fx.make_user(self.db, tenant=self.tenant, role=role)

    def _detail(self, product_id, user):
        product = self.service.get_detail_or_404(self.db, product_id)
        return self.service.to_detail_response(product, user)

    def test_detalle_incluye_presentaciones_activas_con_precio(self):
        admin = self._user("ADMIN")
        product = fx.make_product(self.db)
        viva = fx.make_variant(self.db, product=product, name="1 litro", price=Decimal("18000"))
        fx.make_variant(
            self.db, product=product, name="1/2 litro", price=Decimal("10000"), active=False
        )
        self.db.commit()

        detail = self._detail(product.id, admin)

        self.assertEqual(len(detail.variants), 1)
        [v] = detail.variants
        self.assertEqual(v.id, viva.id)
        self.assertEqual(v.presentation_name, "1 litro")
        self.assertEqual(v.price, Decimal("18000"))
        self.assertTrue(v.active)

    def test_detalle_incluye_las_desactivadas_aparte_sin_pedirlas_por_separado(self):
        """Ajuste posterior a la spec 103: `GET /products/{id}/variants` deja de ser
        necesario para listar las desactivadas -- vienen en la misma respuesta."""
        admin = self._user("ADMIN")
        product = fx.make_product(self.db)
        fx.make_variant(self.db, product=product, name="1 litro", price=Decimal("18000"))
        muerta = fx.make_variant(
            self.db, product=product, name="1/2 litro", price=Decimal("10000"), active=False
        )
        self.db.commit()

        detail = self._detail(product.id, admin)

        self.assertEqual(len(detail.deactivated), 1)
        [d] = detail.deactivated
        self.assertEqual(d.id, muerta.id)
        self.assertEqual(d.presentation_name, "1/2 litro")
        self.assertEqual(d.price, Decimal("10000"))
        self.assertFalse(d.active)

    def test_admin_recibe_receta_y_grupos_de_opciones_embebidos(self):
        """Spec 103, FR-001/FR-002: el detalle de producto embebe receta y grupos de
        opciones por variante, con el mismo contenido que ya devuelven los endpoints
        independientes -- sin peticiones adicionales por tamaño."""
        admin = self._user("ADMIN")
        product = fx.make_product(self.db)
        variant = fx.make_variant(self.db, product=product, name="1 litro")
        unit = fx.make_unit(self.db)
        item = fx.make_inventory_item(self.db, unit=unit, name="Leche")
        fx.make_recipe_item(self.db, variant=variant, item=item, quantity=Decimal("2.5"))
        group = fx.make_option_group(self.db, name="Sabores")
        fx.link_variant_group(self.db, variant, group, min_select=1, max_select=1)
        self.db.commit()

        detail = self._detail(product.id, admin)

        [v] = detail.variants
        self.assertEqual(len(v.recipe), 1)
        self.assertEqual(v.recipe[0].inventory_item_id, item.id)
        self.assertEqual(v.recipe[0].quantity, Decimal("2.5"))
        self.assertEqual(len(v.option_groups), 1)
        self.assertEqual(v.option_groups[0].option_group_id, group.id)
        self.assertEqual(v.option_groups[0].min_select, 1)

    def test_cajero_nunca_recibe_receta_pero_si_grupos_de_opciones(self):
        """Spec 103, FR-003: preserva FR-018 de la spec 093 -- el Cajero puede ver
        "Carta del menú -> detalle de producto" (mismo endpoint) pero nunca su receta.
        Grupos de opciones SÍ se le muestran, igual que ya hace hoy
        `GET /variants/{id}/option-groups` (sin `forbid_cashier`)."""
        cajero = self._user("CASHIER")
        product = fx.make_product(self.db)
        variant = fx.make_variant(self.db, product=product, name="1 litro")
        unit = fx.make_unit(self.db)
        item = fx.make_inventory_item(self.db, unit=unit, name="Leche")
        fx.make_recipe_item(self.db, variant=variant, item=item, quantity=Decimal("2.5"))
        group = fx.make_option_group(self.db, name="Sabores")
        fx.link_variant_group(self.db, variant, group, min_select=1, max_select=1)
        self.db.commit()

        detail = self._detail(product.id, cajero)

        [v] = detail.variants
        self.assertEqual(v.recipe, [])
        self.assertEqual(len(v.option_groups), 1)
        self.assertEqual(v.option_groups[0].option_group_id, group.id)

    def test_tres_variantes_reciben_cada_una_su_propia_receta_y_grupos(self):
        """Guarda contra un error de carga ansiosa que solo materialice la primera
        variante de la lista (research.md D2)."""
        admin = self._user("ADMIN")
        product = fx.make_product(self.db)
        unit = fx.make_unit(self.db)

        variantes = []
        for i in range(3):
            v = fx.make_variant(self.db, product=product, name=f"tamano-{i}")
            item = fx.make_inventory_item(self.db, unit=unit, name=f"insumo-{i}")
            fx.make_recipe_item(self.db, variant=v, item=item, quantity=Decimal(str(i + 1)))
            group = fx.make_option_group(self.db, name=f"grupo-{i}")
            fx.link_variant_group(self.db, v, group, min_select=i, max_select=i + 1)
            variantes.append((v, item, group))
        self.db.commit()

        detail = self._detail(product.id, admin)

        self.assertEqual(len(detail.variants), 3)
        by_id = {v.id: v for v in detail.variants}
        for variant, item, group in variantes:
            dv = by_id[variant.id]
            self.assertEqual(len(dv.recipe), 1)
            self.assertEqual(dv.recipe[0].inventory_item_id, item.id)
            self.assertEqual(len(dv.option_groups), 1)
            self.assertEqual(dv.option_groups[0].option_group_id, group.id)


if __name__ == "__main__":
    unittest.main()
