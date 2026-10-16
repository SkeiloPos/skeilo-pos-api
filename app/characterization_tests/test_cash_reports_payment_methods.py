"""`GET /cash/reports/payment-methods` (spec 122, FR-007,
contracts/payment-methods-report-api.md): ingresos por método de pago, con la
unidad de bucket (hora/día/semana) que corresponda al período resuelto.

Invoca la función de endpoint directamente como función Python (mismo patrón
que `test_cash_report_batch_identity.py`).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_cash_reports_payment_methods -v
"""
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.cash import router
from app.models.payment import Payment


def _seed_sale(db, shift, *, cash_shift_id=None, sold_at=None):
    return fx.make_sale(
        db, cash_shift_id=(cash_shift_id or shift.id),
        total=Decimal("0"), sold_at=sold_at or datetime.now(),
    )


def _seed_payment(db, sale, method, *, amount: Decimal, paid_at=None):
    p = Payment(
        id=uuid4(), sale_id=sale.id, payment_method_id=method.id,
        amount=amount, paid_at=paid_at or datetime.now(),
    )
    db.add(p)
    db.flush()
    return p


def _call(db, tenant, *, period, shift_id=None, date_=None, date_from=None,
          date_to=None, cash_register_id=None):
    return router.payment_methods_report(
        period=period, shift_id=shift_id, date=date_, date_from=date_from,
        date_to=date_to, cash_register_id=cash_register_id,
        db=db, tenant=tenant, _=fx.make_user_double(),
    )


class TestGranularidadPorPeriodo(unittest.TestCase):
    def test_turno_especifico_usa_bucket_por_hora(self):
        db = fx.new_session()
        shift = fx.make_cash_shift(db)
        method = fx.make_payment_method(db, name="Efectivo")
        db.commit()
        sale = _seed_sale(db, shift)
        _seed_payment(db, sale, method, amount=Decimal("10000"))
        db.commit()

        result = _call(db, fx.make_tenant_double(), period="shift", shift_id=shift.id)

        self.assertEqual(result["bucket_unit"], "hour")
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["amount"], Decimal("10000"))

    def test_semana_usa_bucket_por_dia(self):
        db = fx.new_session()
        shift = fx.make_cash_shift(db)
        method = fx.make_payment_method(db, name="Efectivo")
        db.commit()
        sale = _seed_sale(db, shift)
        _seed_payment(db, sale, method, amount=Decimal("5000"))
        db.commit()

        today = date.today()
        result = _call(db, fx.make_tenant_double(), period="week", date_=today)

        self.assertEqual(result["bucket_unit"], "day")

    def test_mes_usa_bucket_por_semana(self):
        db = fx.new_session()
        shift = fx.make_cash_shift(db)
        method = fx.make_payment_method(db, name="Efectivo")
        db.commit()
        sale = _seed_sale(db, shift)
        _seed_payment(db, sale, method, amount=Decimal("5000"))
        db.commit()

        today = date.today()
        result = _call(db, fx.make_tenant_double(), period="month", date_=today)

        self.assertEqual(result["bucket_unit"], "week")

    def test_rango_personalizado_usa_bucket_por_dia(self):
        db = fx.new_session()
        shift = fx.make_cash_shift(db)
        method = fx.make_payment_method(db, name="Efectivo")
        db.commit()
        sale = _seed_sale(db, shift)
        _seed_payment(db, sale, method, amount=Decimal("5000"))
        db.commit()

        today = date.today()
        result = _call(
            db, fx.make_tenant_double(), period="range",
            date_from=today - timedelta(days=3), date_to=today,
        )

        self.assertEqual(result["bucket_unit"], "day")


class TestPagoDividido(unittest.TestCase):
    def test_un_pago_dividido_entre_dos_metodos_se_atribuye_a_cada_uno(self):
        # research.md §6: debe sumarse Payment.amount por método, no Sale.total
        # -- si se leyera Sale.total, un pago dividido se contaría mal.
        db = fx.new_session()
        shift = fx.make_cash_shift(db)
        efectivo = fx.make_payment_method(db, name="Efectivo")
        tarjeta = fx.make_payment_method(db, name="Tarjeta", is_cash=False)
        db.commit()
        sale = _seed_sale(db, shift)
        _seed_payment(db, sale, efectivo, amount=Decimal("6000"))
        _seed_payment(db, sale, tarjeta, amount=Decimal("4000"))
        db.commit()

        result = _call(db, fx.make_tenant_double(), period="shift", shift_id=shift.id)

        by_method = {r["payment_method_name"]: r["amount"] for r in result["rows"]}
        self.assertEqual(by_method["Efectivo"], Decimal("6000"))
        self.assertEqual(by_method["Tarjeta"], Decimal("4000"))
        self.assertEqual(sum(by_method.values()), Decimal("10000"))


class TestSinPagos(unittest.TestCase):
    def test_un_turno_sin_pagos_devuelve_rows_vacio_no_404(self):
        db = fx.new_session()
        shift = fx.make_cash_shift(db)
        db.commit()

        result = _call(db, fx.make_tenant_double(), period="shift", shift_id=shift.id)

        self.assertEqual(result["rows"], [])


class TestValidacion(unittest.TestCase):
    def test_period_shift_sin_shift_id_da_422(self):
        db = fx.new_session()
        db.commit()
        with self.assertRaises(HTTPException) as ctx:
            _call(db, fx.make_tenant_double(), period="shift")
        self.assertEqual(ctx.exception.status_code, 422)

    def test_shift_id_inexistente_da_404(self):
        db = fx.new_session()
        db.commit()
        with self.assertRaises(HTTPException) as ctx:
            _call(db, fx.make_tenant_double(), period="shift", shift_id=uuid4())
        self.assertEqual(ctx.exception.status_code, 404)

    def test_period_range_sin_fechas_da_422(self):
        db = fx.new_session()
        db.commit()
        with self.assertRaises(HTTPException) as ctx:
            _call(db, fx.make_tenant_double(), period="range")
        self.assertEqual(ctx.exception.status_code, 422)

    def test_period_range_date_from_posterior_a_date_to_da_422(self):
        db = fx.new_session()
        db.commit()
        today = date.today()
        with self.assertRaises(HTTPException) as ctx:
            _call(db, fx.make_tenant_double(), period="range", date_from=today, date_to=today - timedelta(days=1))
        self.assertEqual(ctx.exception.status_code, 422)

    def test_cash_register_id_inexistente_da_404(self):
        db = fx.new_session()
        db.commit()
        with self.assertRaises(HTTPException) as ctx:
            _call(db, fx.make_tenant_double(), period="current_shift", cash_register_id=uuid4())
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
