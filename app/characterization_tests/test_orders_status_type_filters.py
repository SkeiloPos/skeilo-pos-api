"""Semántica de los filtros `status` y `order_type` de la rama paginada de
`GET /orders` (spec 079, US2 — FR-011 / FR-016 / FR-018, data-model.md §3-§4).

`status` es el "estado que ve la persona" (port de `displayOrderStatus()` al
servidor), **no** el `status` crudo: una orden `abierta` con `Sale` emitida cae
bajo `pagada`; una `pagada` cruda sin `Sale` también; una cancelada con venta
previa nunca. `order_type` filtra por la columna homónima y las órdenes
históricas con `order_type IS NULL` quedan fuera de cualquier tipo concreto y
dentro de "Todos" (lógica ternaria de SQL, sin `COALESCE`).

Invoca `router.list_orders` directamente (ver `test_orders_pagination.py`).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_orders_status_type_filters -v
"""
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import router as orders_router
from app.api.v1.orders.schemas import OrderType
from app.models.sale import Sale


class _FakeRequest:
    def __init__(self):
        self.headers = {}
        self.client = SimpleNamespace(host="127.0.0.1")


def _ids(db, *, status=None, order_type=None):
    result = orders_router.list_orders(
        _FakeRequest(),
        status_filter=status,
        order_type=order_type,
        active_sessions_only=False,
        page=1,
        size=50,
        date_from=None,
        date_to=None,
        q=None,
        db=db,
        _=fx.make_user_double(),
    )
    return {o.id for o in result["items"]}


class _Base(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.ts = fx.make_table_session(self.db)
        self._clock = datetime(2026, 9, 1, 8, 0, 0)

    def _order(self, *, status="abierta", order_type="DINE_IN", con_sale=False):
        self._clock += timedelta(minutes=1)
        order = fx.make_customer_order(
            self.db, self.ts, status=status, order_type=order_type, created_at=self._clock,
        )
        if con_sale:
            shift = fx.make_cash_shift(self.db)
            self.db.add(Sale(
                cash_shift_id=shift.id, customer_order_id=order.id,
                table_session_id=self.ts.id, user_id=uuid4(), status="paid",
            ))
        self.db.flush()
        return order


class TestFiltroDeEstado(_Base):

    def test_recibida_solo_recibidas_sin_venta(self):
        recibida = self._order(status="recibida")
        self._order(status="recibida", con_sale=True)  # ya tiene venta → "pagada"
        self._order(status="abierta")
        self.db.commit()

        self.assertEqual(_ids(self.db, status="recibida"), {recibida.id})

    def test_abierta_excluye_las_que_ya_tienen_venta(self):
        abierta = self._order(status="abierta")
        con_venta = self._order(status="abierta", con_sale=True)
        self.db.commit()

        self.assertEqual(_ids(self.db, status="abierta"), {abierta.id})
        self.assertNotIn(con_venta.id, _ids(self.db, status="abierta"))

    def test_bloqueada_excluye_las_que_ya_tienen_venta(self):
        bloqueada = self._order(status="bloqueada")
        self._order(status="bloqueada", con_sale=True)
        self.db.commit()

        self.assertEqual(_ids(self.db, status="bloqueada"), {bloqueada.id})

    def test_pagada_incluye_abierta_con_venta_y_no_aparece_bajo_abierta(self):
        abierta_con_venta = self._order(status="abierta", con_sale=True)
        self.db.commit()

        self.assertIn(abierta_con_venta.id, _ids(self.db, status="pagada"))
        self.assertNotIn(abierta_con_venta.id, _ids(self.db, status="abierta"))

    def test_pagada_incluye_status_pagada_cruda_sin_sale(self):
        pagada_cruda = self._order(status="pagada")
        self.db.commit()

        self.assertIn(pagada_cruda.id, _ids(self.db, status="pagada"))

    def test_pagada_excluye_cancelada_con_venta_previa(self):
        cancelada_con_venta = self._order(status="cancelada", con_sale=True)
        self.db.commit()

        self.assertNotIn(cancelada_con_venta.id, _ids(self.db, status="pagada"))
        self.assertIn(cancelada_con_venta.id, _ids(self.db, status="cancelada"))

    def test_cancelada_solo_canceladas(self):
        cancelada = self._order(status="cancelada")
        self._order(status="abierta")
        self.db.commit()

        self.assertEqual(_ids(self.db, status="cancelada"), {cancelada.id})

    def test_sin_filtro_incluye_las_bloqueadas(self):
        bloqueada = self._order(status="bloqueada")
        abierta = self._order(status="abierta")
        self.db.commit()

        self.assertEqual(_ids(self.db), {bloqueada.id, abierta.id})

    def test_status_invalido_da_422(self):
        self._order(status="abierta")
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            _ids(self.db, status="entregada")
        self.assertEqual(ctx.exception.status_code, 422)


class TestFiltroDeTipo(_Base):

    def test_cada_tipo_concreto_devuelve_su_subconjunto(self):
        dine = self._order(order_type="DINE_IN")
        take = self._order(order_type="TAKEAWAY")
        deli = self._order(order_type="DELIVERY")
        self.db.commit()

        self.assertEqual(_ids(self.db, order_type=OrderType.DINE_IN), {dine.id})
        self.assertEqual(_ids(self.db, order_type=OrderType.TAKEAWAY), {take.id})
        self.assertEqual(_ids(self.db, order_type=OrderType.DELIVERY), {deli.id})

    def test_order_type_null_fuera_de_un_tipo_concreto_dentro_de_todos(self):
        sin_tipo = self._order(order_type=None)
        con_tipo = self._order(order_type="DINE_IN")
        self.db.commit()

        self.assertNotIn(sin_tipo.id, _ids(self.db, order_type=OrderType.DINE_IN))
        self.assertEqual(_ids(self.db), {sin_tipo.id, con_tipo.id})


class TestCombinacion(_Base):

    def test_status_y_order_type_se_intersecan_con_and(self):
        objetivo = self._order(status="abierta", order_type="TAKEAWAY")
        self._order(status="abierta", order_type="DINE_IN")
        self._order(status="cancelada", order_type="TAKEAWAY")
        self.db.commit()

        self.assertEqual(
            _ids(self.db, status="abierta", order_type=OrderType.TAKEAWAY),
            {objetivo.id},
        )


if __name__ == "__main__":
    unittest.main()
