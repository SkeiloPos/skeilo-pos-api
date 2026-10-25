"""Spec 135 (US2): un ingreso de 1 sola línea en un método distinto a
efectivo -- hoy el ingreso solo admitía efectivo. Cubre el acceptance
scenario principal de la historia y el control de no regresión (ingreso en
efectivo se comporta idéntico al actual, FR-009).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_cash_movement_non_cash_income -v
"""
from decimal import Decimal
import unittest

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.cash import router, service
from app.api.v1.cash.schemas import CashMovementIn, CashMovementLineIn


class TestCashMovementNonCashIncome(unittest.TestCase):
    def _seed(self):
        db = fx.new_session()
        register = fx.make_cash_register(db)
        shift = fx.make_cash_shift(db, register=register)
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True, type="cash")
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()
        cashier = fx.make_user_double()
        return db, shift, efectivo, nequi, cashier

    def test_ingreso_no_efectivo_no_afecta_expected_y_aparece_en_ingresos_by_method(self):
        db, shift, efectivo, nequi, cashier = self._seed()
        before = service.reconcile(db, shift)

        body = CashMovementIn(
            kind="ingreso", category="Aporte turno", description="Préstamo",
            lines=[CashMovementLineIn(payment_method_id=nequi.id, amount=Decimal("50000"))],
        )
        movimientos = router.add_movement(shift.id, body, db, cashier)

        self.assertEqual(len(movimientos), 1)
        mov = movimientos[0]
        self.assertEqual(mov.payment_method_id, nequi.id)
        self.assertIsNone(mov.group_id)

        after = service.reconcile(db, shift)
        self.assertEqual(after["ingresos"], before["ingresos"])
        self.assertEqual(after["expected"], before["expected"])
        nequi_row = next(r for r in after["ingresos_by_method"] if r["method_id"] == nequi.id)
        self.assertEqual(nequi_row["total"], Decimal("50000"))
        self.assertEqual(nequi_row["count"], 1)

    def test_control_ingreso_en_efectivo_se_comporta_igual_que_antes(self):
        """FR-009: el payload de compatibilidad (`amount`, sin `lines`) sigue
        creando 1 fila en efectivo que sí sube `expected`."""
        db, shift, efectivo, nequi, cashier = self._seed()
        before = service.reconcile(db, shift)

        body = CashMovementIn(kind="ingreso", category="Aporte turno", amount=Decimal("30000"))
        movimientos = router.add_movement(shift.id, body, db, cashier)

        self.assertEqual(len(movimientos), 1)
        mov = movimientos[0]
        self.assertEqual(mov.payment_method_id, efectivo.id)
        self.assertIsNone(mov.group_id)

        after = service.reconcile(db, shift)
        self.assertEqual(after["ingresos"], before["ingresos"] + Decimal("30000"))
        self.assertEqual(after["expected"], before["expected"] + Decimal("30000"))


if __name__ == "__main__":
    unittest.main()
