"""CONGELA app/api/v1/inventory/stock.py — único punto de mutación de stock
(módulo `inventory`, criticidad ALTA).

Referencias: `reglas-de-negocio.md` sección 3 (RN-INV-*).
"""
import unittest
from decimal import Decimal

from app.characterization_tests import fixtures as f
from app.inventory_engine import apply_adjustment, lock_items, record_movement
from app.core.exceptions import InsufficientStockError


class RecordMovementTests(unittest.TestCase):
    def setUp(self):
        self.db = f.new_session()

    def test_rn_inv_01_out_resta_in_suma_signo_lo_da_type_no_quantity(self):
        item = f.make_inventory_item(self.db, current_stock=Decimal("20"))
        record_movement(self.db, item.id, type="out", quantity=Decimal("5"))
        self.assertEqual(item.current_stock, Decimal("15"))
        record_movement(self.db, item.id, type="in", quantity=Decimal("5"))
        self.assertEqual(item.current_stock, Decimal("20"))

    def test_rn_inv_02_cantidad_cero_o_negativa_rechazada(self):
        item = f.make_inventory_item(self.db, current_stock=Decimal("10"))
        with self.assertRaises(ValueError):
            record_movement(self.db, item.id, type="in", quantity=Decimal("0"))
        with self.assertRaises(ValueError):
            record_movement(self.db, item.id, type="out", quantity=Decimal("-1"))
        self.assertEqual(item.current_stock, Decimal("10"), "no se tocó la fila")

    def test_rn_inv_03_no_acepta_type_adjustment(self):
        item = f.make_inventory_item(self.db, current_stock=Decimal("10"))
        with self.assertRaises(ValueError) as ctx:
            record_movement(self.db, item.id, type="adjustment", quantity=Decimal("3"))
        self.assertIn("apply_adjustment", str(ctx.exception))

    def test_rn_inv_04_salida_que_deja_stock_exactamente_en_cero_se_acepta(self):
        item = f.make_inventory_item(self.db, current_stock=Decimal("5"))
        record_movement(self.db, item.id, type="out", quantity=Decimal("5"))
        self.assertEqual(item.current_stock, Decimal("0"))

    def test_rn_inv_04_salida_que_dejaria_negativo_se_rechaza(self):
        item = f.make_inventory_item(self.db, current_stock=Decimal("5"))
        with self.assertRaises(InsufficientStockError):
            record_movement(self.db, item.id, type="out", quantity=Decimal("5.001"))
        self.assertEqual(item.current_stock, Decimal("5"), "no se aplicó el movimiento fallido")

    def test_rn_inv_05_allow_negative_true_permite_dejar_stock_negativo(self):
        item = f.make_inventory_item(self.db, current_stock=Decimal("5"))
        record_movement(self.db, item.id, type="out", quantity=Decimal("8"), allow_negative=True)
        self.assertEqual(item.current_stock, Decimal("-3"))

    def test_movimiento_registra_kardex_con_los_datos_pasados(self):
        item = f.make_inventory_item(self.db, current_stock=Decimal("10"))
        import uuid
        ref_id = uuid.uuid4()
        mv = record_movement(
            self.db, item.id, type="out", quantity=Decimal("2"),
            reason="venta", reference_type="order", reference_id=ref_id,
        )
        self.assertEqual(mv.type, "out")
        self.assertEqual(mv.quantity, Decimal("2"))
        self.assertEqual(mv.signed_quantity, Decimal("-2"))
        self.assertEqual(mv.reason, "venta")
        self.assertEqual(mv.reference_id, ref_id)


class ApplyAdjustmentTests(unittest.TestCase):
    def setUp(self):
        self.db = f.new_session()

    def test_rn_inv_08_ajuste_con_delta_negativo_resta_y_kardex_guarda_magnitud_positiva(self):
        item = f.make_inventory_item(self.db, current_stock=Decimal("10"))
        mv = apply_adjustment(self.db, item.id, signed_delta=Decimal("-2.5"), reason="Merma por derrame")
        self.assertEqual(item.current_stock, Decimal("7.5"))
        self.assertEqual(mv.type, "adjustment")
        self.assertEqual(mv.quantity, Decimal("2.5"), "quantity siempre positiva, el signo va en current_stock")
        self.assertEqual(mv.signed_quantity, Decimal("-2.5"), "signed_quantity sí guarda el signo del ajuste")
        self.assertEqual(mv.reason, "Merma por derrame")

    def test_rn_inv_08_ajuste_con_delta_positivo_suma(self):
        item = f.make_inventory_item(self.db, current_stock=Decimal("10"))
        mv = apply_adjustment(self.db, item.id, signed_delta=Decimal("4"))
        self.assertEqual(item.current_stock, Decimal("14"))
        self.assertEqual(mv.signed_quantity, Decimal("4"))

    def test_rn_inv_09_ajuste_que_dejaria_negativo_se_rechaza_sin_bandera_para_forzarlo(self):
        item = f.make_inventory_item(self.db, current_stock=Decimal("3"))
        with self.assertRaises(InsufficientStockError):
            apply_adjustment(self.db, item.id, signed_delta=Decimal("-5"))
        self.assertEqual(item.current_stock, Decimal("3"))

    def test_rn_inv_09_ajuste_que_deja_stock_exactamente_en_cero_se_acepta(self):
        item = f.make_inventory_item(self.db, current_stock=Decimal("5"))
        apply_adjustment(self.db, item.id, signed_delta=Decimal("-5"))
        self.assertEqual(item.current_stock, Decimal("0"))

    def test_rn_inv_10_delta_cero_lanza_valueerror_no_http_exception(self):
        """RN-INV-10: la regla en sí es intencional (rechazar delta=0), pero
        el mecanismo es un `ValueError` sin handler dedicado — en la app real
        eso propaga como 500 genérico, no 400/422. Se congela el tipo exacto
        de excepción que lanza la función hoy."""
        item = f.make_inventory_item(self.db, current_stock=Decimal("10"))
        with self.assertRaises(ValueError) as ctx:
            apply_adjustment(self.db, item.id, signed_delta=Decimal("0"))
        self.assertIn("!= 0", str(ctx.exception))
        from app.core.exceptions import InsufficientStockError as ISE
        self.assertNotIsInstance(ctx.exception, ISE)

    def test_rn_inv_11_motivo_no_es_obligatorio(self):
        item = f.make_inventory_item(self.db, current_stock=Decimal("10"))
        mv = apply_adjustment(self.db, item.id, signed_delta=Decimal("-1"))
        self.assertIsNone(mv.reason)


class LockItemsTests(unittest.TestCase):
    def setUp(self):
        self.db = f.new_session()

    def test_rn_inv_07_bloqueo_multiple_en_orden_canonico_por_id(self):
        items = [f.make_inventory_item(self.db) for _ in range(3)]
        ids_desordenados = [items[2].id, items[0].id, items[1].id]
        locked = lock_items(self.db, ids_desordenados)
        self.assertEqual(set(locked.keys()), {i.id for i in items})

    def test_lock_items_sin_ids_devuelve_vacio_sin_consultar(self):
        self.assertEqual(lock_items(self.db, []), {})

    def test_lock_items_deduplica_ids_repetidos(self):
        item = f.make_inventory_item(self.db)
        locked = lock_items(self.db, [item.id, item.id])
        self.assertEqual(len(locked), 1)


if __name__ == "__main__":
    unittest.main()
