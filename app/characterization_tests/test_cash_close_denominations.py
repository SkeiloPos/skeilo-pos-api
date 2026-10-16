"""Comportamiento NUEVO (spec 121, D-1 a D-5; A-130 a A-134 de `registro-de-anomalias.md`):
el cierre de turno recibe SOLO `{denominación, cantidad}` + observación; el servidor
calcula subtotal, efectivo contado y diferencia, persiste únicamente las líneas con
cantidad > 0 (con su tipo), registra quién cerró y rechaza de forma atómica todo conteo
inválido. Antes (RN-CASH-09, A-20) el cliente podía enviar `counted_amount` y existía el
cierre "sin conteo".

Invoca `app/api/v1/cash/router.py::close_shift` directamente como función Python (mismo
patrón que `test_cash_close_active_orders.py`). SQLite ignora `SELECT ... FOR UPDATE`: la
serialización real de dos cierres simultáneos se verifica contra PostgreSQL (quickstart §5);
aquí solo se demuestra el resultado secuencial.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_cash_close_denominations -v
"""
import unittest
from decimal import Decimal
from unittest import mock

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.cash import router, service
from app.api.v1.cash.schemas import DenominationIn, ShiftClose
from app.models.cash_count_denomination import CashCountDenomination
from app.models.cash_shift import CashShift

ESPERADO = Decimal("115000")


def _line(valor, cantidad):
    return DenominationIn(denomination=Decimal(str(valor)), quantity=cantidad)


def _nuevo_turno(opening=ESPERADO):
    db = fx.new_session()
    fx.seed_denomination_catalog(db)
    shift = fx.make_cash_shift(db, opening_amount=opening)
    db.commit()
    return db, shift


def _cerrar(db, shift, body, user=None):
    user = user or fx.make_user_double(name="Luis")
    return router.close_shift(shift.id, body, db, user)


def _filas(db, shift):
    return db.execute(
        select(CashCountDenomination)
        .where(CashCountDenomination.cash_shift_id == shift.id)
        .order_by(CashCountDenomination.denomination.desc())
    ).scalars().all()


def _cuenta_filas(db, shift):
    return db.execute(
        select(func.count()).select_from(CashCountDenomination)
        .where(CashCountDenomination.cash_shift_id == shift.id)
    ).scalar_one()


def _intacto(test, db, shift):
    """Tras un rechazo: el turno sigue abierto, sin monto ni observación, y sin filas.
    Se vuelve a leer tras `rollback()` (la sesión de prueba usa autoflush)."""
    db.rollback()
    db.expire_all()
    fresco = db.get(CashShift, shift.id)
    test.assertEqual(fresco.status, "open")
    test.assertIsNone(fresco.closed_at)
    test.assertIsNone(fresco.counted_amount)
    test.assertIsNone(fresco.close_note)
    test.assertIsNone(fresco.closed_by_user_id)
    test.assertEqual(_cuenta_filas(db, shift), 0)


class TestCierreConDesglose(unittest.TestCase):
    def test_a_cuadre_exacto_guarda_tres_lineas_con_su_tipo(self):
        """US1-1 / SC-001: 100000×1 + 10000×1 + 5000×1 contra un esperado de $115.000."""
        db, shift = _nuevo_turno()
        body = ShiftClose(denominations=[_line(100000, 1), _line(10000, 1), _line(5000, 1)])

        cerrado = _cerrar(db, shift, body)

        self.assertEqual(cerrado.status, "closed")
        self.assertEqual(cerrado.counted_amount, Decimal("115000"))
        self.assertEqual(service.reconcile(db, cerrado)["difference"], Decimal("0"))
        filas = _filas(db, shift)
        self.assertEqual(len(filas), 3)
        self.assertEqual([int(f.denomination) for f in filas], [100000, 10000, 5000])
        self.assertTrue(all(f.kind == "billete" for f in filas))
        self.assertTrue(all(f.quantity == 1 for f in filas))

    def test_a_las_monedas_se_guardan_con_tipo_moneda(self):
        db, shift = _nuevo_turno(opening=Decimal("2500"))
        body = ShiftClose(denominations=[_line(2000, 1), _line(500, 1)])
        _cerrar(db, shift, body)
        tipos = {int(f.denomination): f.kind for f in _filas(db, shift)}
        self.assertEqual(tipos, {2000: "billete", 500: "moneda"})

    def test_b_faltante_con_observacion_cierra_con_diferencia_negativa(self):
        """US1-2: $110.000 contados contra $115.000 esperados -> diferencia −$5.000."""
        db, shift = _nuevo_turno()
        body = ShiftClose(
            denominations=[_line(100000, 1), _line(10000, 1)],
            close_note="Faltan $5.000 del cambio de la mañana",
        )

        cerrado = _cerrar(db, shift, body)

        self.assertEqual(cerrado.status, "closed")
        self.assertEqual(cerrado.counted_amount, Decimal("110000"))
        self.assertEqual(cerrado.close_note, "Faltan $5.000 del cambio de la mañana")
        self.assertEqual(service.reconcile(db, cerrado)["difference"], Decimal("-5000"))

    def test_c_faltante_sin_observacion_es_422_y_no_cierra(self):
        """US1-3 / RN-CASH-10: igual que (b) pero SIN observación."""
        db, shift = _nuevo_turno()
        body = ShiftClose(denominations=[_line(100000, 1), _line(10000, 1)])

        with self.assertRaises(HTTPException) as ctx:
            _cerrar(db, shift, body)

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("observación", ctx.exception.detail)
        _intacto(self, db, shift)

    def test_c_observacion_en_blanco_tampoco_basta(self):
        db, shift = _nuevo_turno()
        body = ShiftClose(denominations=[_line(100000, 1)], close_note="   ")
        with self.assertRaises(HTTPException) as ctx:
            _cerrar(db, shift, body)
        self.assertEqual(ctx.exception.status_code, 422)
        _intacto(self, db, shift)

    def test_d_la_linea_en_cero_no_se_guarda(self):
        """US1-4 / FR-015: cuatro líneas, una en cantidad 0 -> tres filas."""
        db, shift = _nuevo_turno()
        body = ShiftClose(denominations=[
            _line(100000, 1), _line(50000, 0), _line(10000, 1), _line(5000, 1),
        ])

        _cerrar(db, shift, body)

        filas = _filas(db, shift)
        self.assertEqual([int(f.denomination) for f in filas], [100000, 10000, 5000])
        self.assertNotIn(50000, [int(f.denomination) for f in filas])

    def test_e_lista_vacia_es_contado_cero_y_exige_observacion(self):
        """US1-5 / D-2: caja vacía por API. Contado $0, sin filas, diferencia = −esperado."""
        db, shift = _nuevo_turno()
        cerrado = _cerrar(db, shift, ShiftClose(denominations=[], close_note="Caja vacía"))

        self.assertEqual(cerrado.status, "closed")
        self.assertEqual(cerrado.counted_amount, Decimal("0"))
        self.assertEqual(_cuenta_filas(db, shift), 0)
        self.assertEqual(service.reconcile(db, cerrado)["difference"], -ESPERADO)

    def test_e_todo_en_cero_equivale_a_lista_vacia(self):
        db, shift = _nuevo_turno()
        body = ShiftClose(denominations=[_line(100000, 0), _line(500, 0)], close_note="Sin efectivo")
        cerrado = _cerrar(db, shift, body)
        self.assertEqual(cerrado.counted_amount, Decimal("0"))
        self.assertEqual(_cuenta_filas(db, shift), 0)

    def test_e_sin_conteo_ya_no_existe_sin_observacion_es_422(self):
        """D-2: el cierre 'sin conteo' desapareció; $0 contra un esperado exige nota."""
        db, shift = _nuevo_turno()
        with self.assertRaises(HTTPException) as ctx:
            _cerrar(db, shift, ShiftClose())
        self.assertEqual(ctx.exception.status_code, 422)
        _intacto(self, db, shift)

    def test_f_registra_quien_cerro_y_es_distinto_de_quien_abrio(self):
        """FR-029 / D-5: `closed_by_*` es el usuario autenticado que confirma el conteo."""
        db, shift = _nuevo_turno()
        luis = fx.make_user_double(name="Luis")
        body = ShiftClose(denominations=[_line(100000, 1), _line(10000, 1), _line(5000, 1)])

        cerrado = _cerrar(db, shift, body, user=luis)

        self.assertEqual(cerrado.closed_by_user_id, luis.id)
        self.assertEqual(cerrado.closed_by_user_name, "Luis")
        self.assertNotEqual(cerrado.closed_by_user_id, cerrado.user_id)
        self.assertEqual(cerrado.user_name, "Cajero de prueba")

    def test_el_turno_abierto_no_tiene_closed_by(self):
        db, shift = _nuevo_turno()
        self.assertIsNone(shift.closed_by_user_id)
        self.assertIsNone(shift.closed_by_user_name)


class TestRechazosDelCierre(unittest.TestCase):
    def _rechaza(self, body, esperado_en_detalle):
        db, shift = _nuevo_turno()
        with self.assertRaises(HTTPException) as ctx:
            _cerrar(db, shift, body)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIsInstance(ctx.exception.detail, str)
        self.assertIn(esperado_en_detalle, ctx.exception.detail)
        _intacto(self, db, shift)

    def test_a_un_valor_monetario_del_cliente_se_rechaza(self):
        """US2-4 / D-1: `counted_amount`, `total`, `difference` o `subtotal` con valor."""
        lineas = [{"denomination": 100000, "quantity": 1}, {"denomination": 10000, "quantity": 1}]
        for campo in ("counted_amount", "total", "difference", "subtotal"):
            with self.subTest(campo=campo):
                body = ShiftClose.model_validate({campo: 95000, "denominations": lineas})
                self._rechaza(body, "Recarga la página")

    def test_a_subtotal_o_total_dentro_de_una_linea_tambien_se_rechaza(self):
        for campo in ("subtotal", "total"):
            with self.subTest(campo=campo):
                body = ShiftClose.model_validate({
                    "denominations": [{"denomination": 10000, "quantity": 1, campo: 10000}],
                })
                self._rechaza(body, "Recarga la página")

    def test_a_el_mensaje_es_el_del_contrato(self):
        body = ShiftClose.model_validate({"counted_amount": 1})
        self._rechaza(
            body,
            "La aplicación está desactualizada: el total contado ahora lo calcula el servidor.",
        )

    def test_b_counted_amount_nulo_no_se_rechaza(self):
        """research.md R4: `null` no 'trae un valor monetario'."""
        db, shift = _nuevo_turno()
        body = ShiftClose.model_validate({
            "counted_amount": None,
            "denominations": [{"denomination": 100000, "quantity": 1},
                              {"denomination": 10000, "quantity": 1},
                              {"denomination": 5000, "quantity": 1}],
        })
        self.assertEqual(_cerrar(db, shift, body).status, "closed")

    def test_b_otras_claves_desconocidas_se_ignoran_como_hoy(self):
        db, shift = _nuevo_turno()
        body = ShiftClose.model_validate({
            "campo_inventado": "x",
            "denominations": [{"denomination": 100000, "quantity": 1},
                              {"denomination": 10000, "quantity": 1},
                              {"denomination": 5000, "quantity": 1, "etiqueta": "y"}],
        })
        self.assertEqual(_cerrar(db, shift, body).status, "closed")

    def test_c_denominacion_fuera_del_catalogo(self):
        """US2-1 / FR-004: 3000 no está en el catálogo -> 422 que nombra $3.000."""
        self._rechaza(ShiftClose(denominations=[_line(3000, 1)]), "$3.000")

    def test_c_denominacion_fuera_del_catalogo_aunque_la_cantidad_sea_cero(self):
        self._rechaza(ShiftClose(denominations=[_line(3000, 0)]), "$3.000")

    def test_d_denominacion_repetida(self):
        """US2-3 / FR-006: 10000 y 10000.00 son la misma denominación."""
        body = ShiftClose(denominations=[_line(10000, 1), _line("10000.00", 2)])
        self._rechaza(body, "repetida")

    def test_d_repetida_tambien_cuando_una_trae_cantidad_cero(self):
        body = ShiftClose(denominations=[_line(10000, 0), _line("10000.00", 2)])
        self._rechaza(body, "repetida")

    def test_e_cantidad_enorme_excede_el_maximo_y_nunca_es_500(self):
        """Edge case: 100000 × 10⁹ = 10¹⁴ > máximo de Numeric(12,2)."""
        body = ShiftClose(denominations=[_line(100000, 1_000_000_000)])
        self._rechaza(body, "excede el monto máximo")

    def test_f_nueve_validas_y_una_invalida_no_guardan_ninguna(self):
        """US2-5 / FR-007: todo o nada."""
        validas = [_line(v, 1) for v in (100000, 50000, 20000, 10000, 5000, 2000, 1000, 500, 200)]
        self._rechaza(ShiftClose(denominations=validas + [_line(3000, 1)]), "$3.000")

    def test_g_cantidad_negativa_o_decimal_la_rechaza_pydantic(self):
        """US2-2 / FR-005: estructurales, formato por defecto de FastAPI."""
        for cantidad in (-1, 2.5):
            with self.subTest(cantidad=cantidad):
                with self.assertRaises(ValidationError):
                    DenominationIn(denomination=Decimal("10000"), quantity=cantidad)

    def test_h_el_rechazo_del_cliente_va_antes_que_el_bloqueo_por_pedidos_activos(self):
        """research.md R4: 422 y no 409 si hay pedidos activos Y un total del cliente."""
        db, shift = _nuevo_turno()
        ts = fx.make_table_session(db)
        fx.make_customer_order(db, ts, order_type="DINE_IN", status="abierta",
                               customer_name="Cliente de prueba")
        db.commit()
        body = ShiftClose.model_validate({"counted_amount": 95000})
        with self.assertRaises(HTTPException) as ctx:
            _cerrar(db, shift, body)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("Recarga la página", ctx.exception.detail)

    def test_los_pedidos_activos_siguen_bloqueando_con_409(self):
        """Spec 119 (FR-007) intacta: cuerpo válido + pedido activo -> 409."""
        from fastapi.responses import JSONResponse
        db, shift = _nuevo_turno()
        ts = fx.make_table_session(db)
        fx.make_customer_order(db, ts, order_type="DINE_IN", status="abierta",
                               customer_name="Cliente de prueba")
        db.commit()
        body = ShiftClose(denominations=[_line(100000, 1), _line(10000, 1), _line(5000, 1)])
        resp = _cerrar(db, shift, body)
        self.assertIsInstance(resp, JSONResponse)
        self.assertEqual(resp.status_code, 409)
        _intacto(self, db, shift)

    def test_turno_inexistente_es_404(self):
        import uuid
        db, _ = _nuevo_turno()
        with self.assertRaises(HTTPException) as ctx:
            router.close_shift(uuid.uuid4(), ShiftClose(), db, fx.make_user_double())
        self.assertEqual(ctx.exception.status_code, 404)


class TestInmutabilidadYConcurrencia(unittest.TestCase):
    def _cerrar_caso_1(self):
        db, shift = _nuevo_turno()
        body = ShiftClose(denominations=[_line(100000, 1), _line(10000, 1), _line(5000, 1)])
        cerrado = _cerrar(db, shift, body, user=fx.make_user_double(name="Luis"))
        return db, shift, cerrado

    @staticmethod
    def _foto(db, shift):
        db.expire_all()
        fresco = db.get(CashShift, shift.id)
        return {
            "counted": fresco.counted_amount,
            "note": fresco.close_note,
            "closed_by": (fresco.closed_by_user_id, fresco.closed_by_user_name),
            "closed_at": fresco.closed_at,
            "difference": service.reconcile(db, fresco)["difference"],
            "filas": [(f.denomination, f.kind, f.quantity) for f in _filas(db, shift)],
        }

    def test_a_reenviar_el_cierre_es_409_y_deja_todo_identico(self):
        """US6-1 / FR-018."""
        db, shift, _ = self._cerrar_caso_1()
        antes = self._foto(db, shift)

        with self.assertRaises(HTTPException) as ctx:
            _cerrar(db, shift, ShiftClose(denominations=[_line(50000, 9)], close_note="otra"),
                    user=fx.make_user_double(name="Otro"))

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail, "El turno ya está cerrado")
        self.assertEqual(self._foto(db, shift), antes)

    def test_b_reintentos_seguidos_no_crean_lineas_nuevas(self):
        """SC-006."""
        db, shift, _ = self._cerrar_caso_1()
        for _i in range(5):
            with self.assertRaises(HTTPException):
                _cerrar(db, shift, ShiftClose(denominations=[_line(100000, 1)]))
        self.assertEqual(_cuenta_filas(db, shift), 3)

    def test_c_un_choque_contra_el_indice_unico_es_409_y_no_deja_el_turno_a_medias(self):
        """FR-019: `IntegrityError` en el `commit` -> 409 (nunca 500) y rollback."""
        db, shift = _nuevo_turno()
        body = ShiftClose(denominations=[_line(100000, 1), _line(10000, 1), _line(5000, 1)])
        error = IntegrityError("INSERT", {}, Exception("duplicate key"))

        with mock.patch.object(db, "commit", side_effect=error):
            with self.assertRaises(HTTPException) as ctx:
                _cerrar(db, shift, body)

        self.assertEqual(ctx.exception.status_code, 409)
        _intacto(self, db, shift)

    def test_d_ninguna_ruta_edita_ni_borra_el_catalogo_ni_las_lineas(self):
        """US6-2 / FR-017 / FR-011: solo lectura sobre denominaciones."""
        for ruta in router.router.routes:
            if "denomination" in ruta.path:
                self.assertEqual(set(ruta.methods) - {"HEAD"}, {"GET"}, ruta.path)
        mutaciones = {
            (m, r.path) for r in router.router.routes for m in r.methods
            if m in {"PUT", "PATCH", "DELETE"}
        }
        self.assertFalse([p for m, p in mutaciones if "shifts" in p and "close" in p])

    def test_get_shift_for_update_404(self):
        import uuid
        db, _ = _nuevo_turno()
        with self.assertRaises(HTTPException) as ctx:
            service.get_shift_for_update(db, uuid.uuid4())
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail, "Shift not found")


if __name__ == "__main__":
    unittest.main()
