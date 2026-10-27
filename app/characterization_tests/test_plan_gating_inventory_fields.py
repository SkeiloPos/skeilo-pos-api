"""Tests de la nueva funcionalidad (spec 064, FR-011/FR-012): activar
`Product.tracks_inventory`, o guardar `inventory_item_id`/`item_quantity` en una opción,
exige que el tenant tenga el módulo Inventario incluido en su plan vigente -- gating a
nivel de CAMPO (no de ruta completa, a diferencia de `unit_measures`/`reports`, spec 062).

No son characterization tests -- comportamiento enteramente nuevo. Combina las tablas de
`plan_fixtures.py` (tenants/plans/users) con las de `fixtures.py` (categorías/productos/
grupos de opciones) en una sola sesión SQLite, porque esta spec es la primera que cruza
ambos dominios en el mismo flujo.

    python -m unittest app.characterization_tests.test_plan_gating_inventory_fields -v
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.characterization_tests import fixtures as f
from app.characterization_tests import plan_fixtures as pf
from app.core.models import Base
from app.api.v1.products.service import ProductService
from app.api.v1.products.schemas import ProductCreate, ProductUpdate
from app.api.v1.catalog.router import add_option, update_option
from app.api.v1.catalog.schemas import OptionCreate, OptionUpdate

_TABLE_NAMES = [
    # plan_fixtures.py
    "tenants", "plans", "roles", "users", "user_invitations",
    "dining_tables", "cash_registers", "payment_methods",
    # fixtures.py (catalog)
    "categories", "products", "product_toppings", "product_variants",
    "option_groups", "options", "variant_option_groups",
    "recipe_items", "inventory_items", "inventory_movements", "unit_measures",
    "unit_measure_groups",  # FK obligatoria de `unit_measures.group_id` (grupos de unidades)
    # spec 083: create_product consulta estas dos tablas incluso cuando la
    # herencia no aplica (0 presentaciones asociadas a la categoría).
    "presentations", "category_presentations",
]


def new_session() -> Session:
    """Unión de las tablas de `plan_fixtures.py` y `fixtures.py` -- primera spec que
    necesita ejercitar gating de plan y entidades de catálogo en la misma sesión."""
    tables = [t for t in Base.metadata.tables.values() if t.name in _TABLE_NAMES]
    engine = create_engine("sqlite:///:memory:")
    conn = engine.connect().execution_options(
        schema_translate_map={"tenant": None, "shared": None}
    )
    Base.metadata.create_all(bind=conn, tables=tables)
    conn.commit()
    return Session(bind=conn)


class ProductTracksInventoryGatingTests(unittest.TestCase):
    """FR-011/FR-012: activar el switch exige el módulo Inventario en el plan."""

    def setUp(self):
        self.db = new_session()
        self.service = ProductService()

    def _sin_inventario(self):
        plan = pf.make_plan(self.db, inventario_access=False)
        return pf.make_tenant(self.db, plan=plan)

    def _con_inventario(self):
        plan = pf.make_plan(self.db, inventario_access=True)
        return pf.make_tenant(self.db, plan=plan)

    def test_crear_producto_con_tracks_inventory_true_sin_modulo_es_rechazado(self):
        tenant = self._sin_inventario()
        category = f.make_category(self.db)
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            self.service.create_product(
                self.db, tenant,
                ProductCreate(
                    category_id=category.id, name="Copa Grande",
                    preparation_type="prepared", tracks_inventory=True,
                ),
            )
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("inventario", ctx.exception.detail.lower())

    def test_crear_producto_con_tracks_inventory_false_no_exige_modulo(self):
        """Acceptance Scenario 1 de US5: un topping/producto sin inventario se crea con
        normalidad sin importar el plan."""
        tenant = self._sin_inventario()
        category = f.make_category(self.db)
        self.db.commit()

        product = self.service.create_product(
            self.db, tenant,
            ProductCreate(
                category_id=category.id, name="Domicilio",
                preparation_type="packaged", tracks_inventory=False,
            ),
        )
        self.assertFalse(product.tracks_inventory)

    def test_crear_producto_con_tracks_inventory_true_con_modulo_funciona(self):
        tenant = self._con_inventario()
        category = f.make_category(self.db)
        self.db.commit()

        product = self.service.create_product(
            self.db, tenant,
            ProductCreate(
                category_id=category.id, name="Copa Grande",
                preparation_type="prepared", tracks_inventory=True,
            ),
        )
        self.assertTrue(product.tracks_inventory)

    def test_activar_tracks_inventory_en_update_sin_modulo_es_rechazado(self):
        tenant = self._sin_inventario()
        product = f.make_product(self.db, tracks_inventory=False)
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            self.service.update_product(
                self.db, tenant, product.id, ProductUpdate(tracks_inventory=True)
            )
        self.assertEqual(ctx.exception.status_code, 403)

    def test_apagar_tracks_inventory_nunca_exige_modulo(self):
        tenant = self._sin_inventario()
        product = f.make_product(self.db, tracks_inventory=True)
        self.db.commit()

        result = self.service.update_product(
            self.db, tenant, product.id, ProductUpdate(tracks_inventory=False)
        )
        self.assertFalse(result.tracks_inventory)

    def test_patch_que_no_toca_tracks_inventory_no_reevalua_el_plan(self):
        """Un producto que YA tenía tracks_inventory=True (dato conservado, FR-013)
        puede seguir editándose (otros campos) sin perder acceso al reevaluar el plan
        en cada PATCH -- solo se reevalúa cuando el valor efectivamente cambia a True."""
        tenant = self._sin_inventario()
        product = f.make_product(self.db, tracks_inventory=True)
        self.db.commit()

        result = self.service.update_product(
            self.db, tenant, product.id, ProductUpdate(name="Nuevo nombre")
        )
        self.assertEqual(result.name, "Nuevo nombre")
        self.assertTrue(result.tracks_inventory)


class OptionInventoryFieldsGatingTests(unittest.TestCase):
    """FR-011/FR-012: guardar insumo o cantidad de consumo en una opción exige el
    módulo Inventario en el plan -- un topping solo con precio no lo exige nunca."""

    def setUp(self):
        self.db = new_session()

    def _sin_inventario(self):
        plan = pf.make_plan(self.db, inventario_access=False)
        return pf.make_tenant(self.db, plan=plan)

    def _con_inventario(self):
        plan = pf.make_plan(self.db, inventario_access=True)
        return pf.make_tenant(self.db, plan=plan)

    def test_crear_opcion_con_insumo_sin_modulo_es_rechazada(self):
        tenant = self._sin_inventario()
        group = f.make_option_group(self.db, pricing_type="incluido")
        item = f.make_inventory_item(self.db)
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            add_option(
                group.id,
                OptionCreate(name="Fresa", inventory_item_id=item.id, item_quantity=Decimal("80")),
                self.db, tenant, None,
            )
        self.assertEqual(ctx.exception.status_code, 403)

    def test_crear_opcion_solo_con_precio_sin_modulo_funciona(self):
        """Acceptance Scenario 2 de US5: un topping puro (sin insumo) se crea con
        normalidad sin importar el plan."""
        tenant = self._sin_inventario()
        group = f.make_option_group(self.db, pricing_type="con_recargo")
        self.db.commit()

        option = add_option(
            group.id, OptionCreate(name="Maní", extra_price=Decimal("500")),
            self.db, tenant, None,
        )
        self.assertIsNone(option.inventory_item_id)
        self.assertEqual(option.extra_price, Decimal("500"))

    def test_crear_opcion_con_insumo_con_modulo_funciona(self):
        tenant = self._con_inventario()
        group = f.make_option_group(self.db, pricing_type="incluido")
        item = f.make_inventory_item(self.db)
        self.db.commit()

        option = add_option(
            group.id,
            OptionCreate(name="Fresa", inventory_item_id=item.id, item_quantity=Decimal("80")),
            self.db, tenant, None,
        )
        self.assertEqual(option.inventory_item_id, item.id)

    def test_enlazar_insumo_en_update_sin_modulo_es_rechazado(self):
        tenant = self._sin_inventario()
        group = f.make_option_group(self.db, pricing_type="incluido")
        option = f.make_option(self.db, group=group)
        item = f.make_inventory_item(self.db)
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            update_option(
                option.id,
                OptionUpdate(inventory_item_id=item.id, item_quantity=Decimal("50")),
                self.db, tenant, None,
            )
        self.assertEqual(ctx.exception.status_code, 403)

    def test_desvincular_insumo_nunca_exige_modulo(self):
        """RN-CAT-38 (spec 004) + FR-012: desvincular (que ya fuerza item_quantity=0)
        nunca exige el módulo, sin importar el plan."""
        tenant = self._sin_inventario()
        group = f.make_option_group(self.db, pricing_type="incluido")
        item = f.make_inventory_item(self.db)
        option = f.make_option(
            self.db, group=group, inventory_item_id=item.id, item_quantity=Decimal("80")
        )
        self.db.commit()

        result = update_option(
            option.id, OptionUpdate(inventory_item_id=None), self.db, tenant, None,
        )
        self.assertIsNone(result.inventory_item_id)
        self.assertEqual(result.item_quantity, Decimal("0"))

    def test_editar_otro_campo_de_opcion_con_insumo_heredado_no_exige_modulo(self):
        """FR-013 + contracts/inventory-field-plan-gating.md: una opción que YA tenía
        insumo/cantidad configurados antes de perder el acceso al módulo puede seguir
        editándose en cualquier otro campo (aquí, el nombre) sin que el insumo heredado
        dispare un 403 -- el gating solo se activa cuando el request intenta agregar o
        aumentar consumo, no por el mero hecho de que la opción ya tenga uno."""
        tenant = self._sin_inventario()
        group = f.make_option_group(self.db, pricing_type="incluido")
        item = f.make_inventory_item(self.db)
        option = f.make_option(
            self.db, group=group, inventory_item_id=item.id, item_quantity=Decimal("80")
        )
        self.db.commit()

        result = update_option(
            option.id, OptionUpdate(name="Fresa (renombrada)"), self.db, tenant, None,
        )
        self.assertEqual(result.name, "Fresa (renombrada)")
        self.assertEqual(result.inventory_item_id, item.id)
        self.assertEqual(result.item_quantity, Decimal("80"))

    def test_subir_cantidad_de_opcion_con_insumo_heredado_si_exige_modulo(self):
        """Contraparte del test anterior: SÍ es "aumentar consumo" y por lo tanto sí
        exige el módulo, aunque el insumo ya estuviera enlazado de antes."""
        tenant = self._sin_inventario()
        group = f.make_option_group(self.db, pricing_type="incluido")
        item = f.make_inventory_item(self.db)
        option = f.make_option(
            self.db, group=group, inventory_item_id=item.id, item_quantity=Decimal("80")
        )
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            update_option(
                option.id, OptionUpdate(item_quantity=Decimal("100")), self.db, tenant, None,
            )
        self.assertEqual(ctx.exception.status_code, 403)

    def test_producto_y_opcion_con_inventario_ya_configurado_conservan_sus_datos_sin_modulo(self):
        """FR-013: retirar el acceso a Inventario no borra tracks_inventory ni el
        insumo/cantidad ya guardados -- un GET (o cualquier lectura) los sigue viendo."""
        tenant = self._sin_inventario()
        product = f.make_product(self.db, tracks_inventory=True)
        group = f.make_option_group(self.db, pricing_type="incluido")
        item = f.make_inventory_item(self.db)
        option = f.make_option(
            self.db, group=group, inventory_item_id=item.id, item_quantity=Decimal("80")
        )
        self.db.commit()

        # ninguna operación de LECTURA se ve afectada por el gating (solo escritura)
        self.db.refresh(product)
        self.db.refresh(option)
        self.assertTrue(product.tracks_inventory)
        self.assertEqual(option.inventory_item_id, item.id)
        self.assertEqual(option.item_quantity, Decimal("80"))


if __name__ == "__main__":
    unittest.main()
