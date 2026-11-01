"""Spec 129: `create_order` acepta `OrderItemIn.toppings` y las resuelve en la MISMA
transacción que crea el pedido (`OrderItemTopping`) -- antes el cliente las
adjuntaba con una llamada aparte por adición, después de crear el pedido (`POST
/orders/items/{item_id}/toppings`, un N+1 de escrituras evitable porque la
selección ya se conoce al armar el pedido).

**A-150 (spec 146)**: antes de esta spec, `create_order` descontaba el stock de la
adición inmediatamente al crear si el tenant tenía el módulo "inventario" activo y
`hold_for_payment` era `False` (el camino real, nunca usado por el frontend
vigente). Desde A-150, `create_order` nunca descuenta inventario (ni del producto
principal ni de sus adiciones) -- el único punto de descuento es la confirmación
(`checkout.py`, `_deduct_and_open`). **Gap conocido, fuera de alcance de esta
spec**: `_deduct_and_open` solo descuenta `OrderItem`/`OrderItemOption` (vía
`consumption.chosen_from_rows`), nunca `OrderItemTopping` -- el descuento de una
adición queda sin ningún punto de ejecución tras A-150 (ya era así para
`hold_for_payment=True`, que nunca pasó por aquí; A-150 solo lo generaliza a todo
canal). Estos tests se actualizan explícitamente citando A-150
(`registro-de-anomalias.md`), no en silencio.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_order_create_with_toppings -v
"""
from decimal import Decimal
import unittest
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

    def _crear(self, *, quantity_gomitas=1, hold_for_payment=False):
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
        return service.create_order(self.db, data, uuid4(), tenant=self.tenant)

    def test_la_adicion_se_guarda_en_la_misma_transaccion_que_crea_el_pedido(self):
        order = self._crear(quantity_gomitas=2)

        [item] = order.items
        [fila] = item.toppings
        self.assertEqual(fila.quantity, 2)
        self.assertEqual(Decimal(fila.unit_price), PRECIO_GOMITAS)
        self.assertEqual(item.addons_total, PRECIO_GOMITAS * 2)
        self.assertEqual(item.line_total, PRECIO + PRECIO_GOMITAS * 2)

    def test_la_creacion_nunca_descuenta_stock_de_la_adicion(self):
        """A-150 (spec 146): a diferencia del comportamiento anterior (descontaba
        si el tenant tenía el módulo "inventario" activo), `create_order` ya no
        descuenta inventario de ninguna adición en la creación, sin importar el
        módulo del tenant -- el descuento se diferiría a la confirmación, que hoy
        no lo implementa (gap conocido, documentado arriba)."""
        insumo = fx.make_inventory_item(self.db, current_stock=Decimal("10"))
        fx.make_recipe_item(self.db, self.gomitas_variant, insumo, quantity=Decimal("3"))
        self.db.commit()

        order = self._crear(quantity_gomitas=3)

        movimientos = self.db.execute(
            select(InventoryMovement).where(InventoryMovement.reference_id == order.id)
        ).scalars().all()
        self.assertEqual(movimientos, [])
        self.db.refresh(insumo)
        self.assertEqual(Decimal(insumo.current_stock), Decimal("10"))

    def test_hold_for_payment_ya_no_cambia_nada_sobre_la_adicion(self):
        """A-150: con todo canal naciendo 'pendiente' sin descuento, el resultado
        con `hold_for_payment=True` es idéntico al de `False` (antes del cambio,
        este test contrastaba explícitamente ambos caminos)."""
        insumo = fx.make_inventory_item(self.db, current_stock=Decimal("10"))
        fx.make_recipe_item(self.db, self.gomitas_variant, insumo, quantity=Decimal("1"))
        self.db.commit()

        order = self._crear(quantity_gomitas=3, hold_for_payment=True)

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
        with self.assertRaises(HTTPException) as ctx:
            service.create_order(self.db, data, uuid4(), tenant=self.tenant)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_un_agotado_de_la_adicion_ya_no_bloquea_la_creacion(self):
        """A-150: antes, un insumo agotado de la adición revertía TODA la
        transacción (409) porque la creación descontaba inmediatamente. Ya no hay
        ningún descuento en la creación, así que ni siquiera se evalúa la
        disponibilidad del insumo de la adición -- la orden se crea igual, en
        'pendiente'. Reemplaza la caracterización anterior (que exigía el 409)."""
        insumo = fx.make_inventory_item(self.db, current_stock=Decimal("1"))
        fx.make_recipe_item(self.db, self.gomitas_variant, insumo, quantity=Decimal("1"))
        self.db.commit()

        order = self._crear(quantity_gomitas=5)

        self.assertEqual(order.status, "pendiente")
        [item] = order.items
        self.assertEqual(len(item.toppings), 1)
        self.db.refresh(insumo)
        self.assertEqual(Decimal(insumo.current_stock), Decimal("1"))


if __name__ == "__main__":
    unittest.main()
