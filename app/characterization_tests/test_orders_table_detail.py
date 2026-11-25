"""`GET /orders/tables/{dining_table_id}/detail` -- endpoint agregado para el
detalle de una mesa en la Terminal (mesa + sesión activa + pedidos), que
reemplaza 3 peticiones sueltas que siempre se pedían juntas
(`/orders/tables?dining_table_id=`, `/table-sessions` sin filtro + buscar la
propia, `/orders?dining_table_id=`).

Invoca la función del router directamente (mismo patrón que
`test_orders_pagination.py`): `Depends(...)` no se resuelve fuera de un
request ASGI real.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_orders_table_detail -v
"""
from datetime import datetime
from decimal import Decimal
import unittest
from unittest import mock
from uuid import uuid4

from fastapi import HTTPException

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import router as orders_router

PRECIO = Decimal("8000")


def _call(table_id, db):
    # `fx.make_tenant_double()` no trae un `Plan` real detrás -- mismo mock
    # que ya usa `test_orders_pagination.py`/`test_orders_kitchen.py` para
    # `_decorate_orders` (spec 157, A-160).
    with mock.patch.object(
        orders_router.plan_service, "get_qr_payment_instruction_mode",
        return_value="PAY_AFTER_RECEIVING",
    ):
        return orders_router.table_detail(
            table_id, db=db, tenant=fx.make_tenant_double(), _=fx.make_user_double(),
        )


class TestOrdersTableDetail(unittest.TestCase):
    def test_mesa_con_sesion_activa_y_pedidos(self):
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table, status="active")
        order = fx.make_customer_order(db, ts, created_at=datetime(2026, 9, 1, 12, 0))
        db.commit()

        result = _call(table.id, db)

        self.assertEqual(result.table.id, table.id)
        self.assertIsNotNone(result.session)
        self.assertEqual(result.session.id, ts.id)
        self.assertEqual([o.id for o in result.orders], [order.id])

    def test_mesa_libre_sin_sesion_devuelve_session_null_y_orders_vacio(self):
        db = fx.new_session()
        table = fx.make_dining_table(db)
        db.commit()

        result = _call(table.id, db)

        self.assertEqual(result.table.id, table.id)
        self.assertIsNone(result.session)
        self.assertEqual(result.orders, [])

    def test_mesa_con_sesion_cerrada_tambien_devuelve_session_null(self):
        """Una sesión `closed` no es la sesión "activa" de la mesa -- la mesa
        puede haberse reabierto por QR sin sesión todavía, o estar libre."""
        db = fx.new_session()
        table = fx.make_dining_table(db)
        fx.make_table_session(db, table=table, status="closed")
        db.commit()

        result = _call(table.id, db)

        self.assertIsNone(result.session)

    def test_mesa_inexistente_404(self):
        db = fx.new_session()
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            _call(uuid4(), db)

        self.assertEqual(ctx.exception.status_code, 404)

    def test_no_mezcla_pedidos_de_otra_mesa(self):
        db = fx.new_session()
        table_a = fx.make_dining_table(db)
        ts_a = fx.make_table_session(db, table=table_a, status="active")
        order_a = fx.make_customer_order(db, ts_a, created_at=datetime(2026, 9, 1, 12, 0))
        table_b = fx.make_dining_table(db)
        ts_b = fx.make_table_session(db, table=table_b, status="active")
        fx.make_customer_order(db, ts_b, created_at=datetime(2026, 9, 1, 13, 0))
        db.commit()

        result = _call(table_a.id, db)

        self.assertEqual([o.id for o in result.orders], [order_a.id])


if __name__ == "__main__":
    unittest.main()
