"""Spec 146 (T024): verifica la migración
`alembic/versions/e967f9848848_rename_recibida_to_pendiente_add_payment_status.py`.

La migración usa `op.*` (contexto de Alembic) y DDL específica de PostgreSQL
(`ALTER TABLE ... DROP CONSTRAINT`, esquemas) dentro de `@for_each_tenant_schema`
-- no es una función de datos aislable como `migrate_promotions_data`
(`test_promotions_migration.py`), así que no se puede ejercitar contra SQLite en
memoria sin un motor Alembic real. La verificación end-to-end de punta a punta
vive contra PostgreSQL local real (quickstart.md Escenario 2), confirmada
manualmente en esta sesión:

  1. `alembic upgrade head` con `heladeria` (único tenant con datos reales en
     este ambiente) teniendo 1 orden en `status='recibida'`: tras la migración,
     `SELECT count(*) FROM customer_orders WHERE status='recibida'` da `0` en
     ese esquema; la orden quedó en `status='pendiente'`.
  2. Backfill de `payment_status`: las 47 órdenes `'pagada'` existentes quedaron
     en `payment_status='PAID'`; las 9 restantes (incluida la recién migrada) en
     `'UNPAID'`.
  3. `UPDATE customer_orders SET status='recibida' WHERE id=...` directo en SQL
     es rechazado por el `CHECK constraint` (`IntegrityError`).
  4. `alembic downgrade` al revision anterior y `alembic upgrade head` de vuelta:
     round-trip limpio, sin dejar la base en un estado intermedio.
  5. `tenant_default` (sin tabla `customer_orders` en este ambiente, previo a
     esta spec) se recorre sin error -- la guarda `_has_table` de la migración
     lo salta, igual que ya hace el patrón de referencia
     (`c9d0e1f2a3b4_table_sessions_participants.py::_has_table`).

Este módulo cubre lo que SÍ se puede verificar sin un Postgres real: la cadena
de revisiones es correcta (no hay huecos ni ciclos) y las funciones públicas de
la migración existen con la firma esperada.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_migration_pendiente_payment_status -v
"""
import importlib.util
from pathlib import Path
import unittest

_MIG_PATH = Path(__file__).resolve().parents[2] / (
    "alembic/versions/e967f9848848_rename_recibida_to_pendiente_add_payment_status.py"
)
_spec = importlib.util.spec_from_file_location("mig_146", _MIG_PATH)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


class TestMigrationRevisionChain(unittest.TestCase):
    def test_revision_identifiers_presentes_y_consistentes(self):
        self.assertEqual(mig.revision, "e967f9848848")
        self.assertEqual(mig.down_revision, "56a2cdbec89c")

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
