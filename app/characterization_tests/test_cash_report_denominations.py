"""Comportamiento NUEVO (spec 121, FR-020 a FR-023, US4 y US5; A-133/A-134 de
`registro-de-anomalias.md`): el reporte de cierre (`GET /cash/shifts/{id}/report`) trae el
desglose del conteo con tipo y subtotal derivado, la suma de subtotales coincide con el
efectivo contado, y los turnos cerrados antes de la spec siguen siendo consultables (sin
líneas, sin 'Contado por', sin error).

Invoca `router.shift_report` directamente como función Python.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_cash_report_denominations -v
"""
import unittest
from datetime import datetime
from decimal import Decimal

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.cash import router
from app.api.v1.cash.schemas import DenominationIn, ShiftClose, ShiftReportResponse

ESPERADO = Decimal("115000")


def _line(valor, cantidad):
    return DenominationIn(denomination=Decimal(str(valor)), quantity=cantidad)


def _turno_abierto(opening=ESPERADO):
    db = fx.new_session()
    fx.seed_denomination_catalog(db)
    shift = fx.make_cash_shift(db, opening_amount=opening, user_name="Ana")
    db.commit()
    return db, shift


def _reporte(db, shift, quien=None):
    return router.shift_report(shift.id, db, quien or fx.make_user_double(name="Admin"))


def _como_json(reporte):
    """Lo que realmente viaja: pasa por `ShiftReportResponse`."""
    return ShiftReportResponse.model_validate(reporte).model_dump(mode="json")


class TestReporteConDesglose(unittest.TestCase):
    def _cerrado_caso_1(self):
        db, shift = _turno_abierto()
        body = ShiftClose(denominations=[_line(100000, 1), _line(10000, 1), _line(5000, 1)])
        router.close_shift(shift.id, body, db, fx.make_user_double(name="Luis"))
        return db, shift

    def test_trae_tres_lineas_de_mayor_a_menor_con_tipo_cantidad_y_subtotal(self):
        """US4-1."""
        db, shift = self._cerrado_caso_1()
        lineas = _reporte(db, shift)["denominations"]

        self.assertEqual(len(lineas), 3)
        self.assertEqual([int(ln.denomination) for ln in lineas], [100000, 10000, 5000])
        self.assertTrue(all(ln.kind == "billete" for ln in lineas))
        self.assertTrue(all(ln.quantity == 1 for ln in lineas))
        self.assertEqual([ln.subtotal for ln in lineas],
                         [Decimal("100000"), Decimal("10000"), Decimal("5000")])

    def test_la_suma_de_subtotales_es_igual_al_efectivo_contado(self):
        """FR-021: invariante del contrato."""
        db, shift = self._cerrado_caso_1()
        reporte = _reporte(db, shift)
        suma = sum((ln.subtotal for ln in reporte["denominations"]), Decimal(0))
        self.assertEqual(suma, shift.counted_amount)
        self.assertEqual(suma, Decimal("115000"))

    def test_el_subtotal_se_deriva_y_no_se_almacena(self):
        db, shift = _turno_abierto(opening=Decimal("1500"))
        router.close_shift(
            shift.id, ShiftClose(denominations=[_line(500, 3)]), db, fx.make_user_double()
        )
        (linea,) = _reporte(db, shift)["denominations"]
        self.assertEqual(linea.kind, "moneda")
        self.assertEqual(linea.subtotal, Decimal("1500"))
        # La tabla no tiene columna de subtotal.
        from app.models.cash_count_denomination import CashCountDenomination
        self.assertNotIn("subtotal", CashCountDenomination.__table__.columns.keys())

    def test_lista_vacia_da_denominaciones_vacias_y_contado_cero(self):
        """US4-4."""
        db, shift = _turno_abierto()
        router.close_shift(
            shift.id, ShiftClose(denominations=[], close_note="Caja vacía"), db,
            fx.make_user_double(),
        )
        reporte = _reporte(db, shift)
        self.assertEqual(reporte["denominations"], [])
        self.assertEqual(shift.counted_amount, Decimal("0"))

    def test_closed_by_es_quien_cerro_aunque_consulte_otro_usuario(self):
        """US4-5."""
        db, shift = self._cerrado_caso_1()
        reporte = _reporte(db, shift, quien=fx.make_user_double(name="Otra persona"))
        self.assertEqual(reporte["shift"].closed_by_user_name, "Luis")
        self.assertEqual(reporte["shift"].user_name, "Ana")

    def test_closed_by_es_un_snapshot_del_nombre_al_cierre(self):
        """US4-6: renombrar al usuario después no cambia el reporte (el nombre vive en
        la propia fila del turno; el reporte no consulta `shared.users`)."""
        db, shift = _turno_abierto()
        luis = fx.make_user_double(name="Luis")
        body = ShiftClose(denominations=[_line(100000, 1), _line(10000, 1), _line(5000, 1)])
        router.close_shift(shift.id, body, db, luis)

        luis.name = "Luis Renombrado"

        self.assertEqual(_reporte(db, shift)["shift"].closed_by_user_name, "Luis")

    def test_un_turno_abierto_devuelve_listas_vacias_y_closed_by_nulo_sin_fallar(self):
        db, shift = _turno_abierto()
        reporte = _reporte(db, shift)
        self.assertEqual(reporte["denominations"], [])
        self.assertIsNone(reporte["shift"].closed_by_user_id)
        self.assertIsNone(reporte["shift"].closed_by_user_name)

    def test_el_json_del_reporte_trae_los_campos_del_contrato(self):
        db, shift = self._cerrado_caso_1()
        datos = _como_json(_reporte(db, shift))
        self.assertEqual(
            set(datos["denominations"][0]), {"denomination", "kind", "quantity", "subtotal"}
        )
        self.assertEqual(datos["shift"]["closed_by_user_name"], "Luis")
        self.assertIn("closed_by_user_id", datos["shift"])
        # Cambio aditivo: los campos del reporte anterior siguen presentes.
        for campo in ("shift", "reconciliation", "movements", "denominations", "close_note"):
            self.assertIn(campo, datos)


class TestTurnosHistoricos(unittest.TestCase):
    """US5: turnos cerrados ANTES de esta spec (sembrados directo, sin `close_shift`)."""

    def _historico(self, db, *, counted="115000", **kw):
        kw.setdefault("status", "closed")
        kw.setdefault("closed_at", datetime.now())
        return fx.make_cash_shift(
            db, opening_amount=ESPERADO, counted_amount=counted, close_note="cuadra", **kw
        )

    def test_a_turno_sin_lineas_ni_closed_by_se_consulta_sin_error(self):
        """US5-1, US5-4: mensaje 'sin desglose' en pantalla; aquí, lista vacía e intactos."""
        db = fx.new_session()
        fx.seed_denomination_catalog(db)
        shift = self._historico(db)
        db.commit()

        reporte = _reporte(db, shift)

        self.assertEqual(reporte["denominations"], [])
        self.assertEqual(shift.counted_amount, Decimal("115000"))
        self.assertEqual(reporte["reconciliation"]["difference"], Decimal("0"))
        self.assertIsNone(shift.closed_by_user_name)
        self.assertIsNone(reporte["shift"].closed_by_user_name)

    def test_b_turno_con_lineas_previas_las_devuelve_con_su_tipo(self):
        """US5-2."""
        db = fx.new_session()
        shift = self._historico(db, counted="110000")
        fx.make_cash_count_denomination(db, shift, denomination=Decimal("100000"), kind="billete", quantity=1)
        fx.make_cash_count_denomination(db, shift, denomination=Decimal("10000"), kind="billete", quantity=1)
        db.commit()

        lineas = _reporte(db, shift)["denominations"]

        self.assertEqual([(int(ln.denomination), ln.kind, ln.subtotal) for ln in lineas],
                         [(100000, "billete", Decimal("100000")), (10000, "billete", Decimal("10000"))])

    def test_c_cierre_antiguo_sin_conteo_con_counted_amount_nulo_no_falla(self):
        """Cierre 'sin conteo' (RN-CASH-09) anterior a la spec: `counted_amount` NULL."""
        db = fx.new_session()
        shift = self._historico(db, counted=None)
        db.commit()

        reporte = _reporte(db, shift)

        self.assertEqual(reporte["denominations"], [])
        self.assertIsNone(reporte["reconciliation"]["difference"])
        self.assertIsNone(_como_json(reporte)["shift"]["counted_amount"])


if __name__ == "__main__":
    unittest.main()
