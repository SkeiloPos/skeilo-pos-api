"""Tests de la nueva funcionalidad -- spec 121 (A-132, A-133): los **pasos de datos**
de la revisión Alembic `e0faa21295a3` (`121_denomination_breakdown`), ejercitados sobre
SQLite en memoria (mismo patrón que `test_presentations_migration.py`).

Se prueban las funciones de módulo que reciben `Connection` y esquema: la siembra del
catálogo, la compuerta FR-028 (denominación fuera del catálogo), el tipo deducido por
valor y el descarte de líneas con cantidad 0. El DDL (CREATE/ALTER ... IF [NOT] EXISTS,
CHECK, reversibilidad) es específico de PostgreSQL y se verifica contra la BD real
(quickstart §2).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_migrate_denomination_breakdown -v
"""
import importlib.util
import os
import unittest
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://x:x@localhost/x")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "test")

_MIG = Path(__file__).resolve().parents[2] / (
    "alembic/versions/e0faa21295a3_121_denomination_breakdown.py"
)
_spec = importlib.util.spec_from_file_location("mig_121_denomination_breakdown", _MIG)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


class _Base(unittest.TestCase):
    """Esquema legado: sin `kind`, `quantity >= 0`, sin catálogo todavía."""

    def setUp(self):
        self.engine = sa.create_engine("sqlite:///:memory:")
        self.conn = self.engine.connect()
        self.conn.execute(sa.text(
            "CREATE TABLE cash_shifts ("
            "id TEXT PRIMARY KEY, counted_amount NUMERIC(12,2), close_note TEXT, status TEXT)"
        ))
        self.conn.execute(sa.text(
            "CREATE TABLE cash_count_denominations ("
            "id TEXT PRIMARY KEY, cash_shift_id TEXT, denomination NUMERIC(12,2), "
            "quantity INTEGER, kind VARCHAR(10))"
        ))
        self.conn.execute(sa.text(
            "CREATE TABLE denomination_catalog ("
            "id TEXT PRIMARY KEY, denomination NUMERIC(12,2) UNIQUE, kind VARCHAR(10))"
        ))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.engine.dispose()

    def _shift(self, counted="115000", note="nota", status="closed"):
        sid = str(uuid4())
        self.conn.execute(
            sa.text("INSERT INTO cash_shifts VALUES (:i, :c, :n, :s)"),
            {"i": sid, "c": counted, "n": note, "s": status},
        )
        return sid

    def _line(self, shift_id, denomination, quantity, kind=None):
        self.conn.execute(
            sa.text("INSERT INTO cash_count_denominations VALUES (:i, :s, :d, :q, :k)"),
            {"i": str(uuid4()), "s": shift_id, "d": str(denomination), "q": quantity, "k": kind},
        )

    def _lines(self):
        rows = self.conn.execute(sa.text(
            "SELECT denomination, quantity, kind FROM cash_count_denominations "
            "ORDER BY denomination DESC"
        )).fetchall()
        return [(Decimal(str(r[0])), r[1], r[2]) for r in rows]

    def _catalog(self):
        rows = self.conn.execute(sa.text(
            "SELECT denomination, kind FROM denomination_catalog ORDER BY denomination DESC"
        )).fetchall()
        return [(Decimal(str(r[0])), r[1]) for r in rows]


class SiembraDelCatalogoTests(_Base):
    def test_siembra_los_11_valores_cop(self):
        mig._seed_catalog(self.conn, None)
        self.conn.commit()
        catalogo = self._catalog()
        self.assertEqual(len(catalogo), 11)
        self.assertEqual(sum(1 for _, k in catalogo if k == "billete"), 6)
        self.assertEqual(sum(1 for _, k in catalogo if k == "moneda"), 5)

    def test_siembra_es_idempotente(self):
        mig._seed_catalog(self.conn, None)
        mig._seed_catalog(self.conn, None)
        self.conn.commit()
        self.assertEqual(len(self._catalog()), 11)

    def test_la_lista_de_la_migracion_coincide_con_la_del_modelo(self):
        """La migración lleva su propia copia literal; no deben divergir hoy."""
        from app.models.denomination_catalog import DEFAULT_COP_DENOMINATIONS
        self.assertEqual(tuple(mig._CATALOG), tuple(DEFAULT_COP_DENOMINATIONS))


class CompuertaFueraDelCatalogoTests(_Base):
    def setUp(self):
        super().setUp()
        mig._seed_catalog(self.conn, None)
        self.shift = self._shift()

    def test_detecta_una_fila_con_valor_fuera_del_catalogo(self):
        self._line(self.shift, 3000, 2)
        self.assertEqual(mig._find_out_of_catalog(self.conn, None), [Decimal("3000")])

    def test_la_compuerta_aborta_con_runtime_error(self):
        self._line(self.shift, 3000, 2)
        with self.assertRaises(RuntimeError) as ctx:
            mig._gate(self.conn, None)
        self.assertIn("3.000", str(ctx.exception))

    def test_el_mensaje_nombra_esquema_y_valor(self):
        msg = mig._gate_message("heladeria", [Decimal("3000"), Decimal("750")])
        self.assertIn("heladeria", msg)
        self.assertIn("$3.000", msg)
        self.assertIn("$750", msg)

    def test_la_compuerta_no_descarta_ni_modifica_nada(self):
        self._line(self.shift, 3000, 2)
        self._line(self.shift, 10000, 1)
        with self.assertRaises(RuntimeError):
            mig._gate(self.conn, None)
        self.assertEqual(len(self._lines()), 2)

    def test_sin_valores_fuera_del_catalogo_no_hace_nada(self):
        self._line(self.shift, 10000, 1)
        self._line(self.shift, 50, 4)
        self.assertEqual(mig._find_out_of_catalog(self.conn, None), [])
        mig._gate(self.conn, None)


class TipoDeducidoPorValorTests(_Base):
    def setUp(self):
        super().setUp()
        mig._seed_catalog(self.conn, None)

    def test_el_tipo_sale_del_catalogo_por_valor(self):
        s = self._shift()
        self._line(s, 100000, 1)
        self._line(s, 2000, 3)
        self._line(s, 1000, 5)
        self._line(s, 500, 2)
        mig._backfill_kind(self.conn, None)
        self.conn.commit()
        por_valor = {v: k for v, _, k in self._lines()}
        self.assertEqual(por_valor[Decimal("100000")], "billete")
        self.assertEqual(por_valor[Decimal("2000")], "billete")
        self.assertEqual(por_valor[Decimal("1000")], "moneda")
        self.assertEqual(por_valor[Decimal("500")], "moneda")

    def test_solo_rellena_donde_kind_es_nulo(self):
        s = self._shift()
        self._line(s, 10000, 1, kind="billete")
        self._line(s, 50, 1)
        self.assertEqual(mig._backfill_kind(self.conn, None), 1)

    def test_valor_y_cantidad_quedan_intactos(self):
        s = self._shift()
        self._line(s, 20000, 7)
        mig._backfill_kind(self.conn, None)
        self.assertEqual(self._lines(), [(Decimal("20000"), 7, "billete")])


class DescarteDeCantidadCeroTests(_Base):
    def test_descarta_solo_las_filas_con_cantidad_cero(self):
        s = self._shift()
        self._line(s, 100000, 1)
        self._line(s, 50000, 0)
        self._line(s, 10000, 2)
        self._line(s, 5000, 0)
        self.assertEqual(mig._discard_zero_quantity(self.conn, None), 2)
        self.conn.commit()
        self.assertEqual(
            [(v, q) for v, q, _ in self._lines()],
            [(Decimal("100000"), 1), (Decimal("10000"), 2)],
        )

    def test_conserva_valor_y_cantidad_del_resto(self):
        s = self._shift()
        self._line(s, 2000, 9)
        self._line(s, 200, 0)
        mig._discard_zero_quantity(self.conn, None)
        self.assertEqual([(v, q) for v, q, _ in self._lines()], [(Decimal("2000"), 9)])


class ReejecucionYMontosTests(_Base):
    def _migrar(self):
        mig._seed_catalog(self.conn, None)
        mig._gate(self.conn, None)
        mig._backfill_kind(self.conn, None)
        mig._discard_zero_quantity(self.conn, None)
        self.conn.commit()

    def test_reejecutar_sobre_un_esquema_en_forma_final_no_falla_ni_cambia_nada(self):
        s = self._shift()
        self._line(s, 100000, 1)
        self._line(s, 50000, 0)
        self._line(s, 500, 4)
        self._migrar()
        primera = self._lines()
        catalogo = self._catalog()

        self._migrar()  # segunda vez: no-op

        self.assertEqual(self._lines(), primera)
        self.assertEqual(self._catalog(), catalogo)
        self.assertEqual(len(catalogo), 11)

    def test_montos_y_estado_de_los_turnos_quedan_identicos(self):
        """SC-004 / FR-027: ningún paso toca `counted_amount`, `close_note` ni `status`."""
        a = self._shift(counted="115000", note="cuadra", status="closed")
        b = self._shift(counted=None, note="", status="closed")
        self._line(a, 100000, 1)
        self._line(a, 10000, 1)
        self._line(a, 5000, 0)

        def huella():
            return self.conn.execute(sa.text(
                "SELECT id, counted_amount, close_note, status FROM cash_shifts ORDER BY id"
            )).fetchall()

        antes = huella()
        self._migrar()
        self.assertEqual(huella(), antes)
        self.assertEqual(len(antes), 2)
        self.assertIn(b, [r[0] for r in antes])

    def test_un_turno_sin_lineas_sigue_sin_lineas(self):
        self._shift()
        self._migrar()
        self.assertEqual(self._lines(), [])


if __name__ == "__main__":
    unittest.main()
