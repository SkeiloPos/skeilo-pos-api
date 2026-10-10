"""Test de ventas en efectivo por valor neto en el resumen de caja (spec 108).

No hay pytest en el proyecto, así que es un script autoejecutable:

    python -m app.scripts.test_cash_net_sales

Cubre los Acceptance Scenarios de las 3 historias de usuario de la spec:
`sales_by_method[cash].total` reporta el neto, no el bruto (US1); `expected`
no cambia respecto al cálculo anterior al hotfix (US2, research.md D3); y el
invariante `sales_by_method[cash].total == ventas_efectivo == cash_sales`
(data-model.md) se cumple siempre.

A diferencia de `test_purchase_presentation.py` (spec 107), no necesita un
tenant real en Postgres: `reconcile()` solo lee `Sale`/`Payment`/`CashMovement`
vía el ORM, así que basta el mismo harness SQLite-en-memoria que ya usan los
characterization tests de `orders` (`orders_fixtures.py`), ampliado con la
tabla `cash_movements` que ese harness no necesitaba hasta ahora.
"""
from decimal import Decimal

from sqlalchemy.orm import Session

from app.api.v1.cash.service import reconcile
from app.characterization_tests.orders_fixtures import (
    new_session, make_cash_shift, make_payment_method, make_sale,
)
from app.models.cash_movement import CashMovement
from app.models.payment import Payment


def _session() -> Session:
    """`orders_fixtures.new_session()` no incluye `cash_movements` -- ningún
    test de `orders` siembra movimientos manuales. `reconcile()` sí los
    consulta (ingresos/egresos/retiros), así que se crea aparte sobre el
    mismo bind, con el mismo `schema_translate_map` que ya trae la sesión."""
    db = new_session()
    CashMovement.__table__.create(bind=db.get_bind(), checkfirst=True)
    return db


def _pay_cash(db: Session, sale, method, amount: Decimal) -> None:
    db.add(Payment(sale_id=sale.id, payment_method_id=method.id, amount=amount))
    db.flush()


def test_tarjeta_muestra_neto_no_bruto() -> None:
    """Escenario 1 de la Historia 1: Factura 25 = $17.000 (pago exacto) +
    Factura 23 = $28.000 recibiendo $31.000 ($3.000 de cambio) -> la fila
    `cash` de `sales_by_method` debe ser $45.000, no $48.000."""
    db = _session()
    shift = make_cash_shift(db, opening_amount=Decimal("100000"))
    efectivo = make_payment_method(db, name="Efectivo", type="cash", is_cash=True)

    factura_25 = make_sale(db, cash_shift_id=shift.id, total=Decimal("17000"),
                            change_given=Decimal("0"))
    _pay_cash(db, factura_25, efectivo, Decimal("17000"))

    factura_23 = make_sale(db, cash_shift_id=shift.id, total=Decimal("28000"),
                            change_given=Decimal("3000"))
    _pay_cash(db, factura_23, efectivo, Decimal("31000"))

    db.commit()
    recon = reconcile(db, shift)

    cash_rows = [m for m in recon["sales_by_method"] if m["method_type"] == "cash"]
    assert len(cash_rows) == 1, f"se esperaba 1 fila cash, hubo {len(cash_rows)}"
    assert cash_rows[0]["total"] == Decimal("45000"), (
        f"sales_by_method[cash].total debía ser 45000 (neto), fue {cash_rows[0]['total']}"
    )
    assert cash_rows[0]["count"] == 2
    print("OK: tarjeta_muestra_neto_no_bruto (45000, no 48000)")


def test_sin_ventas_efectivo_muestra_cero() -> None:
    """Escenario 2 de la Historia 1 / edge case del spec: turno sin ninguna
    venta en efectivo -> la fila `cash` existe (método activo) en $0."""
    db = _session()
    shift = make_cash_shift(db, opening_amount=Decimal("100000"))
    make_payment_method(db, name="Efectivo", type="cash", is_cash=True)

    db.commit()
    recon = reconcile(db, shift)

    cash_rows = [m for m in recon["sales_by_method"] if m["method_type"] == "cash"]
    assert len(cash_rows) == 1
    assert cash_rows[0]["total"] == Decimal("0")
    assert cash_rows[0]["count"] == 0
    print("OK: sin_ventas_efectivo_muestra_cero")


def test_expected_no_cambia_respecto_al_hotfix() -> None:
    """Historia 2, escenario 2 / research.md D3: con el turno de la primera
    prueba (Fondo inicial $100.000, sin ingresos/egresos/retiros), `expected`
    sigue siendo $145.000 -- el mismo valor que el sistema ya daba antes del
    hotfix para el mismo turno. El hotfix es de presentación, no de cálculo."""
    db = _session()
    shift = make_cash_shift(db, opening_amount=Decimal("100000"))
    efectivo = make_payment_method(db, name="Efectivo", type="cash", is_cash=True)

    factura_25 = make_sale(db, cash_shift_id=shift.id, total=Decimal("17000"),
                            change_given=Decimal("0"))
    _pay_cash(db, factura_25, efectivo, Decimal("17000"))

    factura_23 = make_sale(db, cash_shift_id=shift.id, total=Decimal("28000"),
                            change_given=Decimal("3000"))
    _pay_cash(db, factura_23, efectivo, Decimal("31000"))

    db.commit()
    recon = reconcile(db, shift)

    assert recon["expected"] == Decimal("145000"), (
        f"expected debía seguir en 145000, fue {recon['expected']}"
    )
    assert recon["ventas_efectivo"] == Decimal("45000")
    assert recon["cambio_entregado"] == Decimal("3000")
    print("OK: expected_no_cambia_respecto_al_hotfix (145000)")


def test_invariante_sales_by_method_igual_a_ventas_efectivo() -> None:
    """data-model.md: para cualquier turno, sum(sales_by_method para cash) ==
    ventas_efectivo == cash_sales (el alias deprecado, A-40)."""
    db = _session()
    shift = make_cash_shift(db, opening_amount=Decimal("50000"))
    efectivo = make_payment_method(db, name="Efectivo", type="cash", is_cash=True)

    sale = make_sale(db, cash_shift_id=shift.id, total=Decimal("9000"),
                      change_given=Decimal("1000"))
    _pay_cash(db, sale, efectivo, Decimal("10000"))

    db.commit()
    recon = reconcile(db, shift)

    cash_total = sum(
        m["total"] for m in recon["sales_by_method"] if m["method_type"] == "cash"
    )
    assert cash_total == recon["ventas_efectivo"] == recon["cash_sales"] == Decimal("9000")
    print("OK: invariante_sales_by_method_igual_a_ventas_efectivo")


def test_venta_exacta_sin_cambio_no_cambia() -> None:
    """Edge case del spec: pago exacto (sin cambio) -> el neto y el bruto
    coinciden, sin diferencia respecto al comportamiento anterior al hotfix."""
    db = _session()
    shift = make_cash_shift(db, opening_amount=Decimal("0"))
    efectivo = make_payment_method(db, name="Efectivo", type="cash", is_cash=True)

    sale = make_sale(db, cash_shift_id=shift.id, total=Decimal("12000"),
                      change_given=Decimal("0"))
    _pay_cash(db, sale, efectivo, Decimal("12000"))

    db.commit()
    recon = reconcile(db, shift)

    cash_rows = [m for m in recon["sales_by_method"] if m["method_type"] == "cash"]
    assert cash_rows[0]["total"] == Decimal("12000")
    print("OK: venta_exacta_sin_cambio_no_cambia")


def main() -> None:
    tests = [
        test_tarjeta_muestra_neto_no_bruto,
        test_sin_ventas_efectivo_muestra_cero,
        test_expected_no_cambia_respecto_al_hotfix,
        test_invariante_sales_by_method_igual_a_ventas_efectivo,
        test_venta_exacta_sin_cambio_no_cambia,
    ]
    fallos = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            fallos += 1
            print(f"FALLÓ: {test.__name__}: {exc}")
    if fallos:
        raise SystemExit(f"{fallos}/{len(tests)} pruebas fallaron")
    print(f"Todas las {len(tests)} pruebas pasaron.")


if __name__ == "__main__":
    main()
