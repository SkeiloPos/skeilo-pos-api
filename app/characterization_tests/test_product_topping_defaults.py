"""Spec 129 (US1, FR-010, research.md D6): `visible_en_qr` nace con un default
calculado a partir de `is_topping` -- oculto si es una adición, visible en caso
contrario -- a menos que el formulario lo envíe explícito.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_product_topping_defaults -v
"""
import unittest

from app.characterization_tests import fixtures as fx
from app.api.v1.products.service import ProductService
from app.api.v1.products.schemas import ProductCreate

TENANT = fx.make_tenant_stub(schema="tenant")


class TestProductToppingDefaults(unittest.TestCase):
    def test_producto_regular_nace_visible_en_qr_sin_pedirlo(self):
        db = fx.new_session()
        category = fx.make_category(db)
        db.commit()
        service = ProductService()

        product = service.create_product(
            db, TENANT, ProductCreate(category_id=category.id, name="Granizado")
        )

        self.assertFalse(product.is_topping)
        self.assertTrue(product.visible_en_qr)

    def test_adicion_nace_oculta_en_qr_sin_pedirlo(self):
        """FR-010: el administrador no tiene que ocultarla manualmente."""
        db = fx.new_session()
        category = fx.make_category(db)
        db.commit()
        service = ProductService()

        product = service.create_product(
            db, TENANT,
            ProductCreate(category_id=category.id, name="Gomitas", is_topping=True),
        )

        self.assertTrue(product.is_topping)
        self.assertFalse(product.visible_en_qr)

    def test_visible_en_qr_explicito_siempre_gana_sobre_el_default(self):
        db = fx.new_session()
        category = fx.make_category(db)
        db.commit()
        service = ProductService()

        product = service.create_product(
            db, TENANT,
            ProductCreate(
                category_id=category.id, name="Gomitas visibles",
                is_topping=True, visible_en_qr=True,
            ),
        )

        self.assertTrue(product.is_topping)
        self.assertTrue(product.visible_en_qr)


if __name__ == "__main__":
    unittest.main()
