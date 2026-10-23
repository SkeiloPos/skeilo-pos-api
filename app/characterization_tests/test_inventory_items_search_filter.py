"""CONGELA app/api/v1/inventory/service.py::list_items_query() — búsqueda y filtros del
listado de insumos (GET /inventory/items).

Fija el comportamiento existente de `search` sobre `name` (FR-010, sin regresión) antes de
ampliarlo para que también compare contra `code`, y agrega los casos nuevos de spec 133:
búsqueda por código, filtro por proveedor (solo relaciones activas) y su combinación con los
filtros ya existentes de tipo/estado.

Referencia: specs/133-busqueda-filtro-inventario/contracts/inventory-items-list.md
"""
import unittest

from app.characterization_tests import fixtures as f
from app.api.v1.inventory.service import list_items_query


class ListItemsQuerySearchTests(unittest.TestCase):
    def setUp(self):
        self.db = f.new_session()

    def _search(self, **kwargs):
        stmt = list_items_query(**kwargs)
        return self.db.execute(stmt).scalars().all()

    def test_search_por_nombre_sigue_funcionando_igual_que_antes(self):
        a = f.make_inventory_item(self.db, name="Harina de trigo", code="HAR-001")
        f.make_inventory_item(self.db, name="Azúcar blanca", code="AZU-001")
        resultados = self._search(search="harina")
        self.assertEqual([i.id for i in resultados], [a.id])

    def test_search_sin_coincidencias_devuelve_vacio(self):
        f.make_inventory_item(self.db, name="Harina de trigo", code="HAR-001")
        self.assertEqual(self._search(search="xyz-no-existe"), [])

    def test_search_por_codigo_encuentra_el_insumo(self):
        a = f.make_inventory_item(self.db, name="Harina de trigo", code="HAR-001")
        f.make_inventory_item(self.db, name="Azúcar blanca", code="AZU-001")
        resultados = self._search(search="HAR-001")
        self.assertEqual([i.id for i in resultados], [a.id])

    def test_search_que_coincide_con_nombre_de_a_y_codigo_de_b_devuelve_ambos(self):
        a = f.make_inventory_item(self.db, name="Mango maduro", code="MNG-900")
        b = f.make_inventory_item(self.db, name="Leche entera", code="LEC-MANGO")
        resultados = {i.id for i in self._search(search="mango")}
        self.assertEqual(resultados, {a.id, b.id})

    def test_supplier_id_con_relacion_activa_devuelve_solo_esos_insumos(self):
        proveedor = f.make_supplier(self.db)
        vinculado = f.make_inventory_item(self.db)
        no_vinculado = f.make_inventory_item(self.db)
        f.make_supplier_item(self.db, supplier=proveedor, inventory_item=vinculado, active=True)
        resultados = self._search(supplier_id=proveedor.id)
        self.assertEqual([i.id for i in resultados], [vinculado.id])
        self.assertNotIn(no_vinculado.id, [i.id for i in resultados])

    def test_supplier_id_sin_relaciones_activas_devuelve_vacio(self):
        proveedor = f.make_supplier(self.db)
        item = f.make_inventory_item(self.db)
        f.make_supplier_item(self.db, supplier=proveedor, inventory_item=item, active=False)
        self.assertEqual(self._search(supplier_id=proveedor.id), [])

    def test_supplier_id_ignora_relacion_inactiva_aunque_exista_otra_activa_de_otro_proveedor(self):
        proveedor_a = f.make_supplier(self.db)
        proveedor_b = f.make_supplier(self.db)
        item = f.make_inventory_item(self.db)
        f.make_supplier_item(self.db, supplier=proveedor_a, inventory_item=item, active=False)
        f.make_supplier_item(self.db, supplier=proveedor_b, inventory_item=item, active=True)
        self.assertEqual(self._search(supplier_id=proveedor_a.id), [])
        self.assertEqual([i.id for i in self._search(supplier_id=proveedor_b.id)], [item.id])

    def test_search_y_supplier_id_simultaneos_devuelven_la_interseccion(self):
        proveedor = f.make_supplier(self.db)
        coincide_ambos = f.make_inventory_item(self.db, name="Harina de trigo", code="HAR-001")
        solo_coincide_busqueda = f.make_inventory_item(self.db, name="Harina integral", code="HAR-002")
        solo_coincide_proveedor = f.make_inventory_item(self.db, name="Azúcar", code="AZU-001")
        f.make_supplier_item(self.db, supplier=proveedor, inventory_item=coincide_ambos, active=True)
        f.make_supplier_item(self.db, supplier=proveedor, inventory_item=solo_coincide_proveedor, active=True)
        resultados = self._search(search="harina", supplier_id=proveedor.id)
        self.assertEqual([i.id for i in resultados], [coincide_ambos.id])
        self.assertNotIn(solo_coincide_busqueda.id, [i.id for i in resultados])

    def test_search_supplier_id_type_y_active_simultaneos_cumplen_los_cuatro_criterios(self):
        proveedor = f.make_supplier(self.db)
        cumple_todo = f.make_inventory_item(
            self.db, name="Harina de trigo", code="HAR-001", type="raw_material", active=True,
        )
        falla_por_type = f.make_inventory_item(
            self.db, name="Harina preparada", code="HAR-003", type="packaged", active=True,
        )
        falla_por_active = f.make_inventory_item(
            self.db, name="Harina vieja", code="HAR-004", type="raw_material", active=False,
        )
        f.make_supplier_item(self.db, supplier=proveedor, inventory_item=cumple_todo, active=True)
        f.make_supplier_item(self.db, supplier=proveedor, inventory_item=falla_por_type, active=True)
        f.make_supplier_item(self.db, supplier=proveedor, inventory_item=falla_por_active, active=True)
        resultados = self._search(
            search="harina", supplier_id=proveedor.id, type_="raw_material", active=True,
        )
        self.assertEqual([i.id for i in resultados], [cumple_todo.id])


if __name__ == "__main__":
    unittest.main()
