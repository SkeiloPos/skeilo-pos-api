"""Spec 135 (US3): un ingreso mixto de 2 líneas con métodos distintos --
mismo `group_id`, `expected` sube solo la porción efectivo, rechazo por
método repetido (FR-006, mismo criterio que US1/A-146) y por una 3ra línea
(tope de Pydantic, `max_length=2`).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_cash_movement_mixed_income -v
"""
from decimal import Decimal
import unittest

from fastapi import HTTPException
from pydantic import ValidationError

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.cash import router, service
from app.api.v1.cash.schemas import CashMovementIn, CashMovementLineIn
from app.models.cash_movement import CashMovement


class TestCashMovementMixedIncome(unittest.TestCase):
    def _seed(self):
        db = fx.new_session()
        register = fx.make_cash_register(db)
        shift = fx.make_cash_shift(db, register=register)
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True, type="cash")
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()
        cashier = fx.make_user_double()
        return db, shift, efectivo, nequi, cashier

    def test_ingreso_mixto_crea_2_filas_con_mismo_group_id(self):
        db, shift, efectivo, nequi, cashier = self._seed()
        before = service.reconcile(db, shift)

        body = CashMovementIn(
            kind="ingreso", category="Aporte turno", lines=[
                CashMovementLineIn(payment_method_id=efectivo.id, amount=Decimal("20000")),
                CashMovementLineIn(payment_method_id=nequi.id, amount=Decimal("30000")),
            ],
        )
        movimientos = router.add_movement(shift.id, body, db, cashier)

        self.assertEqual(len(movimientos), 2)
        self.assertIsNotNone(movimientos[0].group_id)
        self.assertEqual(movimientos[0].group_id, movimientos[1].group_id)
        metodos = {m.payment_method_id for m in movimientos}
        self.assertEqual(metodos, {efectivo.id, nequi.id})

        after = service.reconcile(db, shift)
        self.assertEqual(after["ingresos"], before["ingresos"] + Decimal("20000"))
        self.assertEqual(after["expected"], before["expected"] + Decimal("20000"))
        nequi_row = next(r for r in after["ingresos_by_method"] if r["method_id"] == nequi.id)
        self.assertEqual(nequi_row["total"], Decimal("30000"))

    def test_rechaza_dos_lineas_con_el_mismo_metodo(self):
        db, shift, efectivo, nequi, cashier = self._seed()
        body = CashMovementIn(
            kind="ingreso", category="Aporte turno", lines=[
                CashMovementLineIn(payment_method_id=efectivo.id, amount=Decimal("20000")),
                CashMovementLineIn(payment_method_id=efectivo.id, amount=Decimal("30000")),
            ],
        )

        with self.assertRaises(HTTPException) as ctx:
            router.add_movement(shift.id, body, db, cashier)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("Efectivo", ctx.exception.detail)
        self.assertEqual(db.query(CashMovement).count(), 0)

    def test_rechaza_tres_lineas_con_error_de_pydantic(self):
        with self.assertRaises(ValidationError):
            CashMovementIn(
                kind="ingreso", category="Aporte turno", lines=[
                    CashMovementLineIn(payment_method_id=fx._uid(), amount=Decimal("1000")),
                    CashMovementLineIn(payment_method_id=fx._uid(), amount=Decimal("1000")),
                    CashMovementLineIn(payment_method_id=fx._uid(), amount=Decimal("1000")),
                ],
            )


if __name__ == "__main__":
    unittest.main()
