"""Comportamiento NUEVO (spec 119, FR-007/FR-008/FR-009): cerrar un turno de
caja con pedidos activos sin cobrar pasa de estar permitido hoy a quedar
bloqueado con 409 -- cita A-129 de `registro-de-anomalias.md`. Antes de esta
spec ninguna regla de `specs/006-turnos-caja-arqueo` (`RN-CASH-01` a
`RN-CASH-18`) validaba esto (research.md D6); el resto de `close_shift`
corre exactamente igual que antes cuando no hay pedidos activos.

Invoca `app/api/v1/cash/router.py::close_shift` directamente como función
Python (mismo patrón que `test_cash_shift_sales_report.py`).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_cash_close_active_orders -v
"""
import json
import unittest
from datetime import datetime
from decimal import Decimal

from fastapi.responses import JSONResponse

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.cash import router
from app.api.v1.cash.schemas import ShiftClose


def _seed_active_order(db, *, order_type: str, status: str = "abierta", **kw):
    """Pedido activo de cualquier canal. `TAKEAWAY`/`DELIVERY` no tienen
    mesa/sesión real (research.md D1) -- se crea una sesión descartable solo
    para satisfacer la firma de la factory y se anula explícitamente."""
    ts = fx.make_table_session(db)
    kw.setdefault("customer_name", "Cliente de prueba")
    if order_type == "DINE_IN":
        order = fx.make_customer_order(db, ts, order_type="DINE_IN", status=status, **kw)
    else:
        order = fx.make_customer_order(
            db, ts, table_session_id=None, dining_table_id=None,
            order_type=order_type, status=status, **kw,
        )
    db.flush()
    return order


class TestBloqueoDeCierrePorPedidosActivos(unittest.TestCase):
    def test_cierre_bloqueado_con_pedidos_activos_de_los_tres_canales(self):
        """A-129/FR-007/FR-008 (Acceptance Scenario 1 de US3): un pedido
        activo de cada canal (`DINE_IN`, `TAKEAWAY`, `DELIVERY`) bloquea el
        cierre con 409 y la lista de los 3; el turno sigue `open`."""
        db = fx.new_session()
        register = fx.make_cash_register(db)
        shift = fx.make_cash_shift(db, register=register)
        db.commit()

        o1 = _seed_active_order(db, order_type="DINE_IN", status="abierta")
        o2 = _seed_active_order(db, order_type="TAKEAWAY", status="recibida")
        o3 = _seed_active_order(db, order_type="DELIVERY", status="bloqueada")
        db.commit()

        resp = router.close_shift(shift.id, ShiftClose(), db)

        self.assertIsInstance(resp, JSONResponse)
        self.assertEqual(resp.status_code, 409)
        body = json.loads(resp.body)
        self.assertIn("pedidos activos", body["detail"])
        ids = {row["id"] for row in body["active_orders"]}
        self.assertEqual(ids, {str(o1.id), str(o2.id), str(o3.id)})

        db.refresh(shift)
        self.assertEqual(shift.status, "open")
        self.assertIsNone(shift.closed_at)

    def test_cierre_se_completa_tras_cobrar_uno_y_anular_el_otro(self):
        """Acceptance Scenario 2 de US3: resolver (cobrar o anular) cada
        pedido activo desbloquea el cierre; a partir de ahí el reporte del
        turno queda disponible como snapshot (`is_live=False`)."""
        db = fx.new_session()
        register = fx.make_cash_register(db)
        shift = fx.make_cash_shift(db, register=register)
        db.commit()
        o1 = _seed_active_order(db, order_type="DINE_IN", status="abierta")
        o2 = _seed_active_order(db, order_type="TAKEAWAY", status="recibida")
        db.commit()

        bloqueado = router.close_shift(shift.id, ShiftClose(), db)
        self.assertIsInstance(bloqueado, JSONResponse)

        o1.status = "pagada"
        o2.status = "cancelada"
        db.commit()

        activos = router.list_active_orders_endpoint(db)
        self.assertEqual(activos, [])

        cerrado = router.close_shift(shift.id, ShiftClose(), db)
        self.assertFalse(isinstance(cerrado, JSONResponse))
        self.assertEqual(cerrado.status, "closed")

        reporte = router.shift_sales_report(shift.id, db)
        self.assertEqual(reporte["is_live"], False)

    def test_turno_sin_pedidos_activos_cierra_sin_ningun_bloqueo(self):
        """Acceptance Scenario 3 de US3: comportamiento SIN CAMBIOS respecto
        a `specs/006-turnos-caja-arqueo` cuando no hay pedidos activos."""
        db = fx.new_session()
        register = fx.make_cash_register(db)
        shift = fx.make_cash_shift(db, register=register)
        db.commit()

        resultado = router.close_shift(shift.id, ShiftClose(), db)

        self.assertFalse(isinstance(resultado, JSONResponse))
        self.assertEqual(resultado.status, "closed")

    def test_reevaluacion_en_un_segundo_intento_tras_resolver_el_pedido(self):
        """Edge case de `spec.md`: un pedido se resuelve ENTRE dos intentos
        de cierre -- la segunda llamada vuelve a consultar el estado
        vigente, sin caché del primer intento (contracts/active-orders-
        close-gate-api.md)."""
        db = fx.new_session()
        register = fx.make_cash_register(db)
        shift = fx.make_cash_shift(db, register=register)
        db.commit()
        orden = _seed_active_order(db, order_type="DINE_IN", status="abierta")
        db.commit()

        primero = router.close_shift(shift.id, ShiftClose(), db)
        self.assertIsInstance(primero, JSONResponse)

        orden.status = "pagada"
        db.commit()

        segundo = router.close_shift(shift.id, ShiftClose(), db)
        self.assertFalse(isinstance(segundo, JSONResponse))
        self.assertEqual(segundo.status, "closed")

    def test_pedido_activo_sin_mesa_ni_cliente_tambien_bloquea(self):
        """Edge case de `spec.md`: un pedido activo que no se ancla a
        ninguna mesa (`TAKEAWAY`/`DELIVERY`) cuenta igual para el bloqueo,
        sin importar el canal (research.md D1: alcance tenant-wide, no por
        caja)."""
        db = fx.new_session()
        register_a = fx.make_cash_register(db, name="Caja A")
        register_b = fx.make_cash_register(db, name="Caja B")
        shift_b = fx.make_cash_shift(db, register=register_b)
        db.commit()
        # El pedido activo no tiene relación alguna con `register_b` ni con
        # ninguna caja -- research.md D1 acepta esta consecuencia.
        _seed_active_order(db, order_type="DELIVERY", status="recibida")
        db.commit()

        resultado = router.close_shift(shift_b.id, ShiftClose(), db)
        self.assertIsInstance(resultado, JSONResponse)
        self.assertEqual(resultado.status_code, 409)


if __name__ == "__main__":
    unittest.main()
