"""Comportamiento NUEVO (spec 119): reporte operativo de ventas por turno de
caja — US1 (reporte exacto de un turno que cruza la medianoche), US2
(reporte "del día"/semana/mes/rango libre de contaminación nocturna, cita
A-128 de `registro-de-anomalias.md`), US4 (reporte en tiempo real de un
turno en curso) y US5 (turno sin ventas).

Ejercita `app/api/v1/cash/reports.py` directamente y, para los endpoints,
las funciones de `app/api/v1/cash/router.py` invocadas como funciones
Python normales (mismo patrón que `test_cart_router.py`/
`test_table_sessions_router.py`, research.md §1 de esas specs): `Depends`
solo se resuelve en una request ASGI real, así que `db`/`tenant` se pasan a
mano y los parámetros de autenticación (`_`) se omiten cuando la función no
los usa.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_cash_shift_sales_report -v
"""
import unittest
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.cash import reports, router
from app.api.v1.cash.schemas import ShiftClose
from app.models.sale import Sale, SaleItem


def _seed_sale(db, shift, variant, *, total: Decimal, quantity: int = 1,
               description: str | None = None, sold_at: datetime | None = None) -> Sale:
    """Venta pagada del turno `shift` -- sin `Payment` (el arqueo de
    efectivo, `cash/service.py::reconcile`, no es el objeto de esta spec;
    los tests de cierre usan `ShiftClose()` sin `counted_amount` para
    saltárselo por completo)."""
    sale = Sale(
        id=uuid4(), cash_shift_id=shift.id, user_id=uuid4(), user_name="Cajero de prueba",
        customer_name="Mostrador", subtotal=total, total=total, status="paid",
        sold_at=sold_at or datetime.now(),
    )
    db.add(sale)
    db.flush()
    db.add(SaleItem(
        id=uuid4(), sale_id=sale.id, product_variant_id=variant.id,
        description=description or f"producto-{variant.id}",
        options=[], quantity=quantity, unit_price=total / quantity, line_total=total,
    ))
    db.flush()
    return sale


class TestComputeShiftSalesReport(unittest.TestCase):
    """US1 (Acceptance Scenario 1/2) + RN1/RN2/RN3."""

    def test_reporte_de_un_turno_que_cruza_medianoche_solo_cuenta_sus_propias_ventas(self):
        db = fx.new_session()
        register = fx.make_cash_register(db, name="Caja 1")
        shift = fx.make_cash_shift(db, register=register, opened_at=datetime(2026, 10, 12, 17, 0))
        category = fx.make_category(db, name="Helados")
        product = fx.make_product(db, category=category, name="Cono")
        variant = fx.make_variant(db, product=product, name="Grande", price=Decimal("30000"))
        db.commit()

        _seed_sale(db, shift, variant, total=Decimal("300000"), quantity=10,
                   description="Cono - Grande", sold_at=datetime(2026, 10, 12, 23, 30))
        _seed_sale(db, shift, variant, total=Decimal("150000"), quantity=5,
                   description="Cono - Grande", sold_at=datetime(2026, 10, 13, 1, 50))
        db.commit()

        data = reports.compute_shift_sales_report(db, shift)

        self.assertEqual(data["total_sales"], Decimal("450000"))
        self.assertEqual(data["order_count"], 2)
        self.assertEqual(data["avg_ticket"], Decimal("225000.00"))
        self.assertEqual(len(data["products_breakdown"]), 1)
        self.assertEqual(data["products_breakdown"][0]["units"], 15)
        self.assertEqual(Decimal(data["products_breakdown"][0]["revenue"]), Decimal("450000"))
        self.assertEqual(len(data["categories_breakdown"]), 1)
        self.assertEqual(data["categories_breakdown"][0]["category_name"], "Helados")
        self.assertEqual(Decimal(data["categories_breakdown"][0]["revenue"]), Decimal("450000"))

    def test_mismo_resultado_filtrando_el_periodo_por_la_caja_del_turno(self):
        """Acceptance Scenario 2 de US1: filtrar además por `cash_register_id`
        de esa caja no cambia el resultado (nada más suma a ese período)."""
        db = fx.new_session()
        register = fx.make_cash_register(db, name="Caja 1")
        shift = fx.make_cash_shift(db, register=register, opened_at=datetime(2026, 10, 12, 17, 0))
        variant = fx.make_variant(db, price=Decimal("10000"))
        db.commit()
        _seed_sale(db, shift, variant, total=Decimal("100000"), quantity=10)
        db.commit()
        router.close_shift(shift.id, ShiftClose(), db)

        tenant = fx.make_tenant_double()
        result = reports.compute_period_sales_report(
            db, tenant, "day", ref_date=date(2026, 10, 12), cash_register_id=register.id,
        )
        self.assertEqual(result["total_sales"], Decimal("100000"))
        self.assertEqual(result["shift_ids"], [shift.id])


class TestSnapshotInmutable(unittest.TestCase):
    """FR-013/SC-003 (research.md D2): el snapshot se escribe una sola vez,
    al cerrar, dentro de la transacción de `close_shift`; anular una venta
    después no lo cambia."""

    def test_snapshot_no_cambia_tras_anular_una_venta_del_turno_ya_cerrado(self):
        db = fx.new_session()
        register = fx.make_cash_register(db)
        shift = fx.make_cash_shift(db, register=register)
        variant = fx.make_variant(db, price=Decimal("20000"))
        db.commit()
        sale = _seed_sale(db, shift, variant, total=Decimal("200000"), quantity=10)
        db.commit()

        closed = router.close_shift(shift.id, ShiftClose(), db)
        self.assertEqual(closed.status, "closed")

        antes = router.shift_sales_report(shift.id, db)
        self.assertEqual(antes["total_sales"], Decimal("200000"))
        self.assertEqual(antes["is_live"], False)

        sale.status = "void"
        db.commit()

        despues = router.shift_sales_report(shift.id, db)
        self.assertEqual(despues["total_sales"], antes["total_sales"])
        self.assertEqual(despues["order_count"], antes["order_count"])
        self.assertEqual(despues["top_products"], antes["top_products"])


class TestAtribucionPorTurnoDeCobro(unittest.TestCase):
    """RN1/RN2 (research.md D3): `Sale.cash_shift_id` ya es el turno que
    estaba abierto en el instante del cobro, nunca el turno vigente cuando
    se creó el pedido -- este test documenta esa atribución tal como la lee
    el reporte, sin tocar `sales/builder.py`."""

    def test_venta_se_atribuye_al_turno_que_cobra_no_al_que_crea_el_pedido(self):
        db = fx.new_session()
        register = fx.make_cash_register(db)
        shift_a = fx.make_cash_shift(db, register=register, opened_at=datetime(2026, 10, 12, 17, 0))
        db.commit()
        router.close_shift(shift_a.id, ShiftClose(), db)

        # Otra caja -- el punto de este test es la atribución por
        # `cash_shift_id`, no el filtro por caja (ver research.md D1).
        shift_b = fx.make_cash_shift(db, opened_at=datetime(2026, 10, 13, 8, 0))
        variant = fx.make_variant(db, price=Decimal("15000"))
        db.commit()
        # El pedido "nació" bajo shift_a (ya cerrado), pero el cobro ocurre
        # bajo shift_b -- la venta se liga a shift_b.
        _seed_sale(db, shift_b, variant, total=Decimal("15000"), quantity=1)
        db.commit()

        report_a = reports.compute_shift_sales_report(db, shift_a)
        report_b = reports.compute_shift_sales_report(db, shift_b)
        self.assertEqual(report_a["total_sales"], Decimal("0"))
        self.assertEqual(report_b["total_sales"], Decimal("15000"))


class TestReportePorPeriodo(unittest.TestCase):
    """US2: el reporte "del día"/semana/mes/rango suma por fecha operativa
    de APERTURA, nunca por la hora de cada venta individual (RN4,
    research.md D4) -- reemplaza la tarjeta "Ingresos hoy" de
    `admin-dashboard.component.ts` (A-128, `registro-de-anomalias.md`)."""

    def test_reporte_del_dia_excluye_el_turno_abierto_el_dia_anterior(self):
        """Acceptance Scenario 1 de US2: un turno abierto el 12 a las 17:00
        y cerrado el 13 a las 02:00 (con ventas de madrugada del 13) NO
        cuenta para "el día 13" -- solo para "el día 12" (A-128: la vieja
        tarjeta, que cortaba por `sold_at`, sí lo habría contaminado)."""
        db = fx.new_session()
        register = fx.make_cash_register(db)
        shift_12 = fx.make_cash_shift(db, register=register, opened_at=datetime(2026, 10, 12, 17, 0))
        variant = fx.make_variant(db, price=Decimal("10000"))
        db.commit()
        _seed_sale(db, shift_12, variant, total=Decimal("150000"), quantity=15,
                   sold_at=datetime(2026, 10, 13, 1, 50))
        db.commit()
        router.close_shift(shift_12.id, ShiftClose(), db)

        # Otra caja -- el índice único parcial que permite reabrir una caja ya
        # usada (`idx_open_shift_per_register`, `postgresql_where=...`) no
        # aplica bajo SQLite (el fixture de este repo no lo remueve, y
        # `postgresql_where` es específico de ese dialecto); el punto de este
        # test es la fecha de apertura, no la identidad de la caja.
        shift_13 = fx.make_cash_shift(db, opened_at=datetime(2026, 10, 13, 17, 0))
        db.commit()
        _seed_sale(db, shift_13, variant, total=Decimal("20000"), quantity=2)
        db.commit()

        tenant = fx.make_tenant_double()
        dia_13 = reports.compute_period_sales_report(db, tenant, "day", ref_date=date(2026, 10, 13))
        self.assertEqual(dia_13["shift_ids"], [shift_13.id])
        self.assertEqual(dia_13["total_sales"], Decimal("20000"))

        dia_12 = reports.compute_period_sales_report(db, tenant, "day", ref_date=date(2026, 10, 12))
        self.assertEqual(dia_12["shift_ids"], [shift_12.id])
        self.assertEqual(dia_12["total_sales"], Decimal("150000"))

    def test_reporte_del_dia_sin_filtro_de_caja_suma_varias_cajas(self):
        """Acceptance Scenario 2 de US2 / FR-012: agregar sin filtro de caja
        suma los turnos de todas las cajas del tenant abiertos ese día."""
        db = fx.new_session()
        caja_a = fx.make_cash_register(db, name="Caja A")
        caja_b = fx.make_cash_register(db, name="Caja B")
        shift_a = fx.make_cash_shift(db, register=caja_a, opened_at=datetime(2026, 10, 13, 9, 0))
        shift_b = fx.make_cash_shift(db, register=caja_b, opened_at=datetime(2026, 10, 13, 10, 0))
        variant = fx.make_variant(db, price=Decimal("5000"))
        db.commit()
        _seed_sale(db, shift_a, variant, total=Decimal("50000"), quantity=10)
        _seed_sale(db, shift_b, variant, total=Decimal("30000"), quantity=6)
        db.commit()

        tenant = fx.make_tenant_double()
        result = reports.compute_period_sales_report(db, tenant, "day", ref_date=date(2026, 10, 13))
        self.assertEqual(result["total_sales"], Decimal("80000"))
        self.assertEqual(set(result["shift_ids"]), {shift_a.id, shift_b.id})

    def test_semana_mes_filtran_por_fecha_operativa_de_apertura_calendario(self):
        """Acceptance Scenario 3 de US2 (research.md D4): semana = lunes a
        domingo calendario; mes = día 1 al último día del mes calendario --
        NO la ventana móvil de 7/30 días de `/dashboard/reports`."""
        db = fx.new_session()
        register = fx.make_cash_register(db)
        # Martes 2026-10-13 (misma semana ISO que el jueves 2026-10-15).
        shift_misma_semana = fx.make_cash_shift(
            db, register=register, opened_at=datetime(2026, 10, 13, 9, 0)
        )
        variant = fx.make_variant(db, price=Decimal("1000"))
        db.commit()
        _seed_sale(db, shift_misma_semana, variant, total=Decimal("10000"), quantity=10)
        db.commit()

        # Otra caja, lunes de la semana SIGUIENTE (2026-10-19) -- fuera de
        # esa semana. El índice único parcial que permitiría reabrir
        # `register` tras cerrarla (`idx_open_shift_per_register`,
        # `postgresql_where=...`) no aplica bajo SQLite; el punto de este
        # test es la fecha de apertura, no la identidad de la caja.
        shift_otra_semana = fx.make_cash_shift(
            db, opened_at=datetime(2026, 10, 19, 9, 0)
        )
        db.commit()
        _seed_sale(db, shift_otra_semana, variant, total=Decimal("40000"), quantity=40)
        db.commit()

        tenant = fx.make_tenant_double()
        semana = reports.compute_period_sales_report(db, tenant, "week", ref_date=date(2026, 10, 15))
        self.assertEqual(semana["date_from"], date(2026, 10, 12))
        self.assertEqual(semana["date_to"], date(2026, 10, 18))
        self.assertEqual(semana["shift_ids"], [shift_misma_semana.id])

        mes = reports.compute_period_sales_report(db, tenant, "month", ref_date=date(2026, 10, 15))
        self.assertEqual(mes["date_from"], date(2026, 10, 1))
        self.assertEqual(mes["date_to"], date(2026, 10, 31))
        self.assertEqual(set(mes["shift_ids"]), {shift_misma_semana.id, shift_otra_semana.id})

    def test_rango_personalizado_usa_date_from_date_to_explicitos(self):
        db = fx.new_session()
        register = fx.make_cash_register(db)
        shift = fx.make_cash_shift(db, register=register, opened_at=datetime(2026, 10, 14, 9, 0))
        variant = fx.make_variant(db, price=Decimal("1000"))
        db.commit()
        _seed_sale(db, shift, variant, total=Decimal("5000"), quantity=5)
        db.commit()

        tenant = fx.make_tenant_double()
        result = reports.compute_period_sales_report(
            db, tenant, "range", date_from=date(2026, 10, 14), date_to=date(2026, 10, 14),
        )
        self.assertEqual(result["shift_ids"], [shift.id])

    def test_agregacion_exacta_por_periodo_recombina_desglose_completo_sin_perder_precision(self):
        """FR-017/SC-007: la suma del período se calcula recombinando el
        desglose COMPLETO de cada turno -- nunca sumando el top-N ya
        recortado de cada uno."""
        db = fx.new_session()
        register = fx.make_cash_register(db)
        category = fx.make_category(db, name="Helados")
        product = fx.make_product(db, category=category)
        compartido = fx.make_variant(db, product=product, name="Cono Chocolate", price=Decimal("8000"))
        exclusivo_a = fx.make_variant(db, product=product, name="Malteada", price=Decimal("12000"))
        exclusivo_b = fx.make_variant(db, product=product, name="Paleta", price=Decimal("3000"))

        shift_a = fx.make_cash_shift(db, register=register, opened_at=datetime(2026, 10, 13, 9, 0))
        db.commit()
        _seed_sale(db, shift_a, compartido, total=Decimal("40000"), quantity=5, description="Cono Chocolate")
        _seed_sale(db, shift_a, exclusivo_a, total=Decimal("12000"), quantity=1, description="Malteada")
        db.commit()
        router.close_shift(shift_a.id, ShiftClose(), db)

        # Otra caja -- el índice único parcial que permitiría reabrir
        # `register` tras cerrarla no aplica bajo SQLite (ver notas arriba);
        # el punto de este test es la recombinación de desgloses, no la
        # identidad de la caja.
        shift_b = fx.make_cash_shift(db, opened_at=datetime(2026, 10, 14, 9, 0))
        db.commit()
        _seed_sale(db, shift_b, compartido, total=Decimal("24000"), quantity=3, description="Cono Chocolate")
        _seed_sale(db, shift_b, exclusivo_b, total=Decimal("9000"), quantity=3, description="Paleta")
        db.commit()
        router.close_shift(shift_b.id, ShiftClose(), db)

        tenant = fx.make_tenant_double()
        result = reports.compute_period_sales_report(db, tenant, "week", ref_date=date(2026, 10, 13))

        by_name = {row["name"]: row for row in result["top_products"]}
        self.assertEqual(by_name["Cono Chocolate"]["units"], 8)
        self.assertEqual(Decimal(by_name["Cono Chocolate"]["revenue"]), Decimal("64000"))
        self.assertIn("Malteada", by_name)
        self.assertIn("Paleta", by_name)
        self.assertEqual(result["total_sales"], Decimal("85000"))

    def test_aislamiento_por_caja_un_filtro_no_mezcla_otra_caja(self):
        """SC-004 (aislamiento por caja; el aislamiento multi-tenant es
        estructural vía `schema_translate_map`, `app/core/db.py`, fuera del
        alcance de este test en SQLite de un solo esquema)."""
        db = fx.new_session()
        caja_a = fx.make_cash_register(db, name="Caja A")
        caja_b = fx.make_cash_register(db, name="Caja B")
        shift_a = fx.make_cash_shift(db, register=caja_a, opened_at=datetime(2026, 10, 13, 9, 0))
        shift_b = fx.make_cash_shift(db, register=caja_b, opened_at=datetime(2026, 10, 13, 9, 0))
        variant = fx.make_variant(db, price=Decimal("1000"))
        db.commit()
        _seed_sale(db, shift_a, variant, total=Decimal("111000"), quantity=1)
        _seed_sale(db, shift_b, variant, total=Decimal("222000"), quantity=1)
        db.commit()

        tenant = fx.make_tenant_double()
        result = reports.compute_period_sales_report(
            db, tenant, "day", ref_date=date(2026, 10, 13), cash_register_id=caja_a.id,
        )
        self.assertEqual(result["total_sales"], Decimal("111000"))
        self.assertEqual(result["shift_ids"], [shift_a.id])


class TestReporteEnVivoDeTurnoEnCurso(unittest.TestCase):
    """US4: el reporte de un turno todavía abierto se calcula en vivo."""

    def test_turno_abierto_es_en_vivo_y_refleja_una_venta_registrada_despues(self):
        db = fx.new_session()
        register = fx.make_cash_register(db)
        shift = fx.make_cash_shift(db, register=register)
        variant = fx.make_variant(db, price=Decimal("10000"))
        db.commit()
        _seed_sale(db, shift, variant, total=Decimal("10000"), quantity=1)
        db.commit()

        primero = router.shift_sales_report(shift.id, db)
        self.assertEqual(primero["status"], "open")
        self.assertEqual(primero["is_live"], True)
        self.assertEqual(primero["total_sales"], Decimal("10000"))
        self.assertEqual(primero["active_orders_count"], 0)

        _seed_sale(db, shift, variant, total=Decimal("5000"), quantity=1)
        db.commit()

        segundo = router.shift_sales_report(shift.id, db)
        self.assertEqual(segundo["total_sales"], Decimal("15000"))

    def test_al_cerrar_deja_de_mostrar_en_curso_y_congela_los_mismos_totales(self):
        db = fx.new_session()
        register = fx.make_cash_register(db)
        shift = fx.make_cash_shift(db, register=register)
        variant = fx.make_variant(db, price=Decimal("7000"))
        db.commit()
        _seed_sale(db, shift, variant, total=Decimal("7000"), quantity=1)
        db.commit()

        antes_de_cerrar = router.shift_sales_report(shift.id, db)
        router.close_shift(shift.id, ShiftClose(), db)
        despues_de_cerrar = router.shift_sales_report(shift.id, db)

        self.assertEqual(despues_de_cerrar["status"], "closed")
        self.assertEqual(despues_de_cerrar["is_live"], False)
        self.assertIsNone(despues_de_cerrar["active_orders_count"])
        self.assertEqual(despues_de_cerrar["total_sales"], antes_de_cerrar["total_sales"])

    def test_reportes_current_sin_filtro_agrega_todos_los_turnos_abiertos_del_tenant(self):
        """FR-012: `GET /cash/reports/current` sin `cash_register_id` agrega
        TODOS los turnos `open`, sean de la caja que sean."""
        db = fx.new_session()
        caja_a = fx.make_cash_register(db, name="Caja A")
        caja_b = fx.make_cash_register(db, name="Caja B")
        shift_a = fx.make_cash_shift(db, register=caja_a)
        shift_b = fx.make_cash_shift(db, register=caja_b)
        variant = fx.make_variant(db, price=Decimal("1000"))
        db.commit()
        _seed_sale(db, shift_a, variant, total=Decimal("10000"), quantity=10)
        _seed_sale(db, shift_b, variant, total=Decimal("20000"), quantity=20)
        db.commit()

        tenant = fx.make_tenant_double()
        result = router.current_shift_report(cash_register_id=None, db=db, tenant=tenant)
        self.assertEqual(result["period"], "current_shift")
        self.assertEqual(result["total_sales"], Decimal("30000"))
        self.assertEqual(set(result["shift_ids"]), {shift_a.id, shift_b.id})

    def test_reportes_current_sin_ningun_turno_abierto_devuelve_ceros_no_404(self):
        db = fx.new_session()
        tenant = fx.make_tenant_double()
        result = router.current_shift_report(cash_register_id=None, db=db, tenant=tenant)
        self.assertEqual(result["total_sales"], Decimal("0"))
        self.assertEqual(result["order_count"], 0)
        self.assertEqual(result["shift_ids"], [])


class TestTurnoSinVentas(unittest.TestCase):
    """US5 (Acceptance Scenario 1): turno abierto y cerrado sin ninguna
    orden pagada -- ceros y listas vacías, nunca un error."""

    def test_turno_sin_ventas_reporte_limpio_sin_excepcion(self):
        db = fx.new_session()
        register = fx.make_cash_register(db)
        shift = fx.make_cash_shift(db, register=register)
        db.commit()
        router.close_shift(shift.id, ShiftClose(), db)

        result = router.shift_sales_report(shift.id, db)
        self.assertEqual(result["total_sales"], Decimal("0"))
        self.assertEqual(result["order_count"], 0)
        self.assertEqual(result["avg_ticket"], Decimal("0.00"))
        self.assertEqual(result["top_products"], [])
        self.assertEqual(result["categories"], [])


if __name__ == "__main__":
    unittest.main()
