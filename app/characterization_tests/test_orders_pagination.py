"""Contrato de paginación de `GET /orders` y `GET /orders/tables` (spec 079).

Invoca las funciones de endpoint directamente como funciones Python (mismo
patrón que `test_cart_router.py` / `test_table_sessions_router.py`: `Depends(...)`
solo se resuelve cuando FastAPI atiende una request real vía ASGI, así que se
pasan `db` / `_` (usuario) / `request` a mano).

Cubre:
  - **modo compatible** (sin `page` ni `size`): cuerpo `list[OrderResponse]`,
    `ETag` + `304`, `active_sessions_only` intacto, `status` crudo
    (FR-023 / FR-024);
  - **modo paginado**: envoltura `Page` con las 5 claves, `len(items) <= size`,
    defaults de `page` / `size`, orden determinista `created_at DESC, id DESC`
    página a página sin huecos ni repeticiones (SC-008), clamp de página fuera
    de rango (FR-005), `total` / `pages` del conjunto ya filtrado (FR-007), y
    una guardia de trabajo acotado por petición (nº de sentencias SQL constante
    — atrapa un N+1 al asignar `paid` / `staff_user_name` en bloque, SC-001 /
    SC-002);
  - **`GET /orders/tables`**: array idéntico a hoy sin parámetros; `Page` con
    orden `number ASC` y clamp con `page` / `size` (T021, FR-023 / FR-025).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_orders_pagination -v
"""
import json
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import event, select

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import router as orders_router
from app.api.v1.orders.schemas import OrderType
from app.models.customer_order import CustomerOrder
from app.models.dining_table import DiningTable
from app.models.sale import Sale

PRECIO = Decimal("10000")


class _FakeRequest:
    """Doble mínimo de `fastapi.Request`: solo `headers` (lo que lee
    `http_cache.json_or_304`)."""

    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}
        self.client = SimpleNamespace(host="127.0.0.1")


def _call_list_orders(db, *, status=None, order_type=None, active_sessions_only=False,
                      page=None, size=None, request=None):
    return orders_router.list_orders(
        request or _FakeRequest(),
        status_filter=status,
        order_type=order_type,
        active_sessions_only=active_sessions_only,
        page=page,
        size=size,
        date_from=None,
        date_to=None,
        q=None,
        db=db,
        _=fx.make_user_double(),
    )


def _call_list_tables(db, *, page=None, size=None):
    return orders_router.list_tables(page=page, size=size, db=db, _=fx.make_user_double())


def _seed_order(db, ts, *, created_at, status="abierta", order_type="DINE_IN", con_sale=False):
    # Sin `user_id`: `shared.users` no está en el esquema SQLite de los tests de
    # `orders` (`orders_fixtures._TABLE_NAMES`), igual que en el resto de la red.
    order = fx.make_customer_order(
        db, ts, status=status, order_type=order_type, created_at=created_at,
    )
    if con_sale:
        shift = fx.make_cash_shift(db)
        db.add(Sale(
            cash_shift_id=shift.id, customer_order_id=order.id,
            table_session_id=ts.id, user_id=uuid4(), status="paid",
        ))
    return order


class TestOrdersListModoCompatible(unittest.TestCase):
    """Sin `page` ni `size`: byte a byte lo de hoy (FR-023 / FR-024)."""

    def _seed(self, n=3):
        db = fx.new_session()
        ts = fx.make_table_session(db)
        base = datetime(2026, 9, 1, 12, 0, 0)
        for i in range(n):
            _seed_order(db, ts, created_at=base + timedelta(minutes=i))
        db.commit()
        return db

    def test_sin_page_ni_size_devuelve_array_con_etag_y_304(self):
        db = self._seed()

        resp = _call_list_orders(db)

        self.assertEqual(resp.status_code, 200)
        etag = resp.headers["ETag"]
        self.assertTrue(etag)
        body = json.loads(resp.body)
        self.assertIsInstance(body, list)
        self.assertEqual(len(body), 3)

        resp304 = _call_list_orders(db, request=_FakeRequest({"if-none-match": etag}))
        self.assertEqual(resp304.status_code, 304)

    def test_active_sessions_only_sigue_filtrando_como_hoy(self):
        db = fx.new_session()
        ts_cerrada = fx.make_table_session(db, status="closed")
        _seed_order(db, ts_cerrada, created_at=datetime(2026, 9, 1, 12, 0), con_sale=True)
        ts_activa = fx.make_table_session(db)
        vivo = _seed_order(db, ts_activa, created_at=datetime(2026, 9, 1, 13, 0))
        db.commit()

        resp = _call_list_orders(db, active_sessions_only=True)

        ids = [o["id"] for o in json.loads(resp.body)]
        self.assertEqual(ids, [str(vivo.id)])

    def test_status_crudo_se_conserva_en_modo_compatible(self):
        db = fx.new_session()
        ts = fx.make_table_session(db)
        abierta = _seed_order(db, ts, created_at=datetime(2026, 9, 1, 12, 0), status="abierta")
        _seed_order(db, ts, created_at=datetime(2026, 9, 1, 13, 0), status="bloqueada")
        db.commit()

        resp = _call_list_orders(db, status="abierta")

        ids = [o["id"] for o in json.loads(resp.body)]
        self.assertEqual(ids, [str(abierta.id)])

    def test_page_presente_con_active_sessions_only_no_da_400_y_la_bandera_se_ignora(self):
        db = fx.new_session()
        ts_cerrada = fx.make_table_session(db, status="closed")
        pagado_sesion_cerrada = _seed_order(
            db, ts_cerrada, created_at=datetime(2026, 9, 1, 12, 0), con_sale=True,
        )
        db.commit()

        result = _call_list_orders(db, page=1, active_sessions_only=True)

        # Es un Page (dict), no un array; la bandera no filtró nada.
        self.assertIn("items", result)
        self.assertEqual([o.id for o in result["items"]], [pagado_sesion_cerrada.id])


class TestOrdersListModoPaginado(unittest.TestCase):

    def _seed(self, n, *, size_hint=20):
        db = fx.new_session()
        ts = fx.make_table_session(db)
        base = datetime(2026, 9, 1, 12, 0, 0)
        orders = [
            _seed_order(db, ts, created_at=base + timedelta(minutes=i))
            for i in range(n)
        ]
        db.commit()
        return db, ts, orders

    def test_envoltura_page_con_las_cinco_claves_y_len_items_acotado(self):
        db, _, _ = self._seed(25)

        result = _call_list_orders(db, page=1, size=20)

        self.assertEqual(set(result.keys()), {"items", "total", "page", "size", "pages"})
        self.assertEqual(result["total"], 25)
        self.assertEqual(result["page"], 1)
        self.assertEqual(result["size"], 20)
        self.assertEqual(result["pages"], 2)
        self.assertLessEqual(len(result["items"]), 20)
        self.assertEqual(len(result["items"]), 20)

    def test_solo_page_toma_size_20_solo_size_toma_page_1(self):
        db, _, _ = self._seed(30)

        solo_page = _call_list_orders(db, page=2)
        self.assertEqual(solo_page["size"], 20)
        self.assertEqual(solo_page["page"], 2)

        solo_size = _call_list_orders(db, size=5)
        self.assertEqual(solo_size["page"], 1)
        self.assertEqual(solo_size["size"], 5)
        self.assertEqual(len(solo_size["items"]), 5)

    def test_orden_determinista_pagina_a_pagina_sin_huecos_ni_repetidos(self):
        # 12 órdenes, varias con `created_at` idéntico → el desempate por `id`
        # tiene que dar una secuencia total estable (SC-008, FR-003).
        db = fx.new_session()
        ts = fx.make_table_session(db)
        t1 = datetime(2026, 9, 1, 12, 0, 0)
        t2 = datetime(2026, 9, 1, 12, 5, 0)
        instants = [t1, t1, t1, t2, t2, t1, t2, t1, t2, t2, t1, t2]
        for inst in instants:
            _seed_order(db, ts, created_at=inst)
        db.commit()

        esperado = db.execute(
            select(CustomerOrder.id).order_by(
                CustomerOrder.created_at.desc(), CustomerOrder.id.desc(),
            )
        ).scalars().all()

        primera = _call_list_orders(db, page=1, size=5)
        recorrido = []
        for page in range(1, primera["pages"] + 1):
            result = _call_list_orders(db, page=page, size=5)
            recorrido.extend(o.id for o in result["items"])

        self.assertEqual(recorrido, esperado)
        self.assertEqual(len(recorrido), len(set(recorrido)))  # sin repetidos

    def test_clamp_pagina_fuera_de_rango_devuelve_ultima_con_resultados(self):
        db, _, _ = self._seed(23)

        result = _call_list_orders(db, page=999, size=10)

        self.assertEqual(result["page"], 3)
        self.assertEqual(result["pages"], 3)
        self.assertEqual(len(result["items"]), 3)

    def test_conjunto_vacio_devuelve_page_1_items_vacios_total_0_pages_0(self):
        db = fx.new_session()
        db.commit()

        result = _call_list_orders(db, page=5, size=20)

        self.assertEqual(result["page"], 1)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["pages"], 0)

    def test_total_y_pages_del_conjunto_ya_filtrado(self):
        db = fx.new_session()
        ts = fx.make_table_session(db)
        base = datetime(2026, 9, 1, 12, 0, 0)
        for i in range(4):
            _seed_order(db, ts, created_at=base + timedelta(minutes=i), order_type="DINE_IN")
        for i in range(6):
            _seed_order(db, ts, created_at=base + timedelta(minutes=10 + i), order_type="TAKEAWAY")
        db.commit()

        result = _call_list_orders(db, order_type=OrderType.TAKEAWAY, page=1, size=20)

        self.assertEqual(result["total"], 6)
        self.assertEqual(result["pages"], 1)

    def test_status_fuera_del_enum_cerrado_da_422(self):
        db, _, _ = self._seed(3)

        with self.assertRaises(HTTPException) as ctx:
            _call_list_orders(db, status="pendiente", page=1)
        self.assertEqual(ctx.exception.status_code, 422)

    def test_guardia_de_trabajo_acotado_numero_de_sentencias_constante(self):
        conteos = []
        resultados = []
        for n in (5, 25):
            db = fx.new_session()
            ts = fx.make_table_session(db)
            base = datetime(2026, 9, 1, 12, 0, 0)
            for i in range(n):
                _seed_order(db, ts, created_at=base + timedelta(minutes=i))
            db.commit()

            sentencias: list[str] = []
            bind = db.get_bind()

            def _rec(conn, cursor, statement, parameters, context, executemany):
                sentencias.append(statement)

            event.listen(bind, "before_cursor_execute", _rec)
            try:
                resultado = _call_list_orders(db, page=1, size=20)
            finally:
                event.remove(bind, "before_cursor_execute", _rec)
            conteos.append(len(sentencias))
            resultados.append(resultado)

        # 5 ítems vs 20 ítems en la página: el nº de sentencias no puede crecer
        # con el tamaño del lote (un N+1 en `paid`/`staff_user_name`/`total` lo
        # haría — spec 121 añade `service.order_totals` a este mismo punto, en
        # bloque, sin reabrir esta guardia).
        self.assertEqual(conteos[0], conteos[1])
        # spec 121 (FR-001/FR-002): `total` ya viaja poblado (no `None`) en esta
        # misma rama paginada, sin que eso mueva el conteo de sentencias de arriba.
        for resultado in resultados:
            self.assertTrue(all(o.total is not None for o in resultado["items"]))

    def test_paid_se_asigna_solo_sobre_los_items_de_la_pagina(self):
        db = fx.new_session()
        ts = fx.make_table_session(db)
        base = datetime(2026, 9, 1, 12, 0, 0)
        pagada = _seed_order(db, ts, created_at=base + timedelta(minutes=50), con_sale=True)
        for i in range(5):
            _seed_order(db, ts, created_at=base + timedelta(minutes=i))
        db.commit()

        result = _call_list_orders(db, page=1, size=20)

        by_id = {o.id: o for o in result["items"]}
        self.assertTrue(by_id[pagada.id].paid)
        self.assertFalse(any(o.paid for oid, o in by_id.items() if oid != pagada.id))

    def test_total_correcto_para_los_tres_estados_de_facturacion_en_el_listado(self):
        # spec 121 (FR-001/FR-002, contracts/orders-total-field.md): el `total`
        # del listado paginado debe coincidir con el mismo cálculo de 3 ramas
        # que ya usa `billing.total` en el detalle (`test_orders_billing_summary`).
        db = fx.new_session()
        ts = fx.make_table_session(db)
        variant = fx.make_variant(db, price=PRECIO)
        base = datetime(2026, 9, 1, 12, 0, 0)

        propia = fx.make_customer_order(db, ts, created_at=base)
        fx.make_order_item(db, propia, variant, unit_price=PRECIO, quantity=2)  # Σ=20.000
        fx.make_sale(
            db, order=propia, table_session_id=ts.id,
            subtotal=Decimal("20000"), discount=Decimal("2000"),
            delivery_fee=Decimal("0"), total=Decimal("18000"),
        )

        agrupada = fx.make_customer_order(db, ts, created_at=base + timedelta(minutes=1))
        fx.make_order_item(db, agrupada, variant, unit_price=PRECIO, quantity=1)  # Σ=10.000
        fx.make_sale(db, table_session_id=ts.id, subtotal=Decimal("99999"), total=Decimal("99999"))

        sin_factura = fx.make_customer_order(db, ts, created_at=base + timedelta(minutes=2))
        fx.make_order_item(db, sin_factura, variant, unit_price=PRECIO, quantity=3)  # Σ=30.000
        db.commit()

        result = _call_list_orders(db, page=1, size=20)

        by_id = {o.id: o for o in result["items"]}
        self.assertEqual(by_id[propia.id].total, Decimal("18000"))
        self.assertEqual(by_id[agrupada.id].total, Decimal("10000"))
        self.assertEqual(by_id[sin_factura.id].total, Decimal("30000"))
        # `billing` sigue sin calcularse en el listado (D3/D15) -- solo cambia `total`.
        # `getattr(..., None)`: el objeto ORM crudo no trae `billing` seteado
        # (solo lo haría `_load_order`, que esta rama no llama) -- mismo criterio
        # que `test_orders_billing_summary::test_d3_el_listado_devuelve_billing_en_null_en_todos_sus_items`.
        self.assertIsNone(getattr(by_id[propia.id], "billing", None))
        self.assertIsNone(getattr(by_id[agrupada.id], "billing", None))
        self.assertIsNone(getattr(by_id[sin_factura.id], "billing", None))


class TestTablesListPaginado(unittest.TestCase):
    """`GET /orders/tables` — T021 (FR-019 / FR-020 / FR-023 / FR-025)."""

    def _seed(self, n):
        db = fx.new_session()
        for _ in range(n):
            fx.make_dining_table(db)
        db.commit()
        return db

    def test_sin_page_ni_size_devuelve_array_ordenado_por_number(self):
        db = self._seed(5)

        result = _call_list_tables(db)

        self.assertIsInstance(result, list)
        numbers = [t.number for t in result]
        self.assertEqual(numbers, sorted(numbers))

    def test_page_y_size_devuelven_page_con_orden_number_asc(self):
        db = self._seed(23)

        result = _call_list_tables(db, page=1, size=20)

        self.assertEqual(set(result.keys()), {"items", "total", "page", "size", "pages"})
        self.assertEqual(result["total"], 23)
        self.assertEqual(result["pages"], 2)
        self.assertEqual(len(result["items"]), 20)
        numbers = [t.number for t in result["items"]]
        self.assertEqual(numbers, sorted(numbers))

    def test_clamp_y_conjunto_vacio_en_mesas(self):
        db = self._seed(15)
        fuera = _call_list_tables(db, page=99, size=10)
        self.assertEqual(fuera["page"], 2)
        self.assertEqual(len(fuera["items"]), 5)

        vacio = _call_list_tables(fx.new_session(), page=1, size=20)
        self.assertEqual(vacio["page"], 1)
        self.assertEqual(vacio["items"], [])
        self.assertEqual(vacio["total"], 0)


if __name__ == "__main__":
    unittest.main()
