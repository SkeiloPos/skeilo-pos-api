"""Tests de la nueva funcionalidad (spec 067): `categories.display_order` —
un valor de orden numérico que el administrador puede definir al crear o
editar una categoría, y que el filtro del Menú QR respeta (mayor a menor,
desempate alfabético). No son characterization tests: no existía ningún
concepto de orden en `categories` antes de esta spec.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_category_display_order -v
"""
import importlib.util
import unittest
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from pydantic import ValidationError

from app.characterization_tests import cart_fixtures as fx
from app.api.v1.categories.router import create_category, update_category, list_categories
from app.api.v1.categories.schemas import CategoryCreate, CategoryUpdate
from app.api.v1.menu.router import _build_menu

_MIG = Path(__file__).resolve().parents[2] / (
    "alembic/versions/94144eaa60b5_categories_display_order.py"
)
_spec = importlib.util.spec_from_file_location("mig_094_categories_display_order", _MIG)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


# ---------------------------------------------------------- US1: POST /categories

class CreateCategoryDisplayOrderTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()

    def test_valor_explicito_se_persiste_tal_cual(self):
        cat = create_category(
            CategoryCreate(name="Bebidas", display_order=10), self.db, None,
        )
        self.assertEqual(cat.display_order, 10)

    def test_sin_valor_asigna_max_mas_uno(self):
        primera = create_category(CategoryCreate(name="Bebidas"), self.db, None)
        segunda = create_category(CategoryCreate(name="Helados"), self.db, None)
        self.assertGreater(segunda.display_order, primera.display_order)
        self.assertEqual(segunda.display_order, primera.display_order + 1)

    def test_primera_categoria_sin_valor_no_bloquea_creacion(self):
        cat = create_category(CategoryCreate(name="Bebidas"), self.db, None)
        self.assertEqual(cat.display_order, 1)


# ---------------------------------------------------------- US1: PATCH /categories/{id}

class UpdateCategoryDisplayOrderTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.cat = fx.make_category(self.db, name="Bebidas", display_order=5)
        self.db.commit()

    def test_valor_explicito_reemplaza_el_existente(self):
        updated = update_category(
            self.cat.id, CategoryUpdate(display_order=25), self.db, None,
        )
        self.assertEqual(updated.display_order, 25)

    def test_sin_el_campo_no_modifica_el_valor_existente(self):
        updated = update_category(
            self.cat.id, CategoryUpdate(description="Nueva descripción"), self.db, None,
        )
        self.assertEqual(updated.display_order, 5)


# ---------------------------------------------------------- FR-003: validación 422

class DisplayOrderValidationTests(unittest.TestCase):
    def test_create_rechaza_valor_negativo(self):
        with self.assertRaises(ValidationError):
            CategoryCreate(name="Bebidas", display_order=-1)

    def test_create_rechaza_valor_no_numerico(self):
        with self.assertRaises(ValidationError):
            CategoryCreate(name="Bebidas", display_order="abc")

    def test_update_rechaza_valor_negativo(self):
        with self.assertRaises(ValidationError):
            CategoryUpdate(display_order=-1)


# ---------------------------------------------------------- US2: _build_menu (orden)

class MenuOrderingTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()

    def _seeded_category(self, **kw):
        # `_build_menu` omite del resultado cualquier categoría sin
        # productos con al menos una variante activa (menu/router.py:198) --
        # cada categoría de este test necesita las dos para aparecer.
        cat = fx.make_category(self.db, **kw)
        product = fx.make_product(self.db, category=cat)
        fx.make_variant(self.db, product=product)
        return cat

    def test_categorias_activas_se_devuelven_de_mayor_a_menor(self):
        self._seeded_category(name="Postres", display_order=10)
        self._seeded_category(name="Bebidas", display_order=5)
        self._seeded_category(name="Helados", display_order=1)
        self.db.commit()

        menu = _build_menu(self.db)
        self.assertEqual([c.name for c in menu], ["Postres", "Bebidas", "Helados"])

    def test_empate_de_orden_se_desempata_alfabeticamente(self):
        self._seeded_category(name="Postres", display_order=10)
        self._seeded_category(name="Bebidas", display_order=10)
        self.db.commit()

        menu = _build_menu(self.db)
        self.assertEqual([c.name for c in menu], ["Bebidas", "Postres"])

    def test_categoria_inactiva_no_aparece_sin_importar_su_orden(self):
        self._seeded_category(name="Activa", display_order=1)
        self._seeded_category(name="Inactiva", display_order=99, active=False)
        self.db.commit()

        menu = _build_menu(self.db)
        self.assertEqual([c.name for c in menu], ["Activa"])


# ---------------------------------------------------------- Regresión: listado admin

class AdminListingUnaffectedTests(unittest.TestCase):
    def test_get_categories_sigue_ordenado_por_nombre_no_por_display_order(self):
        db = fx.new_session()
        fx.make_category(db, name="Postres", display_order=1)
        fx.make_category(db, name="Bebidas", display_order=99)
        fx.make_category(db, name="Helados", display_order=50)
        db.commit()

        page = list_categories(
            page=1, size=20, active=None, visible_en_qr=None, search=None, db=db, _=None, user=None,
        )
        # `paginate()` devuelve un dict crudo (app/core/pagination.py) -- FastAPI
        # solo lo valida contra `Page[CategoryResponse]` en el ciclo HTTP real,
        # que este test no ejercita (llama al handler directo, research.md).
        self.assertEqual([c.name for c in page["items"]], ["Bebidas", "Helados", "Postres"])


# ---------------------------------------------------------- FR-009/SC-003: backfill

class BackfillFormulaTests(unittest.TestCase):
    """Verifica la fórmula de backfill de la migración `94144eaa60b5`
    (research.md Decisión 4): tras aplicarla, ordenar por
    `display_order DESC` reproduce exactamente el orden alfabético
    ascendente por `name` que el Menú QR ya mostraba antes de esta spec."""

    def setUp(self):
        self.engine = sa.create_engine("sqlite:///:memory:")
        self.conn = self.engine.connect()
        self.conn.execute(sa.text(
            "CREATE TABLE categories (id TEXT PRIMARY KEY, name TEXT, display_order INTEGER)"
        ))

    def tearDown(self):
        self.conn.close()

    def _seed(self, names):
        for name in names:
            self.conn.execute(
                sa.text("INSERT INTO categories (id, name) VALUES (:id, :name)"),
                {"id": str(uuid4()), "name": name},
            )
        self.conn.commit()

    def test_orden_post_backfill_reproduce_el_orden_alfabetico_previo(self):
        nombres = ["Postres", "Bebidas", "Helados"]
        self._seed(nombres)

        # `backfill_sql` usa `{schema}.categories` -- se prueba sin
        # calificar de schema (SQLite, un solo namespace), igual que el
        # resto de `characterization_tests/` que ejercitan SQL de
        # migraciones (test_promotions_migration.py).
        sql = mig.backfill_sql("main").replace("main.categories", "categories")
        self.conn.execute(sa.text(sql))
        self.conn.commit()

        after = [
            row[0] for row in self.conn.execute(
                sa.text("SELECT name FROM categories ORDER BY display_order DESC, name")
            )
        ]
        before = sorted(nombres)  # `ORDER BY name` (ASC, comportamiento previo)
        self.assertEqual(after, before)
