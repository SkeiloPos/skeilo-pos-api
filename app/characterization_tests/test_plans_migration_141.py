"""Tests de la nueva funcionalidad — spec 141-modulo-inventario-unificado, US1:
la migración absorbe `compras_access` en `inventario_access` y elimina la columna
sin que ningún plan pierda acceso (FR-019…FR-021, SC-006; `data-model.md` § 2).

No son characterization tests — comportamiento enteramente nuevo. El paso de datos
es la función pura `merge_compras_into_inventario(connection, schema)`: aquí se
ejercita sobre SQLite con una tabla `plans` mínima y sin esquema (`schema=None`);
contra PostgreSQL real (`schema="shared"`) se verifica aparte, dentro de
`BEGIN … ROLLBACK` (tarea T029 de la spec).

    python -m unittest app.characterization_tests.test_plans_migration_141 -v
"""
import importlib.util
import os
import unittest
from pathlib import Path

import sqlalchemy as sa

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://x:x@localhost/x")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "test")

_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
# El nombre del archivo lleva el hash de la revisión: se localiza por sufijo.
_MIG = next(_VERSIONS.glob("*_141_plans_unify_inventory_module.py"))
_spec = importlib.util.spec_from_file_location("mig_141_plans_unify_inventory_module", _MIG)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)

#: (inventario, compras) antes → inventario esperado después (data-model § 1, «Regla de migración»).
CASOS = [
    ((False, False), False),  # US1.4 — igual
    ((False, True), True),    # US1.1 — gana Inventario y Proveedores, conserva Compras
    ((True, False), True),    # US1.3 — gana Proveedores y Compras (decisión de negocio 2)
    ((True, True), True),     # US1.2 — igual
]


class _Base(unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine("sqlite:///:memory:")
        self.conn = self.engine.connect()
        self.conn.execute(sa.text(
            "CREATE TABLE plans ("
            "id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "mesas_limit INTEGER, precio_mensual NUMERIC(12,2), "
            "inventario_access BOOLEAN NOT NULL DEFAULT 0, "
            "compras_access BOOLEAN NOT NULL DEFAULT 0, "
            "promociones_access BOOLEAN NOT NULL DEFAULT 0)"
        ))

    def tearDown(self):
        self.conn.close()
        self.engine.dispose()

    def _sembrar_cuatro_combinaciones(self):
        for i, ((inventario, compras), _) in enumerate(CASOS, start=1):
            self.conn.execute(
                sa.text(
                    "INSERT INTO plans (id, name, mesas_limit, precio_mensual, inventario_access, "
                    "compras_access, promociones_access) VALUES (:id, :name, :mesas, :precio, :inv, :com, :pro)"
                ),
                {
                    "id": i, "name": f"plan-{i}", "mesas": i * 3, "precio": f"{i}9000.50",
                    "inv": inventario, "com": compras, "pro": i % 2 == 0,
                },
            )

    def _filas(self):
        return self.conn.execute(sa.text(
            "SELECT id, name, mesas_limit, precio_mensual, inventario_access, promociones_access "
            "FROM plans ORDER BY id"
        )).fetchall()

    def _columnas(self):
        return {c["name"] for c in sa.inspect(self.conn).get_columns("plans")}


class PasoDeDatosTests(_Base):
    def test_las_cuatro_combinaciones_dan_el_resultado_de_la_regla(self):
        self._sembrar_cuatro_combinaciones()

        mig.merge_compras_into_inventario(self.conn, schema=None)

        resultado = [bool(f.inventario_access) for f in self._filas()]
        self.assertEqual(resultado, [esperado for _, esperado in CASOS])

    def test_ningun_plan_pierde_acceso(self):
        """FR-020, invariante 4: `inventario_después = inventario_antes OR compras_antes`."""
        self._sembrar_cuatro_combinaciones()

        mig.merge_compras_into_inventario(self.conn, schema=None)

        for fila, ((inventario, compras), _) in zip(self._filas(), CASOS):
            self.assertEqual(bool(fila.inventario_access), inventario or compras)

    def test_nada_mas_del_plan_cambia(self):
        """Invariantes 1 y 2: mismo número de filas; nombre, límites, precios y `promociones_access` idénticos."""
        self._sembrar_cuatro_combinaciones()
        antes = [(f.id, f.name, f.mesas_limit, f.precio_mensual, f.promociones_access) for f in self._filas()]
        total_antes = self.conn.execute(sa.text("SELECT count(*) FROM plans")).scalar_one()

        mig.merge_compras_into_inventario(self.conn, schema=None)

        despues = [(f.id, f.name, f.mesas_limit, f.precio_mensual, f.promociones_access) for f in self._filas()]
        self.assertEqual(despues, antes)
        self.assertEqual(self.conn.execute(sa.text("SELECT count(*) FROM plans")).scalar_one(), total_antes)

    def test_el_paso_de_datos_no_elimina_la_columna(self):
        self._sembrar_cuatro_combinaciones()

        mig.merge_compras_into_inventario(self.conn, schema=None)

        self.assertIn("compras_access", self._columnas())  # el `DROP` es de `upgrade()`, no de la función pura

    def test_segunda_ejecucion_no_cambia_nada(self):
        """FR-021, US1.5: idempotente."""
        self._sembrar_cuatro_combinaciones()
        mig.merge_compras_into_inventario(self.conn, schema=None)
        despues_de_una = self._filas()

        mig.merge_compras_into_inventario(self.conn, schema=None)

        self.assertEqual(self._filas(), despues_de_una)

    def test_sin_la_columna_es_un_no_op_sin_error(self):
        """Idempotencia tras el `DROP`: si `compras_access` ya no existe, la función no hace nada."""
        self.conn.execute(sa.text("DROP TABLE plans"))
        self.conn.execute(sa.text(
            "CREATE TABLE plans (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "inventario_access BOOLEAN NOT NULL DEFAULT 0, promociones_access BOOLEAN NOT NULL DEFAULT 0)"
        ))
        self.conn.execute(sa.text("INSERT INTO plans (id, name, inventario_access) VALUES (1, 'sin-compras', 1)"))

        mig.merge_compras_into_inventario(self.conn, schema=None)  # no lanza

        self.assertEqual(
            self.conn.execute(sa.text("SELECT inventario_access FROM plans WHERE id = 1")).scalar_one(), 1
        )


class EstructuraDeLaMigracionTests(unittest.TestCase):
    def test_encadena_con_la_cabeza_anterior(self):
        self.assertEqual(mig.down_revision, "56a2cdbec89c")

    def test_solo_toca_el_esquema_shared(self):
        """data-model § 2: no usa `for_each_tenant_schema` (no recorre esquemas de tenants)."""
        self.assertEqual(mig.SHARED_SCHEMA, "shared")
        self.assertFalse(hasattr(mig.upgrade, "__wrapped__"), "upgrade() no debe ir envuelta por for_each_tenant_schema")
        self.assertFalse(hasattr(mig.downgrade, "__wrapped__"), "downgrade() no debe ir envuelta por for_each_tenant_schema")

    def test_el_docstring_declara_que_no_se_recupera(self):
        """FR-021, Principio VIII: la reversión declara explícitamente qué no recupera."""
        doc = (mig.__doc__ or "") + (mig.downgrade.__doc__ or "")
        self.assertIn("no se recupera", doc.lower())


if __name__ == "__main__":
    unittest.main()
