"""Spec 129 (US1, FR-016): `topping_ids` rechaza la auto-asociación y cualquier id que
no referencie un producto marcado `is_topping=true`; también rechaza que una adición
traiga `topping_ids` propios (adición de una adición, fuera de alcance).

Spec 129 (US1, T017): `GET /products/{id}` (`to_detail_response`) y
`POST`/`PATCH`/`PUT /products` (`to_save_response`) devuelven `topping_ids` ya resuelto,
sin que el cliente tenga que leerlo aparte.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_product_topping_validation -v
"""
import unittest

from fastapi import HTTPException

from app.characterization_tests import fixtures as fx
from app.characterization_tests import product_availability_fixtures as fx_detail
from app.api.v1.products.service import ProductService
from app.api.v1.products.schemas import ProductCreate, ProductUpdate
from app.models.product_topping import ProductTopping

TENANT = fx.make_tenant_stub(schema="tenant")


class TestProductToppingValidation(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.category = fx.make_category(self.db)
        self.db.commit()
        self.service = ProductService()

    def test_rechaza_auto_asociacion(self):
        product = self.service.create_product(
            self.db, TENANT, ProductCreate(category_id=self.category.id, name="Granizado")
        )

        with self.assertRaises(HTTPException) as ctx:
            self.service.update_product(
                self.db, TENANT, product.id,
                ProductUpdate(topping_ids=[product.id]),
            )
        self.assertEqual(ctx.exception.status_code, 422)

    def test_rechaza_id_de_producto_que_no_es_adicion(self):
        producto_a = self.service.create_product(
            self.db, TENANT, ProductCreate(category_id=self.category.id, name="Granizado")
        )
        producto_b = self.service.create_product(
            self.db, TENANT, ProductCreate(category_id=self.category.id, name="Waffle")
        )

        with self.assertRaises(HTTPException) as ctx:
            self.service.update_product(
                self.db, TENANT, producto_a.id,
                ProductUpdate(topping_ids=[producto_b.id]),
            )
        self.assertEqual(ctx.exception.status_code, 422)

    def test_rechaza_id_inexistente(self):
        producto = self.service.create_product(
            self.db, TENANT, ProductCreate(category_id=self.category.id, name="Granizado")
        )

        with self.assertRaises(HTTPException) as ctx:
            self.service.update_product(
                self.db, TENANT, producto.id,
                ProductUpdate(topping_ids=[fx._uid()]),
            )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_rechaza_que_una_adicion_tenga_sus_propias_adiciones(self):
        adicion = self.service.create_product(
            self.db, TENANT,
            ProductCreate(category_id=self.category.id, name="Gomitas", is_topping=True),
        )
        otra_adicion = self.service.create_product(
            self.db, TENANT,
            ProductCreate(category_id=self.category.id, name="Crema", is_topping=True),
        )

        with self.assertRaises(HTTPException) as ctx:
            self.service.update_product(
                self.db, TENANT, adicion.id,
                ProductUpdate(topping_ids=[otra_adicion.id]),
            )
        self.assertEqual(ctx.exception.status_code, 422)

    def test_asociacion_valida_se_persiste_y_es_reemplazo_total(self):
        producto = self.service.create_product(
            self.db, TENANT, ProductCreate(category_id=self.category.id, name="Granizado")
        )
        gomitas = self.service.create_product(
            self.db, TENANT,
            ProductCreate(category_id=self.category.id, name="Gomitas", is_topping=True),
        )
        crema = self.service.create_product(
            self.db, TENANT,
            ProductCreate(category_id=self.category.id, name="Crema", is_topping=True),
        )

        self.service.update_product(
            self.db, TENANT, producto.id, ProductUpdate(topping_ids=[gomitas.id])
        )
        rows = self.db.query(ProductTopping).filter_by(product_id=producto.id).all()
        self.assertEqual([r.topping_product_id for r in rows], [gomitas.id])

        # Reemplazo total: la segunda llamada deja solo "crema", no las dos.
        self.service.update_product(
            self.db, TENANT, producto.id, ProductUpdate(topping_ids=[crema.id])
        )
        rows = self.db.query(ProductTopping).filter_by(product_id=producto.id).all()
        self.assertEqual([r.topping_product_id for r in rows], [crema.id])

    def test_ausente_no_toca_la_asociacion_existente(self):
        producto = self.service.create_product(
            self.db, TENANT, ProductCreate(category_id=self.category.id, name="Granizado")
        )
        gomitas = self.service.create_product(
            self.db, TENANT,
            ProductCreate(category_id=self.category.id, name="Gomitas", is_topping=True),
        )
        self.service.update_product(
            self.db, TENANT, producto.id, ProductUpdate(topping_ids=[gomitas.id])
        )

        self.service.update_product(self.db, TENANT, producto.id, ProductUpdate(name="Granizado XL"))

        rows = self.db.query(ProductTopping).filter_by(product_id=producto.id).all()
        self.assertEqual([r.topping_product_id for r in rows], [gomitas.id])


class TestProductToppingIdsInResponses(unittest.TestCase):
    """T017: `to_detail_response`/`to_save_response` devuelven `topping_ids` ya
    resuelto, sin que el cliente tenga que pedirlo aparte."""

    def setUp(self):
        self.db = fx_detail.new_session()
        self.service = ProductService()
        self.tenant = fx_detail.make_tenant(self.db)
        role = fx_detail.make_role(self.db, name="ADMIN")
        self.admin = fx_detail.make_user(self.db, tenant=self.tenant, role=role)

    def test_to_detail_response_incluye_topping_ids(self):
        producto = self.service.create_product(
            self.db, self.tenant,
            ProductCreate(category_id=fx_detail.make_category(self.db).id, name="Granizado"),
        )
        gomitas = self.service.create_product(
            self.db, self.tenant,
            ProductCreate(category_id=fx_detail.make_category(self.db).id, name="Gomitas", is_topping=True),
        )
        self.service.update_product(
            self.db, self.tenant, producto.id, ProductUpdate(topping_ids=[gomitas.id])
        )

        detail = self.service.to_detail_response(
            self.service.get_detail_or_404(self.db, producto.id), self.admin
        )
        self.assertEqual(detail.topping_ids, [gomitas.id])

    def test_to_save_response_incluye_topping_ids(self):
        producto = self.service.create_product(
            self.db, self.tenant,
            ProductCreate(category_id=fx_detail.make_category(self.db).id, name="Granizado"),
        )
        gomitas = self.service.create_product(
            self.db, self.tenant,
            ProductCreate(category_id=fx_detail.make_category(self.db).id, name="Gomitas", is_topping=True),
        )

        saved = self.service.update_product(
            self.db, self.tenant, producto.id, ProductUpdate(topping_ids=[gomitas.id])
        )
        response = self.service.to_save_response(saved)
        self.assertEqual(response.topping_ids, [gomitas.id])


if __name__ == "__main__":
    unittest.main()
