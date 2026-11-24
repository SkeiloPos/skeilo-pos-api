"""CONGELA app/core/inventory_reasons.py — catálogo de motivos del kardex.

Referencia: `reglas-de-negocio.md` RN-INV-13.
"""
import unittest

from app.core import inventory_reasons as r


class InventoryReasonsCatalogTests(unittest.TestCase):
    def test_rn_inv_13_seis_motivos_canonicos_con_estos_valores_exactos(self):
        self.assertEqual(r.VENTA, "venta")
        self.assertEqual(r.COMPRA, "compra")
        self.assertEqual(r.AJUSTE, "ajuste")
        self.assertEqual(r.DANO, "daño")
        self.assertEqual(r.VENCIMIENTO, "vencimiento")
        self.assertEqual(r.CONSUMO_INTERNO, "consumo_interno")

    # A-165 (registro-de-anomalias.md): séptimo motivo canónico -- la
    # importación masiva de Excel ahora pasa por kardex en vez de escribir
    # `current_stock` directo.
    def test_a165_importacion_masiva_y_referencia_de_job(self):
        self.assertEqual(r.IMPORTACION_MASIVA, "importacion_masiva")
        self.assertEqual(r.REF_IMPORT_JOB, "import_job")

    def test_rn_inv_13_ajuste_es_valido_como_entrada_y_como_salida_a_la_vez(self):
        self.assertIn(r.AJUSTE, r.ENTRADA_REASONS)
        self.assertIn(r.AJUSTE, r.SALIDA_REASONS)

    def test_rn_inv_13_venta_solo_es_salida_compra_solo_es_entrada(self):
        self.assertIn(r.VENTA, r.SALIDA_REASONS)
        self.assertNotIn(r.VENTA, r.ENTRADA_REASONS)
        self.assertIn(r.COMPRA, r.ENTRADA_REASONS)
        self.assertNotIn(r.COMPRA, r.SALIDA_REASONS)

    def test_rn_inv_13_catalogo_completo_de_entrada_y_salida(self):
        self.assertEqual(set(r.ENTRADA_REASONS), {"compra", "ajuste", "importacion_masiva"})
        self.assertEqual(
            set(r.SALIDA_REASONS),
            {"venta", "daño", "vencimiento", "consumo_interno", "ajuste"},
        )

    def test_referencias_catalogadas(self):
        self.assertEqual(r.REF_SALE, "sale")
        self.assertEqual(r.REF_ORDER, "order")
        self.assertEqual(r.REF_ORDER_VOID, "order_void")
        self.assertEqual(r.REF_PURCHASE, "purchase")


if __name__ == "__main__":
    unittest.main()
