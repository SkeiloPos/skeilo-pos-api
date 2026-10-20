"""Spec 129 (US3, FR-009/FR-017/FR-018): agregar, editar cantidad y quitar una
adición (producto-topping) adjunta a una línea de pedido ya creada --
`app/api/v1/orders/toppings.py`, paralelo a `orders/additions.py` (spec 123),
incluido el mismo mecanismo de pago complementario (`added_after_sale`/
`complementary_sale_id`) cuando el pedido ya tiene una `Sale` emitida.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_order_item_topping_attach -v
"""
from decimal import Decimal
import unittest
from unittest import mock

from fastapi import HTTPException

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.catalog.schemas import ToppingSelectionIn, UpdateToppingQuantityIn
from app.api.v1.orders import toppings, service
from app.models.order_item import OrderItemTopping
from app.models.product_topping import ProductTopping
from app.models.sale import Sale

PRECIO = Decimal("8000")
PRECIO_GOMITAS = Decimal("2000")


class TestOrderItemToppingAttach(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.table = fx.make_dining_table(self.db)
        self.ts = fx.make_table_session(self.db, table=self.table)
        self.order = fx.make_customer_order(self.db, self.ts, dining_table_id=self.table.id)
        self.variant = fx.make_variant(self.db, price=PRECIO)
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

    def _item(self, *, quantity=1, estado_cocina="pendiente", order_status=None):
        if order_status is not None:
            self.order.status = order_status
            self.db.flush()
        return fx.make_order_item(
            self.db, self.order, self.variant, quantity=quantity,
            unit_price=PRECIO, estado_cocina=estado_cocina,
        )

    def _agregar(self, item, *, quantity=1, con_inventario=False):
        body = ToppingSelectionIn(topping_product_id=self.gomitas_product.id, quantity=quantity)
        with mock.patch.object(toppings, "has_module_access", return_value=con_inventario):
            return toppings.add_topping_to_order_item(self.db, item.id, body, self.user, self.tenant)

    # ------------------------------------------------- Historia 1 (sin pagar)

    def test_agregar_adicion_a_pedido_sin_pagar_cobra_el_precio_de_catalogo(self):
        """FR-017: el precio es siempre el de catálogo de la adición, sin importar
        a qué producto esté adjunta -- nunca un valor que el request pudiera enviar
        (`ToppingSelectionIn` ni siquiera tiene un campo de precio)."""
        item = self._item()
        order = self._agregar(item, quantity=2)

        self.db.refresh(item)
        [fila] = item.toppings
        self.assertEqual(fila.quantity, 2)
        self.assertEqual(Decimal(fila.unit_price), PRECIO_GOMITAS)
        self.assertEqual(item.addons_total, PRECIO_GOMITAS * 2)
        self.assertFalse(fila.added_after_sale)
        self.assertIsNone(fila.complementary_sale_id)
        self.assertEqual(service.order_pending_balance(self.db, order.id), Decimal("0"))

    def test_addons_total_no_se_multiplica_por_la_cantidad_del_producto_principal(self):
        """FR-007/Assumptions: se cobra una vez por línea, nunca × item.quantity
        (spec 117)."""
        item = self._item(quantity=3)
        self._agregar(item, quantity=1)

        self.db.refresh(item)
        self.assertEqual(item.addons_total, PRECIO_GOMITAS)
        self.assertEqual(item.line_total, PRECIO * 3 + PRECIO_GOMITAS)

    def test_rechaza_una_adicion_no_asociada_al_producto(self):
        otra_adicion = fx.make_product(
            self.db, category=self.variant.product.category, name="Crema", is_topping=True,
        )
        fx.make_variant(self.db, product=otra_adicion, price=Decimal("1500"))
        self.db.commit()
        item = self._item()

        body = ToppingSelectionIn(topping_product_id=otra_adicion.id, quantity=1)
        with self.assertRaises(HTTPException) as ctx:
            toppings.add_topping_to_order_item(self.db, item.id, body, self.user, self.tenant)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_rechaza_agregar_la_misma_adicion_dos_veces(self):
        item = self._item()
        self._agregar(item)

        with self.assertRaises(HTTPException) as ctx:
            self._agregar(item)
        self.assertEqual(ctx.exception.status_code, 422)

    # ------------------------------------------------ Historia 2 (ya pagado)

    def test_agregar_adicion_a_pedido_ya_pagado_genera_saldo_pendiente_sin_tocar_la_sale(self):
        sale = fx.make_sale(self.db, order=self.order, total=Decimal("10000"))
        item = self._item()
        snapshot_antes = (sale.total, sale.subtotal, sale.status)

        order = self._agregar(item)

        self.db.refresh(item)
        [fila] = item.toppings
        self.assertTrue(fila.added_after_sale)
        self.assertIsNone(fila.complementary_sale_id)
        self.assertEqual(service.order_pending_balance(self.db, order.id), PRECIO_GOMITAS)
        self.db.refresh(sale)
        self.assertEqual((sale.total, sale.subtotal, sale.status), snapshot_antes)

    # --------------------------------------------------------- Editar/quitar

    def test_editar_cantidad_ajusta_addons_total(self):
        item = self._item()
        self._agregar(item, quantity=1)

        body = UpdateToppingQuantityIn(quantity=3)
        with mock.patch.object(toppings, "has_module_access", return_value=False):
            toppings.update_topping_quantity(
                self.db, item.id, self.gomitas_product.id, body, self.user, self.tenant,
            )

        self.db.refresh(item)
        [fila] = item.toppings
        self.assertEqual(fila.quantity, 3)
        self.assertEqual(item.addons_total, PRECIO_GOMITAS * 3)

    def test_quitar_adicion_revierte_addons_total_y_borra_la_fila(self):
        item = self._item()
        self._agregar(item, quantity=2)

        with mock.patch.object(toppings, "has_module_access", return_value=False):
            toppings.remove_topping(self.db, item.id, self.gomitas_product.id, self.user, self.tenant)

        self.db.refresh(item)
        self.assertEqual(item.toppings, [])
        self.assertEqual(item.addons_total, Decimal("0"))

    def test_linea_en_estado_listo_rechaza_con_409(self):
        item = self._item(estado_cocina="listo")
        body = ToppingSelectionIn(topping_product_id=self.gomitas_product.id, quantity=1)
        with self.assertRaises(HTTPException) as ctx:
            toppings.add_topping_to_order_item(self.db, item.id, body, self.user, self.tenant)
        self.assertEqual(ctx.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
