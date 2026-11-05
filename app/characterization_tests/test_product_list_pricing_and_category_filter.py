"""Spec 152 (rediseño del listado de la Carta del menú): `ProductListResponse`
recortado, precio resumido (`price_display`/`price_from`) y filtro `category_id` de
`GET /products` (`ProductService.list_query`/`attach_price_fields`).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_product_list_pricing_and_category_filter -v
"""
import unittest
from decimal import Decimal

from app.characterization_tests import fixtures as fx
from app.core.pagination import paginate
from app.api.v1.products.service import ProductService
from app.api.v1.products.schemas import ProductListResponse

EXCLUDED_FIELDS = {
    "code", "description", "tracks_inventory", "is_topping",
    "visible_en_qr", "available_changed_by_name",
}


class TestProductListResponseShape(unittest.TestCase):
    """T007 (US1, FR-008/SC-004, data-model.md D4)."""

    def test_listado_no_incluye_campos_excluidos_y_conserva_fechas(self):
        db = fx.new_session()
        category = fx.make_category(db, name="Helados")
        product = fx.make_product(db, category=category, name="Copa de helado")
        fx.make_variant(db, product=product, price=Decimal("9000"))
        db.commit()
        service = ProductService()

        page = paginate(db, service.list_query(), page=1, size=20)
        service.attach_price_fields(page["items"])
        item = ProductListResponse.model_validate(page["items"][0])
        dumped = item.model_dump()

        self.assertFalse(EXCLUDED_FIELDS & dumped.keys())
        self.assertIsNotNone(dumped["created_at"])
        self.assertIn("updated_at", dumped)


class TestPriceDisplayRule(unittest.TestCase):
    """T021 (US3, FR-005/FR-006, data-model.md "Regla de cómputo")."""

    def _price(self, db, product):
        service = ProductService()
        page = paginate(db, service.list_query(), page=1, size=20)
        service.attach_price_fields(page["items"])
        by_id = {p.id: p for p in page["items"]}
        item = ProductListResponse.model_validate(by_id[product.id])
        return item.price_display, item.price_from

    def test_una_variante_activa_con_precio_devuelve_ese_precio_sin_desde(self):
        db = fx.new_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        fx.make_variant(db, product=product, price=Decimal("12000"))
        db.commit()

        price_display, price_from = self._price(db, product)

        self.assertEqual(price_display, Decimal("12000.00"))
        self.assertFalse(price_from)

    def test_dos_variantes_activas_con_precio_devuelve_el_minimo_con_desde(self):
        db = fx.new_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        fx.make_variant(db, product=product, price=Decimal("9000"))
        fx.make_variant(db, product=product, price=Decimal("15000"))
        db.commit()

        price_display, price_from = self._price(db, product)

        self.assertEqual(price_display, Decimal("9000.00"))
        self.assertTrue(price_from)

    def test_sin_variantes_con_precio_devuelve_sin_precio(self):
        db = fx.new_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        fx.make_variant(db, product=product, price=Decimal("0"))
        fx.make_variant(db, product=product, price=Decimal("5000"), active=False)
        db.commit()

        price_display, price_from = self._price(db, product)

        self.assertIsNone(price_display)
        self.assertFalse(price_from)


class TestCategoryFilter(unittest.TestCase):
    """T015 (US2, FR-003/FR-004, contracts/get-products.md)."""

    def test_category_id_filtra_solo_esa_categoria(self):
        db = fx.new_session()
        helados = fx.make_category(db, name="Helados")
        bebidas = fx.make_category(db, name="Bebidas")
        fx.make_product(db, category=helados, name="Copa de helado")
        fx.make_product(db, category=bebidas, name="Jugo de mora")
        db.commit()
        service = ProductService()

        results = db.execute(
            service.list_query(category_id=helados.id)
        ).scalars().all()

        self.assertEqual([p.name for p in results], ["Copa de helado"])

    def test_category_id_combinado_con_search_aplica_and(self):
        db = fx.new_session()
        helados = fx.make_category(db, name="Helados")
        fx.make_product(db, category=helados, name="Copa de helado")
        fx.make_product(db, category=helados, name="Malteada")
        db.commit()
        service = ProductService()

        results = db.execute(
            service.list_query(search="copa", category_id=helados.id)
        ).scalars().all()

        self.assertEqual([p.name for p in results], ["Copa de helado"])

    def test_category_id_omitido_no_cambia_comportamiento_actual(self):
        db = fx.new_session()
        helados = fx.make_category(db, name="Helados")
        bebidas = fx.make_category(db, name="Bebidas")
        fx.make_product(db, category=helados, name="Copa de helado")
        fx.make_product(db, category=bebidas, name="Jugo de mora")
        db.commit()
        service = ProductService()

        results = db.execute(service.list_query()).scalars().all()

        self.assertEqual({p.name for p in results}, {"Copa de helado", "Jugo de mora"})


if __name__ == "__main__":
    unittest.main()
