"""Test de cobro de pedido con múltiples métodos de pago (spec 112).

No hay pytest en el proyecto, así que es un script autoejecutable:

    python -m app.scripts.test_mixed_payment_checkout

Cubre los Acceptance Scenarios de las Historias 1, 2 y 5 de la spec: `build_sale`
(vía `checkout.pay_order`) ya aceptaba N pagos desde spec 011 (research.md D1) —
este script verifica que sigue siendo así tras el tope `max_length=3` agregado a
`PayIn`/`CheckoutAndSendIn`/`SaleCreate`, para los tres tipos de pedido (Mesa,
Para llevar, Domicilio), que un cobro de un solo método sigue creando una única
fila `Payment` (US2, sin regresión), y que `release_table` sigue liberando la
mesa tras un cobro mixto (US5).

Reutiliza el mismo harness SQLite-en-memoria que ya usan los characterization
tests de `orders` (`orders_fixtures`), igual que `test_cash_net_sales.py`
(spec 108) — no necesita un tenant real en Postgres.
"""
from decimal import Decimal

from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1.orders import checkout
from app.api.v1.orders.schemas import CheckoutAndSendIn, PayIn
from app.api.v1.sales.schemas import PaymentIn, SaleCreate
from app.characterization_tests import orders_fixtures as fx

PRECIO = Decimal("10000")


def _pago(method_id, amount, reference=None) -> PaymentIn:
    return PaymentIn(payment_method_id=method_id, amount=amount, reference=reference)


def _seed_order(*, order_type="DINE_IN", delivery_fee=None):
    """Mesa ocupada + sesión + orden 'bloqueada', con un ítem cobrable de
    inmediato -- mismo patrón que `test_orders_checkout.py::_seed_order_con_receta`,
    sin receta (este script no ejercita inventario)."""
    db = fx.new_session()
    table = fx.make_dining_table(db, status="ocupada")
    ts = fx.make_table_session(db, table=table)
    category = fx.make_category(db)
    product = fx.make_product(db, category=category)
    variant = fx.make_variant(db, product=product, price=PRECIO)
    order = fx.make_customer_order(db, ts, status="bloqueada", channel="POS", order_type=order_type)
    fx.make_order_item(db, order, variant, quantity=1)
    if delivery_fee is not None:
        order.delivery_fee = delivery_fee
    register = fx.make_cash_register(db)
    shift = fx.make_cash_shift(db, register=register)
    efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True, type="cash")
    transferencia = fx.make_payment_method(db, name="Transferencia Nequi", is_cash=False, type="transfer")
    db.commit()
    cashier = fx.make_user_double()
    return dict(
        db=db, table=table, order=order, shift=shift,
        efectivo=efectivo, transferencia=transferencia, cashier=cashier,
    )


def _test_pago_mixto_crea_dos_filas_payment(order_type: str, total: Decimal) -> None:
    """Historia 1 (Esquema de escenario): efectivo + transferencia sumando el
    total -> el pedido queda 'pagada' con 2 filas `Payment` que suman el total,
    para Mesa (DINE_IN), Para llevar (TAKEAWAY) y Domicilio (DELIVERY)."""
    delivery_fee = Decimal("3000") if order_type == "DELIVERY" else None
    s = _seed_order(order_type=order_type, delivery_fee=delivery_fee)
    efectivo_monto = total - Decimal("6000")
    data = PayIn(
        cash_shift_id=s["shift"].id,
        payments=[
            _pago(s["efectivo"].id, efectivo_monto),
            _pago(s["transferencia"].id, Decimal("6000"), reference="4821"),
        ],
    )
    sale = checkout.pay_order(s["db"], s["order"].id, data, s["cashier"])

    assert sale.total == total, f"total esperado {total}, fue {sale.total}"
    assert len(sale.payments) == 2, f"se esperaban 2 pagos, hubo {len(sale.payments)}"
    assert sum(p.amount for p in sale.payments) == total
    s["db"].refresh(s["order"])
    assert s["order"].status == "pagada"
    print(f"OK: pago_mixto_crea_dos_filas_payment [{order_type}]")


def test_pago_mixto_mesa() -> None:
    _test_pago_mixto_crea_dos_filas_payment("DINE_IN", PRECIO)


def test_pago_mixto_para_llevar() -> None:
    _test_pago_mixto_crea_dos_filas_payment("TAKEAWAY", PRECIO)


def test_pago_mixto_domicilio_incluye_envio() -> None:
    """Escenario 'El total de domicilio incluye el costo de envío': el total
    exigido (RN10/FR-016) ya suma productos + `delivery_fee`."""
    _test_pago_mixto_crea_dos_filas_payment("DELIVERY", PRECIO + Decimal("3000"))


def test_un_solo_pago_sigue_creando_una_fila() -> None:
    """US2 (sin regresión): cobrar con un único método sigue creando
    exactamente 1 fila `Payment`, igual que antes de este fix."""
    s = _seed_order()
    data = PayIn(cash_shift_id=s["shift"].id, payments=[_pago(s["efectivo"].id, PRECIO)])
    sale = checkout.pay_order(s["db"], s["order"].id, data, s["cashier"])

    assert len(sale.payments) == 1, f"se esperaba 1 pago, hubo {len(sale.payments)}"
    assert sale.payments[0].amount == PRECIO
    print("OK: un_solo_pago_sigue_creando_una_fila")


def test_cuarta_linea_de_pago_rechazada_422() -> None:
    """RN3/FR-004: `max_length=3` en `PayIn`, `CheckoutAndSendIn` y
    `SaleCreate` -- una 4ta línea de pago se rechaza como error de
    validación (422 cuando llega vía FastAPI), antes de llegar a `build_sale`."""
    cuatro_pagos = [_pago(fx._uid(), Decimal("1000")) for _ in range(4)]

    for schema_cls, kwargs in (
        (PayIn, dict(cash_shift_id=fx._uid())),
        (CheckoutAndSendIn, dict(version=0, cash_shift_id=fx._uid())),
        (SaleCreate, dict(cash_shift_id=fx._uid(), items=[])),
    ):
        try:
            schema_cls(**kwargs, payments=cuatro_pagos)
        except ValidationError:
            pass
        else:
            raise AssertionError(f"{schema_cls.__name__} debía rechazar 4 líneas de pago")
    print("OK: cuarta_linea_de_pago_rechazada_422")


def test_tres_lineas_de_pago_aceptadas() -> None:
    """RN3: el tope es 3, no menos -- 3 líneas deben seguir siendo válidas."""
    tres_pagos = [_pago(fx._uid(), Decimal("1000")) for _ in range(3)]
    PayIn(cash_shift_id=fx._uid(), payments=tres_pagos)
    print("OK: tres_lineas_de_pago_aceptadas")


def test_dos_lineas_mismo_metodo_rechazadas_422() -> None:
    """spec 135 (A-146, revierte A-124/FR-005 de spec 112): 2 líneas con el
    mismo método se rechazan con 422 y el pedido queda sin pagos."""
    s = _seed_order()
    data = PayIn(
        cash_shift_id=s["shift"].id,
        payments=[
            _pago(s["transferencia"].id, Decimal("4000")),
            _pago(s["transferencia"].id, Decimal("6000")),
        ],
    )
    try:
        checkout.pay_order(s["db"], s["order"].id, data, s["cashier"])
    except HTTPException as exc:
        assert exc.status_code == 422, f"se esperaba 422, fue {exc.status_code}"
        assert "Transferencia Nequi" in exc.detail, exc.detail
    else:
        raise AssertionError("debía rechazar 2 líneas con el mismo método")
    s["db"].refresh(s["order"])
    assert s["order"].status == "bloqueada", f"el pedido no debía quedar pagado, quedó {s['order'].status}"
    print("OK: dos_lineas_mismo_metodo_rechazadas_422")


def test_tres_lineas_dos_repiten_metodo_rechazadas_422() -> None:
    """FR-003: la regla aplica sin importar cuántas líneas tenga el cobro --
    3 líneas donde 2 de las 3 repiten método también se rechazan."""
    s = _seed_order()
    efectivo_extra = fx.make_payment_method(s["db"], name="Efectivo 2", is_cash=False, type="other")
    s["db"].commit()
    data = PayIn(
        cash_shift_id=s["shift"].id,
        payments=[
            _pago(s["transferencia"].id, Decimal("3000")),
            _pago(s["transferencia"].id, Decimal("3000")),
            _pago(efectivo_extra.id, Decimal("4000")),
        ],
    )
    try:
        checkout.pay_order(s["db"], s["order"].id, data, s["cashier"])
    except HTTPException as exc:
        assert exc.status_code == 422, f"se esperaba 422, fue {exc.status_code}"
    else:
        raise AssertionError("debía rechazar 3 líneas donde 2 repiten método")
    print("OK: tres_lineas_dos_repiten_metodo_rechazadas_422")


def test_release_table_tras_pago_mixto() -> None:
    """US5/RN12/FR-018: `release_table` sigue liberando la mesa tras un
    cobro con 2 líneas de pago, igual que con una sola (no depende de
    ningún campo de método único)."""
    s = _seed_order()
    data = PayIn(
        cash_shift_id=s["shift"].id,
        payments=[
            _pago(s["efectivo"].id, Decimal("4000")),
            _pago(s["transferencia"].id, Decimal("6000")),
        ],
    )
    checkout.pay_order(s["db"], s["order"].id, data, s["cashier"])

    result = checkout.release_table(s["db"], s["table"].id, closed_by=s["cashier"])
    assert result.status == "libre", f"la mesa debía quedar libre, quedó {result.status}"
    print("OK: release_table_tras_pago_mixto")


def main() -> None:
    tests = [
        test_pago_mixto_mesa,
        test_pago_mixto_para_llevar,
        test_pago_mixto_domicilio_incluye_envio,
        test_un_solo_pago_sigue_creando_una_fila,
        test_cuarta_linea_de_pago_rechazada_422,
        test_tres_lineas_de_pago_aceptadas,
        test_dos_lineas_mismo_metodo_rechazadas_422,
        test_tres_lineas_dos_repiten_metodo_rechazadas_422,
        test_release_table_tras_pago_mixto,
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
