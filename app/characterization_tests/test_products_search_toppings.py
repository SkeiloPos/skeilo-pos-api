"""Spec 129 (US2, FR-006): el catálogo de búsqueda del pedido manual
(`GET /products?search=...`, `ProductService.list_query`) no filtra productos
marcados `is_topping=true` -- una adición aparece en los resultados igual que
cualquier producto (research.md D3).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_products_search_toppings -v
"""
import unittest

from app.characterization_tests import fixtures as fx
from app.api.v1.products.service import ProductService
from app.models.product import Product


class TestProductsSearchToppings(unittest.TestCase):
    def test_busqueda_sin_filtro_is_topping_incluye_adiciones(self):
        db = fx.new_session()
        category = fx.make_category(db)
        fx.make_product(db, category=category, name="Granizado de mora")
        fx.make_product(db, category=category, name="Gomitas", is_topping=True)
        db.commit()
        service = ProductService()

        results = db.execute(service.list_query(search="gomitas")).scalars().all()

        self.assertEqual([p.name for p in results], ["Gomitas"])
        self.assertTrue(results[0].is_topping)

    def test_is_topping_true_filtra_solo_adiciones(self):
        """Usado por el selector de 'Adiciones disponibles' del formulario de producto."""
        db = fx.new_session()
        category = fx.make_category(db)
        fx.make_product(db, category=category, name="Granizado")
        fx.make_product(db, category=category, name="Gomitas", is_topping=True)
        fx.make_product(db, category=category, name="Crema", is_topping=True)
        db.commit()
        service = ProductService()

        results = db.execute(service.list_query(is_topping=True)).scalars().all()

        self.assertEqual({p.name for p in results}, {"Gomitas", "Crema"})
        self.assertTrue(all(isinstance(p, Product) and p.is_topping for p in results))


if __name__ == "__main__":
    unittest.main()
