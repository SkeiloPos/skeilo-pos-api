"""Semántica de los filtros `status` y `order_type` de la rama paginada de
`GET /orders` (spec 079, US2 — FR-011 / FR-016 / FR-018, data-model.md §3-§4).

Reescrito spec 147: con el colapso a 3 estados (`pendiente`, `entregada`,
`cancelada`), `status` deja de ser un "estado que ve la persona" derivado de
si ya existe una `Sale` -- ahora filtra directamente por el `status` crudo
(FR-012/FR-013: ningún campo de respuesta infiere una etiqueta que no sea uno
de los 3 valores válidos). El pago (`payment_status`) es un eje
completamente separado, filtrado aparte por `entregadas_sin_cobrar`
(FR-014/SC-003), no por `status`. `order_type` no cambia: filtra por la
columna homónima y las órdenes históricas con `order_type IS NULL` quedan
fuera de cualquier tipo concreto y dentro de "Todos" (lógica ternaria de SQL,
sin `COALESCE`).

Invoca `router.list_orders` directamente (ver `test_orders_pagination.py`).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_orders_status_type_filters -v
"""
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock
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
    # Spec 157 (A-160): mockea `get_qr_payment_instruction_mode` -- la
    # `tenant` doble de este módulo no trae un `Plan` real detrás (mismo
    # patrón que `test_orders_pagination.py`).
    with mock.patch.object(
        orders_router.plan_service, "get_qr_payment_instruction_mode",
        return_value="PAY_AFTER_RECEIVING",
    ):
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
            # Spec 147: default `Query(False, ...)` -- llamado directo sin pasar
            # por FastAPI necesita el valor explícito (mismo gotcha ya conocido
            # para los otros params Query de este router).
            entregadas_sin_cobrar=False,
            db=db,
            tenant=fx.make_tenant_double(),
            _=fx.make_user_double(),
        )
    return {o.id for o in result["items"]}


class _Base(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.ts = fx.make_table_session(self.db)
        self._clock = datetime(2026, 9, 1, 8, 0, 0)

    def _order(self, *, status="pendiente", order_type="DINE_IN", con_sale=False):
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
    """Reescrita spec 147: `status` ya filtra directo por el valor crudo, sin
    derivar nada de la existencia de una `Sale` -- esa derivación (la vieja
    "pagada") ya no existe como estado de pedido; el pago es
    `payment_status`, un eje aparte (filtrado por `entregadas_sin_cobrar`,
    no por `status`, ver `test_orders_pagination.py`)."""

    def test_pendiente_solo_pendientes_sin_importar_el_pago(self):
        pendiente = self._order(status="pendiente")
        # `con_sale` ya no saca a la orden de 'pendiente' -- el pago es un eje
        # aparte, independiente de status (FR-003).
        pendiente_pagada = self._order(status="pendiente", con_sale=True)
        self._order(status="entregada")
        self.db.commit()

        self.assertEqual(_ids(self.db, status="pendiente"), {pendiente.id, pendiente_pagada.id})

    def test_entregada_incluye_pagadas_y_sin_pagar(self):
        entregada_sin_pagar = self._order(status="entregada")
        entregada_pagada = self._order(status="entregada", con_sale=True)
        self.db.commit()

        self.assertEqual(
            _ids(self.db, status="entregada"), {entregada_sin_pagar.id, entregada_pagada.id},
        )

    def test_cancelada_solo_canceladas(self):
        cancelada = self._order(status="cancelada")
        self._order(status="entregada")
        self.db.commit()

        self.assertEqual(_ids(self.db, status="cancelada"), {cancelada.id})

    def test_sin_filtro_incluye_todos_los_estados(self):
        pendiente = self._order(status="pendiente")
        entregada = self._order(status="entregada")
        cancelada = self._order(status="cancelada")
        self.db.commit()

        self.assertEqual(_ids(self.db), {pendiente.id, entregada.id, cancelada.id})

    def test_status_invalido_da_422(self):
        self._order(status="entregada")
        self.db.commit()

        # Spec 147: los valores retirados ('abierta', 'bloqueada', 'pagada')
        # ya no son aceptados por el filtro -- el enum cerrado tiene 3
        # valores, no 5.
        with self.assertRaises(HTTPException) as ctx:
            _ids(self.db, status="abierta")
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
        objetivo = self._order(status="pendiente", order_type="TAKEAWAY")
        self._order(status="pendiente", order_type="DINE_IN")
        self._order(status="cancelada", order_type="TAKEAWAY")
        self.db.commit()

        self.assertEqual(
            _ids(self.db, status="pendiente", order_type=OrderType.TAKEAWAY),
            {objetivo.id},
        )


if __name__ == "__main__":
    unittest.main()
