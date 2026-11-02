"""Spec 148 (disponibilidad en tiempo real) — 409 unificado por producto
agotado (`sold_out_conflict`/`ensure_orderable`, `app/catalog_engine/
availability.py`). Research.md D6/D7; anomalía A-153
(`specs/000-reconocimiento/registro-de-anomalias.md`).

Este módulo prueba el CONSTRUCTOR directamente (forma del `detail`, orden,
textos, `agotados[]`, claves heredadas) y `ensure_orderable` sobre un
catálogo mínimo. Las pruebas por punto de entrada HTTP (`POST /orders`, etc.)
viven en los módulos de cada router (T074/T075/T076).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_sold_out_conflict -v
"""
from __future__ import annotations

import unittest
from decimal import Decimal
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import select

from app.characterization_tests import cart_fixtures as fx
from app.catalog_engine.availability import (
    OrderableLine, SoldOutItem, ensure_orderable, sold_out_conflict,
)
from app.catalog_engine.core import ChosenOption
from app.models.order_item import OrderItemTopping


class SoldOutConflictShapeTests(unittest.TestCase):
    """Forma exacta del `detail` que arma `sold_out_conflict` (contracts/
    sold-out-conflict.md)."""

    def test_un_solo_elemento_singular(self):
        exc = sold_out_conflict([
            SoldOutItem(id="v1", tipo="presentacion", nombre="Granizado de mora · 16 oz",
                        producto_id="p1", motivo="inventario"),
        ])
        self.assertEqual(exc.status_code, 409)
        self.assertEqual(
            exc.detail["error"],
            "Se agotó: Granizado de mora · 16 oz. Quítalo para continuar",
        )
        self.assertEqual(exc.detail["productos_agotados"], ["Granizado de mora · 16 oz"])
        self.assertEqual(len(exc.detail["agotados"]), 1)
        item = exc.detail["agotados"][0]
        self.assertEqual(item["id"], "v1")
        self.assertEqual(item["tipo"], "presentacion")
        self.assertEqual(item["nombre"], "Granizado de mora · 16 oz")
        self.assertEqual(item["producto_id"], "p1")
        self.assertEqual(item["motivo"], "inventario")

    def test_varios_elementos_plural_y_orden_estable(self):
        exc = sold_out_conflict([
            SoldOutItem(id="o1", tipo="opcion", nombre="Granizado de mora · Mora",
                        producto_id="p1", motivo="inventario"),
            SoldOutItem(id="p2", tipo="producto", nombre="Paleta de coco",
                        producto_id="p2", motivo="manual"),
            SoldOutItem(id="v1", tipo="presentacion", nombre="Granizado de mora · 16 oz",
                        producto_id="p1", motivo="inventario"),
        ])
        self.assertEqual(
            exc.detail["error"],
            "Se agotaron: Paleta de coco, Granizado de mora · 16 oz, "
            "Granizado de mora · Mora. Quítalos para continuar",
        )
        # orden estable: producto -> presentacion -> opcion, luego por nombre
        self.assertEqual(
            [it["tipo"] for it in exc.detail["agotados"]],
            ["producto", "presentacion", "opcion"],
        )

    def test_sin_duplicados_por_id_y_tipo(self):
        exc = sold_out_conflict([
            SoldOutItem(id="p1", tipo="producto", nombre="Paleta de coco",
                        producto_id="p1", motivo="manual"),
            SoldOutItem(id="p1", tipo="producto", nombre="Paleta de coco",
                        producto_id="p1", motivo="manual"),
        ])
        self.assertEqual(len(exc.detail["agotados"]), 1)

    def test_claves_heredadas_se_conservan(self):
        exc = sold_out_conflict(
            [SoldOutItem(id="v1", tipo="presentacion", nombre="X · 16 oz",
                         producto_id="p1", motivo="inventario")],
            legacy={"producto": "X", "insumo": "Leche", "disponible": "2", "requerido": "5",
                    "contexto": "carrito"},
        )
        self.assertEqual(exc.detail["producto"], "X")
        self.assertEqual(exc.detail["insumo"], "Leche")
        self.assertEqual(exc.detail["disponible"], "2")
        self.assertEqual(exc.detail["requerido"], "5")
        self.assertEqual(exc.detail["contexto"], "carrito")
        # y las nuevas siguen presentes (aditivo)
        self.assertIn("agotados", exc.detail)


class EnsureOrderableTests(unittest.TestCase):
    """`ensure_orderable` sobre un catálogo mínimo — los tres `motivo`s."""

    def _seed_simple(self, db, *, stock=Decimal("5"), recipe_qty=Decimal("1")):
        category = fx.make_category(db)
        product = fx.make_product(db, category=category, name="Granizado de mora")
        variant = fx.make_variant(db, product=product, price=Decimal("10000"), name="16 oz")
        item = fx.make_inventory_item(db, current_stock=stock)
        fx.make_recipe_item(db, variant, item, quantity=recipe_qty)
        db.commit()
        return product, variant, item

    def test_sin_elementos_agotados_no_lanza(self):
        db = fx.new_session()
        _, variant, _ = self._seed_simple(db, stock=Decimal("5"), recipe_qty=Decimal("1"))
        ensure_orderable(db, [OrderableLine(variant_id=variant.id, quantity=1)])  # no debe lanzar

    def test_motivo_manual(self):
        db = fx.new_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category, name="Paleta de coco", available=False)
        variant = fx.make_variant(db, product=product, price=Decimal("5000"))
        db.commit()
        with self.assertRaises(Exception) as ctx:
            ensure_orderable(db, [OrderableLine(variant_id=variant.id, quantity=1)])
        exc = ctx.exception
        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail["agotados"][0]["motivo"], "manual")
        self.assertEqual(exc.detail["agotados"][0]["nombre"], "Paleta de coco")

    def test_motivo_inventario_por_receta(self):
        db = fx.new_session()
        _, variant, item = self._seed_simple(db, stock=Decimal("0"), recipe_qty=Decimal("1"))
        with self.assertRaises(Exception) as ctx:
            ensure_orderable(db, [OrderableLine(variant_id=variant.id, quantity=1)],
                              extra_context="carrito")
        exc = ctx.exception
        self.assertEqual(exc.status_code, 409)
        agot = exc.detail["agotados"][0]
        self.assertEqual(agot["motivo"], "inventario")
        self.assertEqual(agot["tipo"], "presentacion")
        self.assertIn("Granizado de mora", agot["nombre"])
        self.assertEqual(exc.detail.get("insumo"), item.name)
        self.assertIn("disponible", exc.detail)
        self.assertIn("requerido", exc.detail)
        self.assertEqual(exc.detail.get("contexto"), "carrito")

    def test_motivo_cantidad_cinco_pedidos_stock_para_tres(self):
        db = fx.new_session()
        _, variant, item = self._seed_simple(db, stock=Decimal("3"), recipe_qty=Decimal("1"))
        with self.assertRaises(Exception) as ctx:
            ensure_orderable(db, [OrderableLine(variant_id=variant.id, quantity=5)])
        exc = ctx.exception
        self.assertEqual(exc.status_code, 409)
        agot = exc.detail["agotados"][0]
        self.assertEqual(agot["motivo"], "cantidad")

    def test_opcion_agotada_se_nombra_junto_con_su_producto(self):
        db = fx.new_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category, name="Granizado")
        variant = fx.make_variant(db, product=product, price=Decimal("10000"), name="16 oz")
        group = fx.make_option_group(db, min_select=0, selection_mode="conteo")
        item = fx.make_inventory_item(db, current_stock=Decimal("0"))
        option = fx.make_option(db, group=group, name="Mora", inventory_item_id=item.id,
                                 item_quantity=Decimal("1"))
        fx.link_variant_group(db, variant, group, min_select=0, max_select=1)
        db.commit()
        chosen = [ChosenOption(option, 1)]
        with self.assertRaises(Exception) as ctx:
            ensure_orderable(db, [OrderableLine(variant_id=variant.id, quantity=1, options=chosen)])
        exc = ctx.exception
        agot = exc.detail["agotados"][0]
        self.assertEqual(agot["tipo"], "opcion")
        self.assertEqual(agot["nombre"], "Granizado · Mora")
        self.assertEqual(agot["motivo"], "inventario")

    def test_toppings_inventory_false_omite_solo_el_chequeo_de_stock_de_adiciones(self):
        db = fx.new_session()
        _, variant, _ = self._seed_simple(db, stock=Decimal("5"), recipe_qty=Decimal("1"))
        category2 = fx.make_category(db)
        topping_product = fx.make_product(db, category=category2, name="Topping agotado",
                                            available=False)
        db.commit()
        # Con toppings_inventory=False la marca manual de la adición NO debería
        # omitirse (D19: solo se omite el chequeo de STOCK, la marca manual sigue
        # aplicando) -- se espera que SÍ rechace.
        with self.assertRaises(Exception) as ctx:
            ensure_orderable(db, [OrderableLine(
                variant_id=variant.id, quantity=1, topping_product_ids=[topping_product.id],
            )], toppings_inventory=False)
        exc = ctx.exception
        self.assertEqual(exc.detail["agotados"][0]["motivo"], "manual")

    def test_toppings_inventory_false_omite_agotado_por_receta_de_la_adicion(self):
        db = fx.new_session()
        _, variant, _ = self._seed_simple(db, stock=Decimal("5"), recipe_qty=Decimal("1"))
        category2 = fx.make_category(db)
        topping_product = fx.make_product(db, category=category2, name="Topping sin stock")
        topping_variant = fx.make_variant(db, product=topping_product, price=Decimal("1000"))
        topping_item = fx.make_inventory_item(db, current_stock=Decimal("0"))
        fx.make_recipe_item(db, topping_variant, topping_item, quantity=Decimal("1"))
        db.commit()
        # toppings_inventory=False: no se mira el stock de la adición en absoluto.
        ensure_orderable(db, [OrderableLine(
            variant_id=variant.id, quantity=1, topping_product_ids=[topping_product.id],
        )], toppings_inventory=False)  # no debe lanzar


class EntryPointConflictTests(unittest.TestCase):
    """T074/T075: un test por punto de entrada de pedidos y del carrito QR
    (además de los que ya ejercitan el 409 unificado en `test_orders_sold_out.py`,
    `test_order_add_item_with_toppings.py`, `test_orders_additions.py`,
    `test_order_item_topping_attach.py`, `test_cart_sold_out.py`): status 409,
    `agotados[]` con las claves correctas, cero filas nuevas, dos agotados a la
    vez y, para adiciones/toppings, que el guardia del módulo `inventario`
    (D19/A-153) se preserva exactamente -- sin el módulo, un insumo en 0 NO
    rechaza."""

    def test_add_item_to_order_toppings_sin_modulo_no_rechaza_con_modulo_si(self):
        from app.api.v1.orders import consolidation
        from app.api.v1.orders.schemas import OrderItemIn
        from app.api.v1.catalog.schemas import ToppingSelectionIn
        from app.models.product_topping import ProductTopping
        from app.characterization_tests import orders_fixtures as ofx

        db = ofx.new_session()
        table = ofx.make_dining_table(db)
        ts = ofx.make_table_session(db, table=table)
        order = ofx.make_customer_order(db, ts, dining_table_id=table.id)
        base_product = ofx.make_product(db, tracks_inventory=False)
        variant = ofx.make_variant(db, product=base_product, price=Decimal("10000"))
        topping_product = ofx.make_product(
            db, category=base_product.category, name="Gomitas", is_topping=True,
        )
        topping_variant = ofx.make_variant(db, product=topping_product, price=Decimal("2000"))
        insumo = ofx.make_inventory_item(db, current_stock=Decimal("0"))
        ofx.make_recipe_item(db, topping_variant, insumo, quantity=Decimal("1"))
        db.add(ProductTopping(product_id=base_product.id, topping_product_id=topping_product.id))
        user = ofx.make_user_double()
        tenant = ofx.make_tenant_double()
        db.commit()

        data = OrderItemIn(
            product_variant_id=variant.id, quantity=1,
            toppings=[ToppingSelectionIn(topping_product_id=topping_product.id, quantity=1)],
        )
        with mock.patch.object(consolidation, "has_module_access", return_value=False):
            order_sin_modulo = consolidation.add_item_to_order(db, order.id, data, user, tenant=tenant)
        self.assertEqual(len(order_sin_modulo.items), 1)

        with mock.patch.object(consolidation, "has_module_access", return_value=True):
            with self.assertRaises(HTTPException) as ctx:
                consolidation.add_item_to_order(db, order.id, data, user, tenant=tenant)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["agotados"][0]["motivo"], "inventario")

    def test_toppings_add_topping_to_order_item_sin_modulo_no_rechaza_con_modulo_si(self):
        from app.api.v1.orders import toppings as toppings_module
        from app.api.v1.catalog.schemas import ToppingSelectionIn
        from app.models.product_topping import ProductTopping
        from app.characterization_tests import orders_fixtures as ofx

        db = ofx.new_session()
        table = ofx.make_dining_table(db)
        ts = ofx.make_table_session(db, table=table)
        order = ofx.make_customer_order(db, ts, dining_table_id=table.id)
        variant = ofx.make_variant(db, price=Decimal("8000"))
        topping_product = ofx.make_product(
            db, category=variant.product.category, name="Gomitas", is_topping=True,
        )
        topping_variant = ofx.make_variant(db, product=topping_product, price=Decimal("2000"))
        insumo = ofx.make_inventory_item(db, current_stock=Decimal("0"))
        ofx.make_recipe_item(db, topping_variant, insumo, quantity=Decimal("1"))
        db.add(ProductTopping(product_id=variant.product_id, topping_product_id=topping_product.id))
        user = ofx.make_user_double()
        tenant = ofx.make_tenant_double()
        db.commit()
        item = ofx.make_order_item(db, order, variant, quantity=1, unit_price=Decimal("8000"))
        db.commit()

        body = ToppingSelectionIn(topping_product_id=topping_product.id, quantity=1)
        with mock.patch.object(toppings_module, "has_module_access", return_value=False):
            toppings_module.add_topping_to_order_item(db, item.id, body, user, tenant)
        self.db_items_count = len(
            db.execute(select(OrderItemTopping).where(OrderItemTopping.order_item_id == item.id)).scalars().all()
        )
        self.assertEqual(self.db_items_count, 1)

        item2 = ofx.make_order_item(db, order, variant, quantity=1, unit_price=Decimal("8000"))
        db.commit()
        with mock.patch.object(toppings_module, "has_module_access", return_value=True):
            with self.assertRaises(HTTPException) as ctx:
                toppings_module.add_topping_to_order_item(db, item2.id, body, user, tenant)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["agotados"][0]["motivo"], "inventario")

    def test_create_order_dos_agotados_no_crea_ninguna_fila(self):
        from app.api.v1.orders import service as orders_service
        from app.api.v1.orders.schemas import OrderChannel, OrderCreate, OrderItemIn
        from app.models.customer_order import CustomerOrder
        from app.models.order_item import OrderItem
        from app.models.inventory_movement import InventoryMovement
        from app.characterization_tests import orders_fixtures as ofx
        from uuid import uuid4

        db = ofx.new_session()
        cat = ofx.make_category(db)
        p1 = ofx.make_product(db, category=cat, available=False)
        v1 = ofx.make_variant(db, product=p1, price=Decimal("10000"))
        p2 = ofx.make_product(db, category=cat, available=False)
        v2 = ofx.make_variant(db, product=p2, price=Decimal("10000"))
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS, customer_name="Cliente",
            items=[
                OrderItemIn(product_variant_id=v1.id, quantity=1),
                OrderItemIn(product_variant_id=v2.id, quantity=1),
            ],
        )
        with self.assertRaises(HTTPException) as ctx:
            orders_service.create_order(db, data, uuid4())
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(len(ctx.exception.detail["agotados"]), 2)
        self.assertEqual(db.execute(select(CustomerOrder)).scalars().all(), [])
        self.assertEqual(db.execute(select(OrderItem)).scalars().all(), [])
        self.assertEqual(db.execute(select(InventoryMovement)).scalars().all(), [])

    def test_submit_cart_dos_agotados_a_la_vez_carrito_intacto(self):
        from app.api.v1.cart import service as cart_service
        from app.models.customer_order import CustomerOrder
        from app.models.cart_item import CartItem

        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        participant = fx.make_participant(db, ts)
        cat = fx.make_category(db)
        p1 = fx.make_product(db, category=cat, available=False)
        v1 = fx.make_variant(db, product=p1, price=Decimal("10000"))
        p2 = fx.make_product(db, category=cat, available=False)
        v2 = fx.make_variant(db, product=p2, price=Decimal("10000"))
        cart = fx.make_cart(db, participant=participant)
        fx.make_cart_item(db, cart, v1, quantity=1)
        fx.make_cart_item(db, cart, v2, quantity=1)
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            cart_service.submit_cart(db, participant, efectivo.id)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(len(ctx.exception.detail["agotados"]), 2)
        self.assertEqual(db.execute(select(CustomerOrder)).scalars().all(), [])
        remaining = db.execute(select(CartItem).where(CartItem.cart_id == cart.id)).scalars().all()
        self.assertEqual(len(remaining), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
