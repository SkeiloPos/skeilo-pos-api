"""Semántica de los filtros nuevos `date_from`, `date_to` y `q` de la rama
paginada de `GET /orders` (spec 120, research.md §1-§2, contracts/orders-list-api.md).

A diferencia de los demás archivos de `characterization_tests/`, este no
congela comportamiento *heredado*: `date_from`/`date_to`/`q` son parámetros
opcionales **nuevos**, aditivos y retrocompatibles — ningún llamador existente
(Terminal de Mesas, Dashboard) los envía, así que su ausencia no cambia nada
de lo que ya protege `test_orders_status_type_filters.py` /
`test_orders_pagination.py` (esos archivos no se tocan, Principio III).

`date_from`/`date_to` filtran sobre `created_at` (inclusive en ambos
extremos). `q` hace *match* insensible a mayúsculas/minúsculas contra
`customer_name` **o** contra la representación en texto de `id` — el mismo
criterio que `orderDisplayCode()` usa en el frontend para el código corto
visible (`order-status.util.ts`).

Invoca `router.list_orders` directamente, igual que
`test_orders_status_type_filters.py`.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_orders_date_search_filters -v
"""
import unittest
from datetime import date, datetime
from types import SimpleNamespace

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import router as orders_router
from app.api.v1.orders.schemas import OrderType


class _FakeRequest:
    def __init__(self):
        self.headers = {}
        self.client = SimpleNamespace(host="127.0.0.1")


def _ids(db, *, date_from=None, date_to=None, q=None, status=None, order_type=None):
    result = orders_router.list_orders(
        _FakeRequest(),
        status_filter=status,
        order_type=order_type,
        active_sessions_only=False,
        page=1,
        size=50,
        date_from=date_from,
        date_to=date_to,
        q=q,
        db=db,
        _=fx.make_user_double(),
    )
    return {o.id for o in result["items"]}


class _Base(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.ts = fx.make_table_session(self.db)

    def _order(self, *, created_at, customer_name=None, status="abierta", order_type="DINE_IN"):
        order = fx.make_customer_order(
            self.db, self.ts,
            status=status, order_type=order_type,
            created_at=created_at, customer_name=customer_name,
        )
        self.db.flush()
        return order


class TestFiltroDeFecha(_Base):

    def test_date_from_excluye_lo_anterior(self):
        anterior = self._order(created_at=datetime(2026, 10, 13, 10, 0))
        dentro = self._order(created_at=datetime(2026, 10, 15, 10, 0))
        self.db.commit()

        ids = _ids(self.db, date_from=date(2026, 10, 14))
        self.assertIn(dentro.id, ids)
        self.assertNotIn(anterior.id, ids)

    def test_date_to_es_inclusive_hasta_el_fin_del_dia(self):
        limite = self._order(created_at=datetime(2026, 10, 14, 23, 59, 59))
        siguiente_dia = self._order(created_at=datetime(2026, 10, 15, 0, 0, 1))
        self.db.commit()

        ids = _ids(self.db, date_to=date(2026, 10, 14))
        self.assertIn(limite.id, ids)
        self.assertNotIn(siguiente_dia.id, ids)

    def test_date_from_y_date_to_acotan_un_solo_dia(self):
        ayer = self._order(created_at=datetime(2026, 10, 14, 12, 0))
        hoy = self._order(created_at=datetime(2026, 10, 15, 12, 0))
        manana = self._order(created_at=datetime(2026, 10, 16, 12, 0))
        self.db.commit()

        ids = _ids(self.db, date_from=date(2026, 10, 15), date_to=date(2026, 10, 15))
        self.assertEqual(ids, {hoy.id})
        self.assertNotIn(ayer.id, ids)
        self.assertNotIn(manana.id, ids)

    def test_sin_date_from_ni_date_to_no_filtra_por_fecha(self):
        a = self._order(created_at=datetime(2020, 1, 1, 0, 0))
        b = self._order(created_at=datetime(2026, 10, 15, 0, 0))
        self.db.commit()

        self.assertEqual(_ids(self.db), {a.id, b.id})


class TestBusquedaQ(_Base):

    def test_q_coincide_por_nombre_de_cliente_insensible_a_mayusculas(self):
        objetivo = self._order(created_at=datetime.now(), customer_name="María Restrepo")
        self._order(created_at=datetime.now(), customer_name="Carlos Mejía")
        self.db.commit()

        self.assertEqual(_ids(self.db, q="maría"), {objetivo.id})
        self.assertEqual(_ids(self.db, q="RESTREPO"), {objetivo.id})

    def test_q_coincide_por_fragmento_del_id(self):
        objetivo = self._order(created_at=datetime.now())
        self._order(created_at=datetime.now())
        self.db.commit()

        fragmento = str(objetivo.id).replace("-", "")[-6:]
        self.assertEqual(_ids(self.db, q=fragmento), {objetivo.id})

    def test_q_vacio_o_solo_espacios_no_filtra(self):
        a = self._order(created_at=datetime.now(), customer_name="Ana")
        b = self._order(created_at=datetime.now(), customer_name="Beto")
        self.db.commit()

        self.assertEqual(_ids(self.db, q=""), {a.id, b.id})
        self.assertEqual(_ids(self.db, q="   "), {a.id, b.id})

    def test_q_sin_coincidencias_devuelve_vacio(self):
        self._order(created_at=datetime.now(), customer_name="Ana")
        self.db.commit()

        self.assertEqual(_ids(self.db, q="nadie-coincide-con-esto"), set())


class TestCombinacionConFiltrosExistentes(_Base):

    def test_q_y_fecha_se_combinan_con_status_y_order_type_con_and(self):
        objetivo = self._order(
            created_at=datetime(2026, 10, 15, 9, 0),
            customer_name="Deimer Hernández",
            status="abierta", order_type="TAKEAWAY",
        )
        self._order(
            created_at=datetime(2026, 10, 15, 9, 0),
            customer_name="Deimer Hernández",
            status="cancelada", order_type="TAKEAWAY",
        )
        self._order(
            created_at=datetime(2026, 10, 10, 9, 0),
            customer_name="Deimer Hernández",
            status="abierta", order_type="TAKEAWAY",
        )
        self.db.commit()

        ids = _ids(
            self.db, q="deimer",
            date_from=date(2026, 10, 15), date_to=date(2026, 10, 15),
            status="abierta", order_type=OrderType.TAKEAWAY,
        )
        self.assertEqual(ids, {objetivo.id})


if __name__ == "__main__":
    unittest.main()
