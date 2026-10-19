"""Spec 129: `create_order` acepta `OrderItemIn.toppings` y las resuelve en la MISMA
transacción que crea el pedido (`OrderItemTopping` + descuento de inventario, si el
tenant tiene el módulo activo) -- antes el cliente las adjuntaba con una llamada
aparte por adición, después de crear el pedido (`POST /orders/items/{item_id}/
toppings`, un N+1 de escrituras evitable porque la selección ya se conoce al armar
el pedido).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_order_create_with_toppings -v
"""
from decimal import Decimal
import unittest
from unittest import mock
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import service
from app.api.v1.orders.schemas import OrderChannel, OrderCreate, OrderItemIn, OrderType
from app.api.v1.catalog.schemas import ToppingSelectionIn
from app.models.inventory_movement import InventoryMovement
from app.models.product_topping import ProductTopping

PRECIO = Decimal("10000")
PRECIO_GOMITAS = Decimal("2000")


class TestCreateOrderConToppings(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.category = fx.make_category(self.db)
        # tracks_inventory=False: lo que se prueba aquí es la adición, no el descuento del
        # producto principal (mismo criterio que `test_order_item_topping_standalone.py`).
        self.product = fx.make_product(self.db, category=self.category, tracks_inventory=False)
        self.variant = fx.make_variant(self.db, product=self.product, price=PRECIO)
        self.gomitas_product = fx.make_product(
            self.db, category=self.category, name="Gomitas", is_topping=True,
        )
        self.gomitas_variant = fx.make_variant(
            self.db, product=self.gomitas_product, price=PRECIO_GOMITAS,
        )
        self.db.add(ProductTopping(
            product_id=self.variant.product_id, topping_product_id=self.gomitas_product.id,
        ))
        self.tenant = fx.make_tenant_double()
        self.db.commit()

    def _crear(self, *, quantity_gomitas=1, con_inventario=False, hold_for_payment=False):
        data = OrderCreate(
            channel=OrderChannel.POS,
            order_type=OrderType.DINE_IN,
            customer_name="Cliente de prueba",
            hold_for_payment=hold_for_payment,
            items=[OrderItemIn(
                product_variant_id=self.variant.id,
                quantity=1,
                toppings=[ToppingSelectionIn(
                    topping_product_id=self.gomitas_product.id, quantity=quantity_gomitas,
                )],
            )],
        )
        with mock.patch.object(service, "has_module_access", return_value=con_inventario):
            return service.create_order(self.db, data, uuid4(), tenant=self.tenant)

    def test_la_adicion_se_guarda_en_la_misma_transaccion_que_crea_el_pedido(self):
        order = self._crear(quantity_gomitas=2)

        [item] = order.items
        [fila] = item.toppings
        self.assertEqual(fila.quantity, 2)
        self.assertEqual(Decimal(fila.unit_price), PRECIO_GOMITAS)
        self.assertEqual(item.addons_total, PRECIO_GOMITAS * 2)
        self.assertEqual(item.line_total, PRECIO + PRECIO_GOMITAS * 2)

    def test_sin_tenant_con_modulo_inventario_no_descuenta_stock_de_la_adicion(self):
        order = self._crear(con_inventario=False)
        movimientos = self.db.execute(
            select(InventoryMovement).where(InventoryMovement.reference_id == order.id)
        ).scalars().all()
        self.assertEqual(movimientos, [])

    def test_con_modulo_inventario_descuenta_stock_de_la_adicion_al_crear(self):
        insumo = fx.make_inventory_item(self.db, current_stock=Decimal("10"))
        fx.make_recipe_item(self.db, self.gomitas_variant, insumo, quantity=Decimal("1"))
        self.db.commit()

        self._crear(quantity_gomitas=3, con_inventario=True)

        self.db.refresh(insumo)
        self.assertEqual(Decimal(insumo.current_stock), Decimal("7"))

    def test_hold_for_payment_difiere_el_descuento_pero_guarda_la_adicion(self):
        insumo = fx.make_inventory_item(self.db, current_stock=Decimal("10"))
        fx.make_recipe_item(self.db, self.gomitas_variant, insumo, quantity=Decimal("1"))
        self.db.commit()

        order = self._crear(quantity_gomitas=3, con_inventario=True, hold_for_payment=True)

        [item] = order.items
        self.assertEqual(len(item.toppings), 1)
        self.db.refresh(insumo)
        self.assertEqual(Decimal(insumo.current_stock), Decimal("10"))

    def test_rechaza_una_adicion_no_asociada_al_producto(self):
        otra = fx.make_product(self.db, category=self.category, name="Crema", is_topping=True)
        fx.make_variant(self.db, product=otra, price=Decimal("1500"))
        self.db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            order_type=OrderType.DINE_IN,
            customer_name="Cliente de prueba",
            items=[OrderItemIn(
                product_variant_id=self.variant.id,
                quantity=1,
                toppings=[ToppingSelectionIn(topping_product_id=otra.id, quantity=1)],
            )],
        )
        with mock.patch.object(service, "has_module_access", return_value=False):
            with self.assertRaises(HTTPException) as ctx:
                service.create_order(self.db, data, uuid4(), tenant=self.tenant)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_agotado_de_la_adicion_revienta_toda_la_transaccion(self):
        insumo = fx.make_inventory_item(self.db, current_stock=Decimal("1"))
        fx.make_recipe_item(self.db, self.gomitas_variant, insumo, quantity=Decimal("1"))
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            self._crear(quantity_gomitas=5, con_inventario=True)
        self.assertEqual(ctx.exception.status_code, 409)

        # Transacción entera revertida: tampoco se creó el pedido ni su línea principal.
        huerfanos = self.db.execute(
            select(InventoryMovement).where(InventoryMovement.reference_type == "order")
        ).scalars().all()
        self.assertEqual(huerfanos, [])


if __name__ == "__main__":
    unittest.main()
