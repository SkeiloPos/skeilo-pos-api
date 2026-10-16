"""Comportamiento NUEVO (spec 121, FR-009/FR-011/FR-012/FR-015, D-4): catálogo de
denominaciones del tenant (`denomination_catalog`), su siembra por defecto y las
restricciones de base de datos que respaldan al desglose del cierre -- cita A-133
(tipo de cada línea) y A-132 (sin líneas en cero) de `registro-de-anomalias.md`.

La siembra de los tenants NUEVOS (`tenant_create`) no se puede ejercitar sobre
SQLite; se verifica contra PostgreSQL real (quickstart §6). Aquí se prueba la
función que esa ruta invoca, `seed_default_denominations`.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_cash_denomination_catalog -v
"""
import unittest
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.cash import router
from app.api.v1.cash.schemas import DenominationCatalogItem
from app.models.denomination_catalog import (
    DEFAULT_COP_DENOMINATIONS, DenominationCatalog, seed_default_denominations,
)

BILLETES = {100000, 50000, 20000, 10000, 5000, 2000}
MONEDAS = {1000, 500, 200, 100, 50}


def _filas(db):
    return db.execute(
        select(DenominationCatalog).order_by(DenominationCatalog.denomination.desc())
    ).scalars().all()


class TestSiembraDelCatalogo(unittest.TestCase):
    def test_siembra_crea_exactamente_11_filas_6_billetes_y_5_monedas(self):
        """FR-009: el catálogo COP por defecto."""
        db = fx.new_session()
        seed_default_denominations(db)
        db.commit()

        filas = _filas(db)
        self.assertEqual(len(filas), 11)
        self.assertEqual({int(f.denomination) for f in filas if f.kind == "billete"}, BILLETES)
        self.assertEqual({int(f.denomination) for f in filas if f.kind == "moneda"}, MONEDAS)

    def test_la_constante_por_defecto_coincide_con_data_model(self):
        self.assertEqual(len(DEFAULT_COP_DENOMINATIONS), 11)
        self.assertEqual(
            [int(v) for v, _ in DEFAULT_COP_DENOMINATIONS],
            [100000, 50000, 20000, 10000, 5000, 2000, 1000, 500, 200, 100, 50],
        )

    def test_siembra_es_idempotente(self):
        """Dos llamadas = 11 filas, no 22."""
        db = fx.new_session()
        seed_default_denominations(db)
        seed_default_denominations(db)
        db.commit()
        self.assertEqual(db.execute(select(func.count()).select_from(DenominationCatalog)).scalar(), 11)

    def test_siembra_solo_agrega_los_valores_que_faltan(self):
        db = fx.new_session()
        db.add(DenominationCatalog(denomination=Decimal("10000"), kind="billete"))
        db.commit()
        seed_default_denominations(db)
        db.commit()
        self.assertEqual(len(_filas(db)), 11)

    def test_fixture_de_siembra_usa_la_misma_funcion(self):
        db = fx.new_session()
        fx.seed_denomination_catalog(db)
        self.assertEqual(len(_filas(db)), 11)


class TestRestriccionesDelCatalogo(unittest.TestCase):
    def test_unique_rechaza_un_valor_repetido(self):
        db = fx.new_session()
        db.add(DenominationCatalog(denomination=Decimal("5000"), kind="billete"))
        db.commit()
        db.add(DenominationCatalog(denomination=Decimal("5000.00"), kind="billete"))
        with self.assertRaises(IntegrityError):
            db.commit()
        db.rollback()

    def test_check_rechaza_denominacion_no_positiva(self):
        for valor in (Decimal("0"), Decimal("-100")):
            with self.subTest(valor=valor):
                db = fx.new_session()
                db.add(DenominationCatalog(denomination=valor, kind="moneda"))
                with self.assertRaises(IntegrityError):
                    db.commit()
                db.rollback()

    def test_check_rechaza_un_tipo_distinto_de_billete_o_moneda(self):
        db = fx.new_session()
        db.add(DenominationCatalog(denomination=Decimal("1000"), kind="cheque"))
        with self.assertRaises(IntegrityError):
            db.commit()
        db.rollback()


class TestRestriccionesDelDesglose(unittest.TestCase):
    def test_check_rechaza_una_linea_con_cantidad_cero(self):
        """FR-015 como garantía de base de datos (D-3, A-132)."""
        db = fx.new_session()
        shift = fx.make_cash_shift(db)
        db.commit()
        with self.assertRaises(IntegrityError):
            fx.make_cash_count_denomination(db, shift, quantity=0)
        db.rollback()

    def test_check_rechaza_un_tipo_invalido_en_la_linea(self):
        db = fx.new_session()
        shift = fx.make_cash_shift(db)
        db.commit()
        with self.assertRaises(IntegrityError):
            fx.make_cash_count_denomination(db, shift, kind="cheque")
        db.rollback()

    def test_una_linea_con_cantidad_y_tipo_validos_se_guarda(self):
        db = fx.new_session()
        shift = fx.make_cash_shift(db)
        linea = fx.make_cash_count_denomination(
            db, shift, denomination=Decimal("500"), kind="moneda", quantity=3
        )
        db.commit()
        self.assertEqual(linea.kind, "moneda")
        self.assertEqual(linea.quantity, 3)


class TestEndpointCatalogo(unittest.TestCase):
    """GET /cash/denominations (spec 121, US3-1, US3-5, FR-011): solo lectura."""

    def test_devuelve_las_11_de_mayor_a_menor_con_su_tipo(self):
        db = fx.new_session()
        fx.seed_denomination_catalog(db)
        db.commit()

        items = router.list_denominations(db, fx.make_user_double())

        self.assertEqual(len(items), 11)
        self.assertEqual(
            [int(i.denomination) for i in items],
            [100000, 50000, 20000, 10000, 5000, 2000, 1000, 500, 200, 100, 50],
        )
        self.assertEqual(sum(1 for i in items if i.kind == "billete"), 6)
        self.assertEqual(sum(1 for i in items if i.kind == "moneda"), 5)

    def test_la_serializacion_es_decimal_como_cadena_y_tipo(self):
        db = fx.new_session()
        fx.seed_denomination_catalog(db)
        db.commit()
        items = router.list_denominations(db, fx.make_user_double())
        primero = DenominationCatalogItem.model_validate(items[0]).model_dump(mode="json")
        self.assertEqual(set(primero), {"denomination", "kind"})
        self.assertIsInstance(primero["denomination"], str)
        self.assertEqual(Decimal(primero["denomination"]), Decimal("100000"))
        self.assertEqual(primero["kind"], "billete")

    def test_un_catalogo_vacio_devuelve_lista_vacia_con_exito(self):
        """FR-013: no es un error del servidor; la pantalla lo trata como 'no se puede contar'."""
        db = fx.new_session()
        self.assertEqual(router.list_denominations(db, fx.make_user_double()), [])

    def test_no_hay_rutas_de_escritura_sobre_el_catalogo(self):
        rutas = [r for r in router.router.routes if r.path.rstrip("/") == "/cash/denominations"]
        self.assertEqual(len(rutas), 1)
        self.assertEqual(set(rutas[0].methods) - {"HEAD"}, {"GET"})
        for ruta in router.router.routes:
            if "denomination" in ruta.path:
                self.assertTrue(set(ruta.methods).isdisjoint({"POST", "PUT", "PATCH", "DELETE"}), ruta.path)


if __name__ == "__main__":
    unittest.main()
