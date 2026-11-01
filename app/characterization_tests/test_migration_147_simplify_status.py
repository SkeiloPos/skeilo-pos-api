"""Spec 147 (A-151, T005): verifica la migración
`alembic/versions/6bf8898f6e63_147_simplify_order_item_status_payment.py`.

Igual que `test_migration_pendiente_payment_status.py` (spec 146), esta migración
usa `op.*` (contexto de Alembic) y DDL específica de PostgreSQL dentro de
`@for_each_tenant_schema` — no es una función de datos aislable, así que no se
puede ejercitar contra SQLite en memoria sin un motor Alembic real.

Verificación end-to-end contra PostgreSQL local real, confirmada manualmente en
esta sesión:

  1. `alembic upgrade 6bf8898f6e63` aplicado sobre `heladeria` (único tenant con
     datos reales en este ambiente): tras la migración, el `SELECT` de control
     de `data-model.md` (SC-007) da `count(*) == 0` tanto en `customer_orders`
     (`status`/`payment_status` fuera del conjunto final) como en `order_items`
     (`estado_cocina` fuera del conjunto final). `tenant_default` (sin tabla
     `customer_orders` en este ambiente) se recorre sin error — guarda
     `_has_table`.
  2. Caso de forzado FR-023 in fine, verificado sobre un esquema de prueba
     descartable (`test_147_scratch`, creado y eliminado dentro de una
     transacción que se revierte al final, sin tocar datos reales) con filas
     fixture en los 6 estados viejos de pedido y los 4 de ítem: una fila
     `'bloqueada'` con un ítem `'pendiente'` termina `status='entregada'` con
     ese ítem forzado a `estado_cocina='entregado'` (no `'pendiente'`); una
     fila `'cancelada'` con un ítem `'pendiente'` termina con ese ítem forzado
     a `'cancelado'`.
  3. **Corrección descubierta durante esta verificación** (ver `data-model.md`,
     "Corrección post-implementación"): la migración también dropea y recrea
     `idx_active_order_per_participant` con el criterio de "orden abierta" de
     FR-016 (`status = 'pendiente' OR (status = 'entregada' AND
     payment_status = 'UNPAID')`), no el `status != 'cancelada'` originalmente
     propuesto — ese predicado chocaba con datos reales (`heladeria`, dos
     pedidos históricos ya `'pagada'`+`'PAID'` del mismo `participant_id`,
     ambos pasarían a `'entregada'` y quedarían simultáneamente "activos" bajo
     `!= 'cancelada'`, violando la unicidad). El criterio corregido excluye un
     pedido ya cerrado (`'entregada'`+`'PAID'`) de la cuenta de activo,
     verificado contra ese mismo caso real sin error.
  4. `UPDATE customer_orders SET status='bloqueada' WHERE id=...` directo en
     SQL tras la migración es rechazado por el `CHECK constraint` nuevo
     (`IntegrityError`) — `'bloqueada'` ya no es un valor válido.

Este módulo cubre lo que SÍ se puede verificar sin un Postgres real: la cadena
de revisiones es correcta y las funciones públicas de la migración existen con
la firma esperada.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_migration_147_simplify_status -v
"""
import importlib.util
from pathlib import Path
import unittest

_MIG_PATH = Path(__file__).resolve().parents[2] / (
    "alembic/versions/6bf8898f6e63_147_simplify_order_item_status_payment.py"
)
_spec = importlib.util.spec_from_file_location("mig_147", _MIG_PATH)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


class TestMigrationRevisionChain(unittest.TestCase):
    def test_revision_identifiers_presentes_y_consistentes(self):
        self.assertEqual(mig.revision, "6bf8898f6e63")
        self.assertEqual(mig.down_revision, "e967f9848848")

    def test_upgrade_y_downgrade_existen_y_son_invocables(self):
        # `for_each_tenant_schema` envuelve ambas en `wrapped()` (sin
        # argumentos) -- confirma que el decorador se aplicó (no quedaron como
        # funciones `(schema)` crudas, que romperían `alembic upgrade`).
        self.assertTrue(callable(mig.upgrade))
        self.assertTrue(callable(mig.downgrade))
        self.assertEqual(mig.upgrade.__code__.co_argcount, 0)
        self.assertEqual(mig.downgrade.__code__.co_argcount, 0)


if __name__ == "__main__":
    unittest.main()
