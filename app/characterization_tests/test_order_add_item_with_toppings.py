"""Spec 129: `add_item_to_order` (anexar un producto a un pedido ya creado, vía
"+ Agregar producto") acepta `OrderItemIn.toppings` y las resuelve en la MISMA
transacción -- mismo cambio que `test_order_create_with_toppings.py` hace para
`create_order`, aplicado al otro punto donde el cliente arma una línea con
adiciones (`PosTerminalStore.saveOrder()`).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_order_add_item_with_toppings -v
"""
from decimal import Decimal
import unittest
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import select

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import consolidation
from app.api.v1.orders.schemas import OrderItemIn
from app.api.v1.catalog.schemas import ToppingSelectionIn
from app.models.inventory_movement import InventoryMovement
from app.models.product_topping import ProductTopping

PRECIO = Decimal("10000")
PRECIO_GOMITAS = Decimal("2000")


class TestAddItemToOrderConToppings(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.table = fx.make_dining_table(self.db)
        self.ts = fx.make_table_session(self.db, table=self.table)
        self.order = fx.make_customer_order(self.db, self.ts, dining_table_id=self.table.id)
        # tracks_inventory=False: lo que se prueba aquí es la adición, no el descuento del
        # producto principal (mismo criterio que `test_order_item_topping_standalone.py`).
        base_product = fx.make_product(self.db, tracks_inventory=False)
        self.variant = fx.make_variant(self.db, product=base_product, price=PRECIO)
        self.gomitas_product = fx.make_product(
            self.db, category=self.variant.product.category, name="Gomitas", is_topping=True,
        )
        self.gomitas_variant = fx.make_variant(
            self.db, product=self.gomitas_product, price=PRECIO_GOMITAS,
        )
        self.db.add(ProductTopping(
            product_id=self.variant.product_id, topping_product_id=self.gomitas_product.id,
        ))
        self.user = fx.make_user_double()
        self.tenant = fx.make_tenant_double()
        self.db.commit()

    def _agregar(self, *, quantity_gomitas=1, con_inventario=False):
        data = OrderItemIn(
            product_variant_id=self.variant.id,
            quantity=1,
            toppings=[ToppingSelectionIn(
                topping_product_id=self.gomitas_product.id, quantity=quantity_gomitas,
            )],
        )
        with mock.patch.object(consolidation, "has_module_access", return_value=con_inventario):
            return consolidation.add_item_to_order(self.db, self.order.id, data, self.user, tenant=self.tenant)

    def test_la_adicion_se_guarda_en_la_misma_transaccion_sin_llamada_aparte(self):
        order = self._agregar(quantity_gomitas=2)

        linea = next(i for i in order.items if i.product_variant_id == self.variant.id)
        [fila] = linea.toppings
        self.assertEqual(fila.quantity, 2)
        self.assertEqual(Decimal(fila.unit_price), PRECIO_GOMITAS)
        self.assertEqual(linea.addons_total, PRECIO_GOMITAS * 2)

    def test_con_modulo_inventario_descuenta_stock_de_la_adicion(self):
        insumo = fx.make_inventory_item(self.db, current_stock=Decimal("10"))
        fx.make_recipe_item(self.db, self.gomitas_variant, insumo, quantity=Decimal("1"))
        self.db.commit()

        self._agregar(quantity_gomitas=3, con_inventario=True)

        self.db.refresh(insumo)
        self.assertEqual(Decimal(insumo.current_stock), Decimal("7"))

    def test_agotado_de_la_adicion_revienta_la_transaccion_sin_crear_la_linea(self):
        insumo = fx.make_inventory_item(self.db, current_stock=Decimal("1"))
        fx.make_recipe_item(self.db, self.gomitas_variant, insumo, quantity=Decimal("1"))
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            self._agregar(quantity_gomitas=5, con_inventario=True)
        self.assertEqual(ctx.exception.status_code, 409)

        self.db.refresh(self.order)
        self.assertEqual(self.order.items, [])


if __name__ == "__main__":
    unittest.main()
