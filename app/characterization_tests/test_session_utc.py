"""Spec 138, Historia 4 (FR-004) — toda conexión a PostgreSQL trabaja en UTC.

`connect_args={"options": "-c timezone=UTC"}` NO sirve: pierde contra la variable de
entorno `PGTZ` (medido en la investigación, research.md Decisión 1: con `PGTZ=America/Bogota`
la sesión conserva Bogotá y las 17:00 UTC se guardan como 12:00). Lo que sí funciona es un
evento `connect` de SQLAlchemy que ejecuta `SET TIME ZONE 'UTC'` en autocommit, de modo que
sobrevive a un `rollback` y al pool. Un solo `engine` cubre API, worker de Celery,
scheduler, Alembic y scripts.

Estas pruebas no abren ninguna conexión real: usan un DBAPI simulado. La verificación contra
PostgreSQL de verdad la hace `app/scripts/verify_timezone_storage.py` (quickstart Q2).

    python -m unittest app.characterization_tests.test_session_utc -v
"""
import unittest

from sqlalchemy import create_engine, event

from app.core.db import force_utc_session, install_force_utc_session


class _CursorSimulado:
    def __init__(self, conexion):
        self._conexion = conexion
        self.cerrado = False

    def execute(self, sentencia, *args):
        if self._conexion.falla:
            raise RuntimeError("el motor rechazó la sentencia")
        # Registra con qué `autocommit` se ejecutó: SET TIME ZONE debe correr en autocommit
        # para que un rollback posterior no lo deshaga.
        self._conexion.ejecutadas.append((sentencia, self._conexion.autocommit))

    def close(self):
        self.cerrado = True


class _ConexionDbapiSimulada:
    def __init__(self, autocommit=False, falla=False):
        self.autocommit = autocommit
        self.falla = falla
        self.ejecutadas = []
        self.cursores = []

    def cursor(self):
        cursor = _CursorSimulado(self)
        self.cursores.append(cursor)
        return cursor


class TestForceUtcSession(unittest.TestCase):
    def test_ejecuta_set_time_zone_utc(self):
        conexion = _ConexionDbapiSimulada()
        force_utc_session(conexion, None)
        self.assertEqual([s for s, _ in conexion.ejecutadas], ["SET TIME ZONE 'UTC'"])

    def test_lo_ejecuta_en_autocommit_aunque_la_conexion_no_lo_estuviera(self):
        conexion = _ConexionDbapiSimulada(autocommit=False)
        force_utc_session(conexion, None)
        self.assertEqual(conexion.ejecutadas, [("SET TIME ZONE 'UTC'", True)])

    def test_deja_autocommit_como_estaba(self):
        for previo in (False, True):
            with self.subTest(autocommit_previo=previo):
                conexion = _ConexionDbapiSimulada(autocommit=previo)
                force_utc_session(conexion, None)
                self.assertIs(conexion.autocommit, previo)

    def test_cierra_el_cursor(self):
        conexion = _ConexionDbapiSimulada()
        force_utc_session(conexion, None)
        self.assertEqual(len(conexion.cursores), 1)
        self.assertTrue(conexion.cursores[0].cerrado)

    def test_si_el_motor_rechaza_la_sentencia_igual_restaura_autocommit_y_cierra_el_cursor(self):
        conexion = _ConexionDbapiSimulada(autocommit=False, falla=True)
        with self.assertRaises(RuntimeError):
            force_utc_session(conexion, None)
        self.assertIs(conexion.autocommit, False)
        self.assertTrue(conexion.cursores[0].cerrado)


class TestRegistroDelListener(unittest.TestCase):
    """El listener solo se registra sobre PostgreSQL: las suites usan SQLite y no deben ejecutarlo."""

    def test_en_postgresql_se_registra_como_evento_connect(self):
        # `create_engine` no conecta hasta que se pide una conexión.
        engine = create_engine("postgresql+psycopg://usuario:clave@localhost:5432/base")
        self.assertFalse(event.contains(engine, "connect", force_utc_session))
        install_force_utc_session(engine)
        self.assertTrue(event.contains(engine, "connect", force_utc_session))

    def test_en_sqlite_no_se_registra(self):
        engine = create_engine("sqlite:///:memory:")
        install_force_utc_session(engine)
        self.assertFalse(event.contains(engine, "connect", force_utc_session))

    def test_el_listener_de_sqlite_nunca_corre_al_conectar(self):
        engine = create_engine("sqlite:///:memory:")
        install_force_utc_session(engine)
        with engine.connect() as conexion:  # reventaría con `SET TIME ZONE` si estuviera registrado
            self.assertEqual(conexion.exec_driver_sql("SELECT 1").scalar(), 1)


if __name__ == "__main__":
    unittest.main()
