"""Tests de las tres operaciones nuevas de división/transferencia de ítems
entre órdenes (spec 164): `split_order`, `move_item`,
`split_item_to_new_order` (`app/api/v1/orders/item_transfer.py`). Cubre los
escenarios 1-3 de `specs/164-division-automatica-cuenta/quickstart.md` contra
el contrato `contracts/orders-item-transfer.md`.

No son characterization tests de comportamiento PREEXISTENTE (Principio IV:
esta spec introduce comportamiento nuevo) — siguen el mismo patrón de
infraestructura (`orders_fixtures.py`, SQLite en memoria) que
`test_orders_tables_advanced.py`.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_orders_item_transfer -v
"""
from decimal import Decimal
import unittest

from fastapi import HTTPException
from sqlalchemy import select

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import item_transfer
from app.models.customer_order import CustomerOrder

PRECIO = Decimal("10000")


class ItemTransferTestCase(unittest.TestCase):
    def _seed_table(self, db=None):
        if db is None:
            db = fx.new_session()
        table = fx.make_dining_table(db, status="ocupada")
        ts = fx.make_table_session(db, table=table)
        return db, table, ts

    def _variant(self, db, price=PRECIO):
        return fx.make_variant(db, price=price)


# --------------------------------------------------------------------------
# User Story 1 — split_order
# --------------------------------------------------------------------------
class TestSplitOrder(ItemTransferTestCase):
    def test_split_3_lineas_distintas_crea_3_ordenes_y_elimina_la_original(self):
        """Acceptance Scenario 1 / quickstart Escenario 1, pasos 1-3."""
        db, table, ts = self._seed_table()
        v1, v2, v3 = self._variant(db), self._variant(db), self._variant(db)
        order = fx.make_order_with_items(db, ts, [v1, v2, v3], quantities=[1, 1, 1])
        db.commit()
        order_id = order.id

        result = item_transfer.split_order(db, order_id)

        self.assertEqual(result["original_order_id"], order_id)
        self.assertEqual(len(result["created_orders"]), 3)
        self.assertIsNone(
            db.execute(select(CustomerOrder.id).where(CustomerOrder.id == order_id)).scalar()
        )
        for created in result["created_orders"]:
            new_order = db.get(CustomerOrder, created["order_id"])
            self.assertEqual(len(new_order.items), 1)
            self.assertEqual(new_order.table_session_id, ts.id)
            self.assertEqual(new_order.dining_table_id, table.id)

    def test_split_orden_de_1_sola_linea_es_no_op(self):
        """Acceptance Scenario 2 / quickstart Escenario 1, paso 4."""
        db, table, ts = self._seed_table()
        order = fx.make_order_with_items(db, ts, self._variant(db), quantities=[2])
        db.commit()
        order_id = order.id

        result = item_transfer.split_order(db, order_id)

        self.assertEqual(result["created_orders"], [])
        kept = db.get(CustomerOrder, order_id)
        self.assertIsNotNone(kept)
        self.assertEqual(len(kept.items), 1)

    def test_split_conserva_cantidad_mayor_a_1_sin_fraccionar(self):
        """Acceptance Scenario 3 / quickstart Escenario 1, paso 5: una línea
        con `quantity=2` conserva esa cantidad en su orden nueva, sin
        fraccionarse por unidad."""
        db, table, ts = self._seed_table()
        v1, v2 = self._variant(db), self._variant(db)
        order = fx.make_order_with_items(db, ts, [v1, v2], quantities=[2, 1])
        db.commit()

        result = item_transfer.split_order(db, order.id)

        created_2 = next(c for c in result["created_orders"] if c["item"]["quantity"] == 2)
        new_order = db.get(CustomerOrder, created_2["order_id"])
        self.assertEqual(len(new_order.items), 1)
        self.assertEqual(new_order.items[0].quantity, 2)

    def test_split_de_linea_ya_entregada_crea_orden_ya_entregada(self):
        """Bugfix (reportado tras implementar la feature): una orden nueva
        creada a partir de una línea YA `entregado` no puede quedar en
        `status='pendiente'` para siempre -- sin ningún ítem `pendiente`, la
        terminal nunca vuelve a mostrar "Entregar" para esa orden, y eso
        bloquea `release_table` (bloquea mientras exista una orden
        `'pendiente'`/`'confirmado'`, sin importar el pago) de forma
        permanente."""
        db, table, ts = self._seed_table()
        v1, v2 = self._variant(db), self._variant(db)
        order = fx.make_order_with_items(
            db, ts, [v1, v2], quantities=[1, 1], estado_cocina="entregado",
        )
        db.commit()
        order_id = order.id

        result = item_transfer.split_order(db, order_id)

        for created in result["created_orders"]:
            new_order = db.get(CustomerOrder, created["order_id"])
            self.assertEqual(new_order.status, "entregada")

    def test_split_403_con_pago_registrado_y_orden_original_intacta(self):
        """FR-008 / quickstart Escenario 1, paso 6."""
        db, table, ts = self._seed_table()
        v1, v2 = self._variant(db), self._variant(db)
        order = fx.make_order_with_items(db, ts, [v1, v2], quantities=[1, 1])
        order.payment_status = "PAID"
        db.commit()
        order_id = order.id

        with self.assertRaises(HTTPException) as ctx:
            item_transfer.split_order(db, order_id)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(len(db.get(CustomerOrder, order_id).items), 2)


# --------------------------------------------------------------------------
# User Story 2 — move_item
# --------------------------------------------------------------------------
class TestMoveItem(ItemTransferTestCase):
    def _two_orders(self, db=None):
        db, table, ts = self._seed_table(db)
        order_a = fx.make_order_with_items(db, ts, self._variant(db), quantities=[1])
        order_b = fx.make_order_with_items(db, ts, self._variant(db), quantities=[1])
        db.commit()
        return db, table, ts, order_a, order_b

    def test_mover_item_completo_recalcula_ambos_totales(self):
        """Acceptance Scenario 1 / quickstart Escenario 2, pasos 1-3."""
        db, table, ts, order_a, order_b = self._two_orders()
        item_id, order_a_id, order_b_id = order_a.items[0].id, order_a.id, order_b.id

        result = item_transfer.move_item(db, order_a_id, item_id, order_b_id)

        self.assertTrue(result["source_order"]["deleted"])
        self.assertEqual(result["source_order"]["total"], Decimal("0"))
        self.assertIsNone(
            db.execute(select(CustomerOrder.id).where(CustomerOrder.id == order_a_id)).scalar()
        )
        target = db.get(CustomerOrder, order_b_id)
        self.assertEqual(len(target.items), 2)
        self.assertEqual(result["target_order"]["total"], Decimal("20000.00"))

    def test_mover_conserva_estado_cocina_en_destino(self):
        """Acceptance Scenario 2 / quickstart Escenario 2, paso 4."""
        db, table, ts, order_a, order_b = self._two_orders()
        item = order_a.items[0]
        item.estado_cocina = "entregado"
        db.commit()
        variant_id = item.product_variant_id
        item_id, order_a_id, order_b_id = item.id, order_a.id, order_b.id

        result = item_transfer.move_item(db, order_a_id, item_id, order_b_id)

        target = db.get(CustomerOrder, result["target_order"]["order_id"])
        moved = next(it for it in target.items if it.product_variant_id == variant_id)
        self.assertEqual(moved.estado_cocina, "entregado")

    def test_mover_el_unico_item_restante_elimina_la_orden_origen(self):
        """Acceptance Scenario 3 / quickstart Escenario 2, paso 8."""
        db, table, ts, order_a, order_b = self._two_orders()
        item_id, order_a_id, order_b_id = order_a.items[0].id, order_a.id, order_b.id

        result = item_transfer.move_item(db, order_a_id, item_id, order_b_id)

        self.assertTrue(result["source_order"]["deleted"])
        self.assertIsNone(
            db.execute(select(CustomerOrder.id).where(CustomerOrder.id == order_a_id)).scalar()
        )

    def test_mover_cantidad_parcial_divide_la_linea(self):
        """Acceptance Scenario 4 / quickstart Escenario 2, paso 5."""
        db, table, ts = self._seed_table()
        v = self._variant(db)
        order_a = fx.make_order_with_items(db, ts, v, quantities=[2])
        order_b = fx.make_order_with_items(db, ts, self._variant(db), quantities=[1])
        db.commit()
        item = order_a.items[0]
        item_id, order_a_id, order_b_id = item.id, order_a.id, order_b.id
        variant_id = v.id

        result = item_transfer.move_item(db, order_a_id, item_id, order_b_id, quantity=1)

        self.assertFalse(result["source_order"]["deleted"])
        source = db.get(CustomerOrder, order_a_id)
        self.assertEqual(len(source.items), 1)
        self.assertEqual(source.items[0].quantity, 1)
        target = db.get(CustomerOrder, order_b_id)
        moved = next(it for it in target.items if it.product_variant_id == variant_id)
        self.assertEqual(moved.quantity, 1)

    def test_mover_cantidad_total_de_una_linea_entre_varias_elimina_esa_linea(self):
        """Acceptance Scenario 5: fraccionar hasta vaciar UNA línea de una
        orden con varias no elimina la orden, solo esa línea."""
        db, table, ts = self._seed_table()
        v1, v2 = self._variant(db), self._variant(db)
        order_a = fx.make_order_with_items(db, ts, [v1, v2], quantities=[1, 1])
        order_b = fx.make_order_with_items(db, ts, self._variant(db), quantities=[1])
        db.commit()
        item = next(it for it in order_a.items if it.product_variant_id == v1.id)
        item_id, order_a_id, order_b_id = item.id, order_a.id, order_b.id

        result = item_transfer.move_item(db, order_a_id, item_id, order_b_id)

        self.assertFalse(result["source_order"]["deleted"])
        source = db.get(CustomerOrder, order_a_id)
        self.assertEqual(len(source.items), 1)
        self.assertEqual(source.items[0].product_variant_id, v2.id)

    def test_mover_fusiona_con_linea_compatible_en_destino(self):
        """Acceptance Scenario 6 / quickstart Escenario 2, paso 6."""
        db, table, ts = self._seed_table()
        v = self._variant(db)
        order_a = fx.make_order_with_items(db, ts, v, quantities=[1])
        order_b = fx.make_order_with_items(db, ts, v, quantities=[1])
        db.commit()
        item_id, order_a_id, order_b_id = order_a.items[0].id, order_a.id, order_b.id

        result = item_transfer.move_item(db, order_a_id, item_id, order_b_id)

        self.assertTrue(result["merged"])
        target = db.get(CustomerOrder, order_b_id)
        self.assertEqual(len(target.items), 1)
        self.assertEqual(target.items[0].quantity, 2)

    def test_mover_no_fusiona_si_estado_cocina_difiere(self):
        """Acceptance Scenario 7 / quickstart Escenario 2, paso 7."""
        db, table, ts = self._seed_table()
        v = self._variant(db)
        order_a = fx.make_order_with_items(db, ts, v, quantities=[1], estado_cocina="entregado")
        order_b = fx.make_order_with_items(db, ts, v, quantities=[1], estado_cocina="pendiente")
        db.commit()
        item_id, order_a_id, order_b_id = order_a.items[0].id, order_a.id, order_b.id

        result = item_transfer.move_item(db, order_a_id, item_id, order_b_id)

        self.assertFalse(result["merged"])
        target = db.get(CustomerOrder, order_b_id)
        self.assertEqual(len(target.items), 2)

    def test_mover_403_si_origen_o_destino_ya_tiene_pago(self):
        """FR-008 / quickstart Escenario 2, paso 9."""
        db, table, ts, order_a, order_b = self._two_orders()
        order_a.payment_status = "PAID"
        db.commit()
        item_id, order_a_id, order_b_id = order_a.items[0].id, order_a.id, order_b.id

        with self.assertRaises(HTTPException) as ctx:
            item_transfer.move_item(db, order_a_id, item_id, order_b_id)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(len(db.get(CustomerOrder, order_a_id).items), 1)
        self.assertEqual(len(db.get(CustomerOrder, order_b_id).items), 1)

    def test_mover_item_ya_entregado_hacia_orden_vacia_la_deja_entregada(self):
        """Bugfix: `move_item` crea una línea nueva (caso no-fusión) en el
        destino -- si el destino queda con todas sus líneas `entregado`
        (incluida la recién llegada), su `status` debe promoverse a
        `'entregada'`, no quedar atascado en `'pendiente'`."""
        db, table, ts = self._seed_table()
        order_a = fx.make_order_with_items(db, ts, self._variant(db), quantities=[1], estado_cocina="entregado")
        order_b = fx.make_order_with_items(db, ts, self._variant(db), quantities=[1], estado_cocina="entregado")
        db.commit()
        item_id, order_a_id, order_b_id = order_a.items[0].id, order_a.id, order_b.id

        item_transfer.move_item(db, order_a_id, item_id, order_b_id)

        target = db.get(CustomerOrder, order_b_id)
        self.assertEqual(target.status, "entregada")

    def test_mover_400_si_destino_es_de_otra_mesa(self):
        """FR-009 / quickstart Escenario 2, paso 10."""
        db, table, ts, order_a, order_b = self._two_orders()
        otra_table = fx.make_dining_table(db, status="ocupada")
        otra_ts = fx.make_table_session(db, table=otra_table)
        order_c = fx.make_order_with_items(db, otra_ts, self._variant(db), quantities=[1])
        db.commit()
        item_id, order_a_id, order_c_id = order_a.items[0].id, order_a.id, order_c.id

        with self.assertRaises(HTTPException) as ctx:
            item_transfer.move_item(db, order_a_id, item_id, order_c_id)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_mover_400_mismo_destino_que_origen_y_cantidad_fuera_de_rango(self):
        """Reglas del contrato: `target_order_id == order_id`, y `quantity`
        fuera de `(0, line.quantity)`."""
        db, table, ts, order_a, order_b = self._two_orders()
        item_id, order_a_id, order_b_id = order_a.items[0].id, order_a.id, order_b.id

        with self.assertRaises(HTTPException) as ctx:
            item_transfer.move_item(db, order_a_id, item_id, order_a_id)
        self.assertEqual(ctx.exception.status_code, 400)

        with self.assertRaises(HTTPException) as ctx2:
            item_transfer.move_item(db, order_a_id, item_id, order_b_id, quantity=5)
        self.assertEqual(ctx2.exception.status_code, 400)


# --------------------------------------------------------------------------
# User Story 3 — split_item_to_new_order
# --------------------------------------------------------------------------
class TestSplitItemToNewOrder(ItemTransferTestCase):
    def test_split_to_new_con_2_items_crea_orden_nueva_con_el_item_elegido(self):
        """Acceptance Scenario 1 / quickstart Escenario 3, pasos 1-3."""
        db, table, ts = self._seed_table()
        v1, v2 = self._variant(db), self._variant(db)
        order = fx.make_order_with_items(db, ts, [v1, v2], quantities=[1, 1])
        db.commit()
        item = next(it for it in order.items if it.product_variant_id == v2.id)
        item_id, order_id = item.id, order.id

        result = item_transfer.split_item_to_new_order(db, order_id, item_id)

        self.assertFalse(result["source_order"]["deleted"])
        source = db.get(CustomerOrder, order_id)
        self.assertEqual(len(source.items), 1)
        self.assertEqual(source.items[0].product_variant_id, v1.id)
        new_order = db.get(CustomerOrder, result["new_order"]["order_id"])
        self.assertEqual(len(new_order.items), 1)
        self.assertEqual(new_order.items[0].product_variant_id, v2.id)
        self.assertEqual(new_order.table_session_id, ts.id)

    def test_delete_order_if_empty_elimina_la_orden_sin_items(self):
        """SC-002/FR-007: verifica el helper de limpieza compartido
        (`_delete_order_if_empty`) que usan las 3 operaciones -- construido
        directamente para la prueba (caso límite): `split_item_to_new_order`
        nunca deja su PROPIA orden de origen vacía por diseño (el guard de
        "única línea restante + cantidad total" lo impide, ver el test de
        abajo), así que el mecanismo de limpieza en sí se verifica aparte."""
        db, table, ts = self._seed_table()
        order = fx.make_order_with_items(db, ts, self._variant(db), quantities=[1])
        db.commit()
        order_id = order.id

        order.items.remove(order.items[0])
        db.flush()
        deleted = item_transfer._delete_order_if_empty(db, order)
        db.commit()

        self.assertTrue(deleted)
        self.assertIsNone(
            db.execute(select(CustomerOrder.id).where(CustomerOrder.id == order_id)).scalar()
        )

    def test_split_to_new_parcial_divide_la_linea(self):
        """Acceptance Scenario 3 / quickstart Escenario 3, paso 5."""
        db, table, ts = self._seed_table()
        v1, v2 = self._variant(db), self._variant(db)
        order = fx.make_order_with_items(db, ts, [v1, v2], quantities=[2, 1])
        db.commit()
        item = next(it for it in order.items if it.product_variant_id == v1.id)
        item_id, order_id = item.id, order.id

        result = item_transfer.split_item_to_new_order(db, order_id, item_id, quantity=1)

        source = db.get(CustomerOrder, order_id)
        source_v1 = next(it for it in source.items if it.product_variant_id == v1.id)
        self.assertEqual(source_v1.quantity, 1)
        new_order = db.get(CustomerOrder, result["new_order"]["order_id"])
        self.assertEqual(new_order.items[0].quantity, 1)

    def test_split_to_new_400_unica_linea_cantidad_total_pero_permite_parcial(self):
        """Edge Cases / quickstart Escenario 3, pasos 6-7."""
        db, table, ts = self._seed_table()
        order = fx.make_order_with_items(db, ts, self._variant(db), quantities=[2])
        db.commit()
        item_id, order_id = order.items[0].id, order.id

        with self.assertRaises(HTTPException) as ctx:
            item_transfer.split_item_to_new_order(db, order_id, item_id, quantity=2)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(len(db.get(CustomerOrder, order_id).items), 1)
        self.assertEqual(db.get(CustomerOrder, order_id).items[0].quantity, 2)

        result = item_transfer.split_item_to_new_order(db, order_id, item_id, quantity=1)
        self.assertFalse(result["source_order"]["deleted"])
        self.assertEqual(db.get(CustomerOrder, order_id).items[0].quantity, 1)

    def test_split_to_new_403_con_pago_registrado(self):
        """FR-008."""
        db, table, ts = self._seed_table()
        v1, v2 = self._variant(db), self._variant(db)
        order = fx.make_order_with_items(db, ts, [v1, v2], quantities=[1, 1])
        order.payment_status = "PAID"
        db.commit()
        item_id, order_id = order.items[0].id, order.id

        with self.assertRaises(HTTPException) as ctx:
            item_transfer.split_item_to_new_order(db, order_id, item_id)
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
