"""Spec 138, Historia 3 (FR-007, FR-009) — el día de negocio es el día LOCAL del tenant.

«Hoy», «el segundo rechazo del día» y el día del nombre del archivo de inventario usan la fecha local
del tenant, nunca la fecha UTC ni la del servidor. Esta prueba fija:

  * `local_day_bounds_utc` con los valores **medidos** de `data-model.md «Día de negocio»`: un día de
    25 h, uno de 23 h y la medianoche que no existe (Santiago).
  * `business_today(tenant)` con el reloj congelado, en las tres zonas de prueba (Bogotá, Ciudad de
    México y Madrid), incluida la ventana 00:00–02:00 al este de UTC en la que el día UTC todavía es el anterior.
  * Un turno de caja que cruza la medianoche local sigue atribuido al día de APERTURA (US3-3, sin cambio): la
    atribución la define el vínculo con el turno (`Sale.cash_shift_id`), no la hora de cada venta.

    python -m unittest app.characterization_tests.test_business_day_boundaries -v
"""
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.api.v1.cash import reports
from app.characterization_tests import orders_fixtures as fx
from app.characterization_tests.cart_fixtures import frozen_now
from app.characterization_tests.fixtures import ZONAS_DE_PRUEBA, tenant_en
from app.core.timezone import business_today, local_day_bounds_utc, resolve_timezone
from app.models.sale import Sale, SaleItem


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _hoy(zona: str, instante: datetime) -> date:
    with frozen_now(instante, module="app.core.timezone"):
        return business_today(tenant_en(zona))


class TestLimitesDelDiaLocalMedidos(unittest.TestCase):
    """(zona, día local, inicio UTC, fin UTC) medidos el 2026-10-25 (data-model.md)."""

    CASOS = [
        ("Madrid, fin del horario de verano: 25 h", "Europe/Madrid", date(2026, 10, 25),
         datetime(2026, 10, 24, 22, 0), datetime(2026, 10, 25, 23, 0)),
        ("Madrid, día normal: 24 h", "Europe/Madrid", date(2026, 10, 24),
         datetime(2026, 10, 23, 22, 0), datetime(2026, 10, 24, 22, 0)),
        ("Madrid, inicio del horario de verano: 23 h", "Europe/Madrid", date(2026, 3, 29),
         datetime(2026, 3, 28, 23, 0), datetime(2026, 3, 29, 22, 0)),
        ("Santiago, la medianoche no existe: 23 h", "America/Santiago", date(2026, 9, 6),
         datetime(2026, 9, 6, 4, 0), datetime(2026, 9, 7, 3, 0)),
        ("Bogotá", "America/Bogota", date(2026, 10, 25),
         datetime(2026, 10, 25, 5, 0), datetime(2026, 10, 26, 5, 0)),
        ("Tokio", "Asia/Tokyo", date(2026, 10, 25),
         datetime(2026, 10, 24, 15, 0), datetime(2026, 10, 25, 15, 0)),
    ]

    def test_limites_medidos(self):
        for nombre, zona, dia, inicio, fin in self.CASOS:
            with self.subTest(caso=nombre):
                self.assertEqual(local_day_bounds_utc(dia, ZoneInfo(zona)), (inicio, fin))

    def test_duraciones_de_25_23_y_24_horas(self):
        horas = {
            nombre: (fin - inicio) / timedelta(hours=1) for nombre, _z, _d, inicio, fin in self.CASOS
        }
        self.assertEqual(horas["Madrid, fin del horario de verano: 25 h"], 25)
        self.assertEqual(horas["Madrid, inicio del horario de verano: 23 h"], 23)
        self.assertEqual(horas["Santiago, la medianoche no existe: 23 h"], 23)
        self.assertEqual(horas["Madrid, día normal: 24 h"], 24)

    def test_ciudad_de_mexico_dura_24_horas_todo_el_ano_y_va_seis_horas_detras(self):
        zona = ZoneInfo("America/Mexico_City")
        for dia in (date(2026, 1, 15), date(2026, 4, 5), date(2026, 7, 15), date(2026, 10, 25), date(2026, 11, 1)):
            with self.subTest(dia=dia):
                inicio, fin = local_day_bounds_utc(dia, zona)
                self.assertEqual(fin - inicio, timedelta(hours=24))
                self.assertEqual(inicio, datetime.combine(dia, datetime.min.time()) + timedelta(hours=6))

    def test_dias_consecutivos_no_dejan_hueco_ni_se_solapan(self):
        # Cruzando los dos cambios de hora de Madrid y el de Santiago.
        for zona, desde in (("Europe/Madrid", date(2026, 3, 27)), ("Europe/Madrid", date(2026, 10, 23)),
                            ("America/Santiago", date(2026, 9, 4))):
            tz = ZoneInfo(zona)
            for i in range(6):
                dia = desde + timedelta(days=i)
                with self.subTest(zona=zona, dia=dia):
                    _, fin = local_day_bounds_utc(dia, tz)
                    inicio_siguiente, _ = local_day_bounds_utc(dia + timedelta(days=1), tz)
                    self.assertEqual(fin, inicio_siguiente)


class TestBusinessToday(unittest.TestCase):
    def test_hoy_es_el_dia_local_no_el_dia_utc(self):
        # 2026-10-26 00:30Z: en Bogotá y en México todavía es el 25; en Madrid (UTC+1) ya es el 26.
        instante = _utc(2026, 10, 26, 0, 30)
        self.assertEqual(_hoy("America/Bogota", instante), date(2026, 10, 25))
        self.assertEqual(_hoy("America/Mexico_City", instante), date(2026, 10, 25))
        self.assertEqual(_hoy("Europe/Madrid", instante), date(2026, 10, 26))

    def test_madrid_entre_las_00_y_las_02_locales_hoy_es_el_dia_local(self):
        # 23:30Z del 24/11 = 00:30 del 25/11 en Madrid (UTC+1), aunque el día UTC sea el 24.
        instante = _utc(2026, 11, 24, 23, 30)
        self.assertEqual(instante.date(), date(2026, 11, 24))
        self.assertEqual(_hoy("Europe/Madrid", instante), date(2026, 11, 25))

    def test_tokio_a_la_1_de_la_madrugada_local_hoy_es_el_dia_local(self):
        # 16:00Z del 24/10 = 01:00 del 25/10 en Tokio (UTC+9).
        self.assertEqual(_hoy("Asia/Tokyo", _utc(2026, 10, 24, 16, 0)), date(2026, 10, 25))

    def test_bogota_entre_las_19_y_las_24_hoy_sigue_siendo_el_dia_local(self):
        # El defecto C de la spec: a las 20:00 de Bogotá el día UTC ya es el siguiente.
        instante = _utc(2026, 10, 26, 1, 0)  # 20:00 del 25/10 en Bogotá
        self.assertEqual(instante.date(), date(2026, 10, 26))
        self.assertEqual(_hoy("America/Bogota", instante), date(2026, 10, 25))

    def test_sin_tenant_usa_la_zona_de_respaldo_de_bogota(self):
        with frozen_now(_utc(2026, 10, 26, 1, 0), module="app.core.timezone"):
            self.assertEqual(business_today(None), date(2026, 10, 25))

    def test_propiedad_el_instante_siempre_cae_dentro_de_los_limites_de_su_dia_local(self):
        """Para cada zona de prueba y cada media hora de tres días: `business_today` es
        `instante.astimezone(zona).date()` y `[inicio, fin)` de ese día contiene el instante."""
        for zona in ZONAS_DE_PRUEBA:
            tz = resolve_timezone(tenant_en(zona))
            # Madrid cambia de hora el 25/10: la ventana cubre el día de 25 h.
            instante = _utc(2026, 10, 24, 0, 0)
            for _ in range(48 * 3):
                with self.subTest(zona=zona, instante=instante.isoformat()):
                    hoy = _hoy(zona, instante)
                    self.assertEqual(hoy, instante.astimezone(tz).date())
                    inicio, fin = local_day_bounds_utc(hoy, tz)
                    naive = instante.replace(tzinfo=None)
                    self.assertLessEqual(inicio, naive)
                    self.assertLess(naive, fin)
                instante += timedelta(minutes=30)


class TestTurnoQueCruzaLaMedianocheLocal(unittest.TestCase):
    """US3-3 (sin cambio): un turno abierto a las 23:30 locales y con una venta a las 00:30 locales del
    día siguiente se atribuye al día de APERTURA, en cualquier zona. Antes no había ninguna prueba así."""

    DIA = date(2026, 10, 20)  # sin cambios de hora en ninguna de las tres zonas

    def _seed(self, db, zona: str):
        tz = ZoneInfo(zona)
        a_utc = lambda local: local.astimezone(timezone.utc).replace(tzinfo=None)  # noqa: E731
        apertura = a_utc(datetime.combine(self.DIA, datetime.min.time(), tzinfo=tz) + timedelta(hours=23, minutes=30))
        venta = a_utc(datetime.combine(self.DIA + timedelta(days=1), datetime.min.time(), tzinfo=tz) + timedelta(minutes=30))
        register = fx.make_cash_register(db, name=f"Caja {zona}")
        shift = fx.make_cash_shift(db, register=register, opened_at=apertura)
        product = fx.make_product(db, category=fx.make_category(db), name="Cono")
        variant = fx.make_variant(db, product=product, name="Grande", price=Decimal("30000"))
        db.commit()
        sale = Sale(
            id=uuid4(), cash_shift_id=shift.id, user_id=uuid4(), user_name="Cajero de prueba",
            customer_name="Mostrador", subtotal=Decimal("30000"), total=Decimal("30000"),
            status="paid", sold_at=venta,
        )
        db.add(sale)
        db.flush()
        db.add(SaleItem(
            id=uuid4(), sale_id=sale.id, product_variant_id=variant.id, description="Cono - Grande",
            options=[], quantity=1, unit_price=Decimal("30000"), line_total=Decimal("30000"),
        ))
        db.commit()
        return apertura, venta

    def test_la_venta_de_las_00_30_cuenta_en_el_dia_de_apertura_y_no_en_el_siguiente(self):
        for zona in ZONAS_DE_PRUEBA:
            with self.subTest(zona=zona):
                db = fx.new_session()
                apertura, venta = self._seed(db, zona)
                tenant = tenant_en(zona)
                # La venta cae realmente en el día local SIGUIENTE al de apertura.
                tz = resolve_timezone(tenant)
                self.assertEqual(venta.replace(tzinfo=timezone.utc).astimezone(tz).date(), self.DIA + timedelta(days=1))

                dia_apertura = reports.compute_period_sales_report(db, tenant, "day", ref_date=self.DIA)
                self.assertEqual(dia_apertura["total_sales"], Decimal("30000"))
                self.assertEqual(dia_apertura["order_count"], 1)

                dia_siguiente = reports.compute_period_sales_report(
                    db, tenant, "day", ref_date=self.DIA + timedelta(days=1)
                )
                self.assertEqual(dia_siguiente["total_sales"], Decimal("0"))
                self.assertEqual(dia_siguiente["order_count"], 0)


if __name__ == "__main__":
    unittest.main()
