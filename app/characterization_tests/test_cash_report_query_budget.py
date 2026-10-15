"""Comportamiento NUEVO (spec 119 US6): presupuesto de consultas de los reportes
de turno/período (FR-018/FR-019/FR-020/FR-023, SC-009, research.md D15).

Compuerta automática: el número de sentencias SQL de un reporte NO puede
crecer con el número de turnos que contiene -- ni con cuántos estén congelados
(snapshot), abiertos o cerrados sin snapshot (D8) -- ni con cuántos productos
se vendieron. Cuenta sentencias, no tiempo ni planes: los umbrales de SC-010 y el
uso de índices se verifican a mano (`quickstart.md` §10).

Cada turno va en su PROPIA caja: el índice parcial `idx_open_shift_per_register`
(`postgresql_where=...`) se vuelve UNIQUE total bajo SQLite y no se remueve del
metadata para no alterar otros tests (memoria `project_spec_119_implementado`).

Conteos medidos (sentencias por llamada; "antes" = agregador turno por turno de
US1-US5 con el puente T046, contra esta misma compuerta en rojo):

    período, 186 turnos congelados ........ antes 559  -> ahora 4  (1 turno: 4)
    período, 186 turnos sin cabecera (D8) .. antes 745  -> ahora 4  (1 turno: 5 antes)
    período, mezcla 62 + 62 + 62 ........... antes 621  -> ahora 4  (3 turnos: 11 antes)
    turno actual, 186 turnos abiertos ...... antes 559  -> ahora 4
    reporte de un turno (80 productos) ..... ahora 5 congelado / 7 abierto / 6 D8,
                                             los mismos con 1 producto
    cierre de turno, INSERT ................ 3 con 1 y con 80 productos
    período sin turnos ..................... 1

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_cash_report_query_budget -v
"""
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from sqlalchemy import event

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.cash import active_orders, reports, router
from app.api.v1.cash.schemas import ShiftClose
from app.models.cash_register import CashRegister
from app.models.cash_shift import CashShift
from app.models.sale import Sale, SaleItem

TENANT = fx.make_tenant_double()
BASE = datetime(2026, 10, 2, 12, 0)  # dentro de octubre de 2026, zona America/Bogota
GRANDE = 186  # 3 cajas x 2 turnos/día x 31 días (peor caso de un mes)

# Techos FIJOS de sentencias por llamada (research.md D10: cuatro consultas del
# agregador + las propias del endpoint). Se anotan con el valor medido.
TOPE_PERIODO = 4   # compute_period_sales_report: turnos, totales, productos, categorías
TOPE_ACTUAL = 4    # router.current_shift_report sin filtro de caja (mismas cuatro)
TOPE_TURNO = 7     # router.shift_sales_report abierto: turno + 4 + caja + pedidos activos


def _seed_variants(db, n):
    category = fx.make_category(db, name="Helados")
    variants = []
    for i in range(n):
        product = fx.make_product(db, category=category, name=f"Producto {i:03d}")
        variants.append(fx.make_variant(db, product=product, price=Decimal("1000")))
    db.commit()
    return variants


def _sale_pair(shift, variant, *, total=Decimal("1000"), quantity=1):
    sale = Sale(
        id=uuid4(), cash_shift_id=shift.id, user_id=uuid4(), user_name="Cajero de prueba",
        customer_name="Mostrador", subtotal=total, total=total, status="paid",
        sold_at=shift.opened_at,
    )
    item = SaleItem(
        id=uuid4(), sale_id=sale.id, product_variant_id=variant.id,
        description=f"producto-{variant.id}", options=[], quantity=quantity,
        unit_price=total / quantity, line_total=total,
    )
    return sale, item


def _seed_shifts(db, n, kind, variants, *, offset=0, sales_per_shift=2):
    """`n` turnos, cada uno en su propia caja, con `sales_per_shift` ventas
    pagadas. `kind`: "snapshot" (cerrado con `close_shift`, tiene cabecera),
    "open" (sin cerrar) o "d8" (cerrado SIN cabecera: anterior a esta spec)."""
    shifts, rows = [], []
    for i in range(n):
        register = CashRegister(id=uuid4(), name=f"Caja {uuid4()}", active=True)
        opened_at = BASE + timedelta(minutes=offset + i)
        shift = CashShift(
            id=uuid4(), cash_register_id=register.id, user_id=uuid4(), user_name="Cajero",
            opening_amount=Decimal("1000"), opened_at=opened_at,
            status="closed" if kind == "d8" else "open",
            closed_at=opened_at + timedelta(hours=1) if kind == "d8" else None,
        )
        rows += [register, shift]
        for j in range(sales_per_shift):
            sale, item = _sale_pair(shift, variants[(offset + i + j) % len(variants)])
            rows += [sale, item]
        shifts.append(shift)
    db.add_all(rows)
    db.commit()
    if kind == "snapshot":
        for shift in shifts:
            router.close_shift(shift.id, ShiftClose(), db)
    return shifts


def _capturar(db, fn):
    """Ejecuta `fn()` y devuelve `(resultado, [sentencias SQL])`."""
    sentencias: list[str] = []
    bind = db.get_bind()

    def _rec(conn, cursor, statement, parameters, context, executemany):
        sentencias.append(statement)

    event.listen(bind, "before_cursor_execute", _rec)
    try:
        resultado = fn()
    finally:
        event.remove(bind, "before_cursor_execute", _rec)
    return resultado, sentencias


def _periodo(db):
    return reports.compute_period_sales_report(db, TENANT, "month", ref_date=date(2026, 10, 15))


def _actual(db):
    return router.current_shift_report(cash_register_id=None, db=db, tenant=TENANT)


class TestPresupuestoDeConsultasDelPeriodo(unittest.TestCase):
    """SC-009/FR-018: mismas sentencias con un período pequeño y con 186 turnos,
    en las tres composiciones del dato."""

    def _comparar(self, fn, base: dict, grande: dict, tope: int):
        db_chico = fx.new_session()
        variants = _seed_variants(db_chico, 4)
        for kind, n in base.items():
            _seed_shifts(db_chico, n, kind, variants)
        _, chico = _capturar(db_chico, lambda: fn(db_chico))

        db_grande = fx.new_session()
        variants = _seed_variants(db_grande, 4)
        for kind, n in grande.items():
            _seed_shifts(db_grande, n, kind, variants)
        _, grande_sql = _capturar(db_grande, lambda: fn(db_grande))

        self.assertEqual(
            len(chico), len(grande_sql),
            f"CONTEOS: {base} -> {len(chico)} sentencias; {grande} -> {len(grande_sql)}",
        )
        self.assertLessEqual(len(grande_sql), tope, f"CONTEOS: {len(grande_sql)} > tope {tope}")

    def test_periodo_con_todos_los_turnos_congelados(self):
        self._comparar(_periodo, {"snapshot": 1}, {"snapshot": GRANDE}, TOPE_PERIODO)

    def test_periodo_con_mezcla_de_congelados_abiertos_y_cerrados_sin_cabecera(self):
        mezcla = GRANDE // 3
        self._comparar(
            _periodo,
            {"snapshot": 1, "open": 1, "d8": 1},
            {"snapshot": mezcla, "open": mezcla, "d8": mezcla},
            TOPE_PERIODO,
        )

    def test_periodo_con_todos_los_turnos_sin_cabecera(self):
        self._comparar(_periodo, {"d8": 1}, {"d8": GRANDE}, TOPE_PERIODO)

    def test_turno_actual_con_un_turno_abierto_y_con_186_abiertos(self):
        self._comparar(_actual, {"open": 1}, {"open": GRANDE}, TOPE_ACTUAL)

    def test_periodo_sin_turnos_no_ejecuta_mas_sentencias_que_uno_con_turnos(self):
        db_vacio = fx.new_session()
        _, vacio = _capturar(db_vacio, lambda: _periodo(db_vacio))
        resultado = _periodo(db_vacio)
        self.assertEqual(resultado["shift_ids"], [])
        self.assertEqual(resultado["top_products"], [])

        db = fx.new_session()
        _seed_shifts(db, 3, "d8", _seed_variants(db, 2))
        _, con_turnos = _capturar(db, lambda: _periodo(db))
        self.assertLessEqual(len(vacio), len(con_turnos), f"CONTEOS: {len(vacio)} vs {len(con_turnos)}")


class TestPresupuestoDeUnTurno(unittest.TestCase):
    """`router.shift_sales_report` (siempre un turno): las sentencias no crecen con
    los productos vendidos, en los tres estados del turno."""

    def _sentencias_de_un_turno(self, kind: str, n_productos: int) -> int:
        db = fx.new_session()
        variants = _seed_variants(db, n_productos)
        register = CashRegister(id=uuid4(), name="Caja 1", active=True)
        opened_at = BASE
        shift = CashShift(
            id=uuid4(), cash_register_id=register.id, user_id=uuid4(), user_name="Cajero",
            opening_amount=Decimal("1000"), opened_at=opened_at,
            status="closed" if kind == "d8" else "open",
            closed_at=opened_at + timedelta(hours=1) if kind == "d8" else None,
        )
        rows = [register, shift]
        for variant in variants:
            rows += list(_sale_pair(shift, variant))
        db.add_all(rows)
        db.commit()
        if kind == "snapshot":
            router.close_shift(shift.id, ShiftClose(), db)
        _, sql = _capturar(db, lambda: router.shift_sales_report(shift.id, db))
        return len(sql)

    def test_las_sentencias_no_crecen_con_los_productos_en_ningun_estado_del_turno(self):
        for kind in ("snapshot", "open", "d8"):
            with self.subTest(estado=kind):
                con_1 = self._sentencias_de_un_turno(kind, 1)
                con_80 = self._sentencias_de_un_turno(kind, 80)
                self.assertEqual(con_1, con_80, f"CONTEOS {kind}: 1 producto -> {con_1}; 80 -> {con_80}")
                self.assertLessEqual(con_80, TOPE_TURNO, f"CONTEOS {kind}: {con_80} > tope {TOPE_TURNO}")


class TestEscrituraDelCierre(unittest.TestCase):
    """data-model.md §2.4/D15: `close_shift` escribe el desglose con UNA inserción
    masiva por tabla hija, no una sentencia por producto."""

    def _inserts_del_cierre(self, n_productos: int) -> int:
        db = fx.new_session()
        variants = _seed_variants(db, n_productos)
        shift = fx.make_cash_shift(db)
        db.commit()
        db.add_all([row for v in variants for row in _sale_pair(shift, v)])
        db.commit()
        _, sql = _capturar(db, lambda: router.close_shift(shift.id, ShiftClose(), db))
        return len([s for s in sql if s.lstrip().upper().startswith("INSERT")])

    def test_el_cierre_ejecuta_el_mismo_numero_de_insert_con_1_y_con_80_productos(self):
        con_1 = self._inserts_del_cierre(1)
        con_80 = self._inserts_del_cierre(80)
        self.assertEqual(con_1, con_80, f"CONTEOS INSERT: 1 producto -> {con_1}; 80 -> {con_80}")


class TestAgregacionEnLaBaseDeDatos(unittest.TestCase):
    """FR-019: contar sentencias no basta (cuatro consultas que agreguen en Python
    también las cumplirían): el SQL de productos debe agrupar y recortar al top-N
    EN la base de datos, y el de categorías agrupar."""

    def test_productos_agrupa_y_limita_y_categorias_agrupa(self):
        db = fx.new_session()
        variants = _seed_variants(db, 12)  # > TOP_PRODUCTS_LIMIT
        shift = fx.make_cash_shift(db)
        db.commit()
        db.add_all([row for v in variants for row in _sale_pair(shift, v)])
        db.commit()
        router.close_shift(shift.id, ShiftClose(), db)

        resultado, sql = _capturar(db, lambda: _periodo(db))

        con_limit = [s for s in sql if "LIMIT" in s.upper()]
        self.assertEqual(len(con_limit), 1, f"se esperaba una sola sentencia con LIMIT, hubo {len(con_limit)}")
        self.assertIn("GROUP BY", con_limit[0].upper())
        categorias = [s for s in sql if "shift_sales_report_categories" in s]
        self.assertTrue(categorias, "ninguna sentencia lee las filas de categorías del snapshot")
        self.assertTrue(all("GROUP BY" in s.upper() for s in categorias))
        # El recorte lo hace la base de datos: llegan exactamente 10 filas.
        self.assertEqual(len(resultado["top_products"]), reports.TOP_PRODUCTS_LIMIT)


class TestContadoresDePedidosActivos(unittest.TestCase):
    """FR-023, research.md D12: el contador de activos es un `COUNT` en la base de
    datos y la lista solo se trae cuando hay que mostrarla."""

    def _seed_pedidos(self, db, n_por_canal, *, canales=("DINE_IN", "TAKEAWAY", "DELIVERY")):
        for canal in canales:
            for _ in range(n_por_canal):
                fx.make_customer_order(
                    db, fx.make_table_session(db), order_type=canal, status="abierta",
                )
        for estado in ("pagada", "cancelada"):
            fx.make_customer_order(db, fx.make_table_session(db), order_type="DINE_IN", status=estado)
        db.commit()

    def test_el_conteo_coincide_con_la_longitud_de_la_lista_de_activos(self):
        db = fx.new_session()
        self._seed_pedidos(db, 2)
        self.assertEqual(active_orders.count_active_orders(db), 6)
        self.assertEqual(active_orders.count_active_orders(db), len(active_orders.list_active_orders(db)))

    def test_sin_pedidos_activos_close_shift_no_trae_la_lista(self):
        db = fx.new_session()
        shift = fx.make_cash_shift(db)
        db.commit()
        with mock.patch.object(
            active_orders, "list_active_orders", wraps=active_orders.list_active_orders
        ) as espia:
            router.close_shift(shift.id, ShiftClose(), db)
        espia.assert_not_called()

    def test_con_pedidos_activos_close_shift_trae_la_lista_una_sola_vez(self):
        db = fx.new_session()
        shift = fx.make_cash_shift(db)
        self._seed_pedidos(db, 1)
        with mock.patch.object(
            active_orders, "list_active_orders", wraps=active_orders.list_active_orders
        ) as espia:
            respuesta = router.close_shift(shift.id, ShiftClose(), db)
        self.assertEqual(respuesta.status_code, 409)
        self.assertEqual(espia.call_count, 1)

    def test_reporte_de_turno_abierto_no_gana_sentencias_con_50_pedidos_activos(self):
        def sentencias(n_pedidos: int):
            db = fx.new_session()
            shift = fx.make_cash_shift(db)
            db.commit()
            for _ in range(n_pedidos):
                fx.make_customer_order(db, fx.make_table_session(db), order_type="TAKEAWAY", status="abierta")
            db.commit()
            resultado, sql = _capturar(db, lambda: router.shift_sales_report(shift.id, db))
            return resultado["active_orders_count"], len(sql)

        contador_1, sql_1 = sentencias(1)
        contador_50, sql_50 = sentencias(50)
        self.assertEqual((contador_1, contador_50), (1, 50))
        self.assertEqual(sql_1, sql_50, f"CONTEOS: 1 pedido -> {sql_1}; 50 -> {sql_50}")


if __name__ == "__main__":
    unittest.main()
