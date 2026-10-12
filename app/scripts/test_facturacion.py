"""Test de la emisión automática de facturas.

    python -m app.scripts.test_facturacion

Antes, la factura solo existía si alguien pulsaba un botón, y ese botón partía del
**pedido**: buscaba la venta por `customer_order_id`, enlace que casi nunca existe
—mostrador nunca lo pone, el cierre `split` tampoco—. Resultado: 20 ventas reales,
cero facturas.

Ahora se emite dentro de la transacción del cobro, en `build_sale`, que es el paso
común de los cuatro caminos. Este test comprueba lo que eso implica:

  - una factura por **venta**, no por pedido (un `split` de dos comensales → dos);
  - consecutivo sin huecos ni repeticiones, por prefijo;
  - atomicidad: si el cobro falla, ni venta ni factura, y el contador no avanza;
  - idempotencia por `sale_id`.

Crea sus propios datos desechables y los borra al terminar.
"""
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text

from app.core.db import with_db
from app.core.models import User
from app.models.cash_register import CashRegister
from app.models.cash_shift import CashShift
from app.models.invoice import Invoice, InvoiceCounter
from app.models.payment import PaymentMethod
from app.models.product_variant import ProductVariant
from app.models.sale import Sale
from app.api.v1.sales.builder import SaleLine, build_sale
from app.api.v1.invoices.service import issue_for_sale

PREFIJO = f"T{uuid4().hex[:4].upper()}"


def _schema() -> str:
    with with_db(None) as db:
        row = db.execute(text("SELECT schema FROM shared.tenants ORDER BY id LIMIT 1")).scalar()
    if row is None:
        raise SystemExit("No hay tenants en shared.tenants.")
    return row


def _check(label, actual, esperado):
    if actual != esperado:
        raise AssertionError(f"{label}: esperado {esperado}, obtenido {actual}")
    print(f"  ok  · {label}")


class _Pago:
    """Imita el schema `PaymentIn` que espera `build_sale`."""
    def __init__(self, method_id, amount):
        self.payment_method_id = method_id
        self.amount = Decimal(amount)
        self.reference = None


def _fixture(db):
    user = db.execute(select(User).limit(1)).scalar_one_or_none()
    if user is None:
        raise SystemExit("No hay usuarios en shared.users.")

    method = db.execute(select(PaymentMethod).where(PaymentMethod.is_cash)).scalars().first()
    if method is None:
        raise SystemExit("El tenant no tiene método de pago en efectivo.")

    reg = db.execute(select(CashRegister).limit(1)).scalar_one_or_none()
    if reg is None:
        reg = CashRegister(name=f"caja-fact-{uuid4().hex[:6]}")
        db.add(reg); db.flush()

    shift = CashShift(
        cash_register_id=reg.id, user_id=user.id, user_name=user.name,
        opening_amount=Decimal("100000"), status="open",
    )
    db.add(shift); db.flush()

    # `sale_items.product_variant_id` tiene FK real: hace falta una variante viva.
    variant = db.execute(select(ProductVariant).limit(1)).scalar_one_or_none()
    if variant is None:
        raise SystemExit("El tenant no tiene variantes de producto.")

    return user, method, shift, variant


def _line(variant_id, precio="10.00", qty=1) -> SaleLine:
    return SaleLine(
        product_variant_id=variant_id, description="producto de prueba",
        options=[], quantity=qty, unit_price=Decimal(precio),
    )


def _factura(db, sale_id) -> Invoice | None:
    db.flush()
    return db.execute(select(Invoice).where(Invoice.sale_id == sale_id)).scalar_one_or_none()


def _cleanup(schema, shift_id):
    with with_db(schema) as db:
        s = str(shift_id)
        db.execute(text(f'''DELETE FROM "{schema}".invoices WHERE sale_id IN (
            SELECT id FROM "{schema}".sales WHERE cash_shift_id = :s)'''), {"s": s})
        db.execute(text(f'''DELETE FROM "{schema}".payments WHERE sale_id IN (
            SELECT id FROM "{schema}".sales WHERE cash_shift_id = :s)'''), {"s": s})
        db.execute(text(f'''DELETE FROM "{schema}".sale_items WHERE sale_id IN (
            SELECT id FROM "{schema}".sales WHERE cash_shift_id = :s)'''), {"s": s})
        db.execute(text(f'DELETE FROM "{schema}".sales WHERE cash_shift_id = :s'), {"s": s})
        db.execute(text(f'DELETE FROM "{schema}".cash_shifts WHERE id = :s'), {"s": s})
        db.execute(text(f'DELETE FROM "{schema}".invoice_counters WHERE prefix = :p'), {"p": PREFIJO})
        db.commit()


def main():
    schema = _schema()
    print(f"Facturación automática al cobrar (tenant: {schema}, prefijo {PREFIJO})")

    with with_db(schema) as db:
        user, method, shift, variant = _fixture(db)
        db.commit()
        shift_id, user_id, method_id, variant_id = shift.id, user.id, method.id, variant.id

    try:
        # --- 1. toda venta emite factura -------------------------------------
        with with_db(schema) as db:
            sale = build_sale(
                db, lines=[_line(variant_id)], shift=db.get(CashShift, shift_id),
                cashier=db.get(User, user_id), payments=[_Pago(method_id, "10.00")],
                invoice_prefix=PREFIJO,
            )
            inv = _factura(db, sale.id)
            _check("cobrar emite factura", inv is not None, True)
            _check("con el prefijo del tenant", inv.prefix, PREFIJO)
            _check("empezando en el número 1", inv.number, 1)
            _check("y copiando el total de la venta", inv.total, sale.total)
            db.commit()

        # --- 2. consecutivo sin huecos ---------------------------------------
        with with_db(schema) as db:
            numeros = []
            for _ in range(2):
                s = build_sale(
                    db, lines=[_line(variant_id)], shift=db.get(CashShift, shift_id),
                    cashier=db.get(User, user_id), payments=[_Pago(method_id, "10.00")],
                    invoice_prefix=PREFIJO,
                )
                numeros.append(_factura(db, s.id).number)
            db.commit()
            _check("el consecutivo avanza sin huecos", numeros, [2, 3])

        # --- 3. atomicidad: si el cobro falla, no se consume número ----------
        with with_db(schema) as db:
            antes = db.execute(
                select(InvoiceCounter.next_number).where(InvoiceCounter.prefix == PREFIJO)
            ).scalar()
        with with_db(schema) as db:
            try:
                # Pago insuficiente: build_sale lanza 422 después de haber creado
                # la venta en memoria.
                build_sale(
                    db, lines=[_line(variant_id, "50.00")], shift=db.get(CashShift, shift_id),
                    cashier=db.get(User, user_id), payments=[_Pago(method_id, "1.00")],
                    invoice_prefix=PREFIJO,
                )
                raise AssertionError("el pago insuficiente no lanzó 422")
            except HTTPException as e:
                _check("pago insuficiente → 422", e.status_code, 422)
            db.rollback()

        with with_db(schema) as db:
            despues = db.execute(
                select(InvoiceCounter.next_number).where(InvoiceCounter.prefix == PREFIJO)
            ).scalar()
            _check("un cobro fallido NO consume número", despues, antes)
            _check("ni deja facturas huérfanas",
                   db.execute(text(f'''SELECT count(*) FROM "{schema}".invoices i
                       LEFT JOIN "{schema}".sales s ON s.id = i.sale_id
                       WHERE s.id IS NULL''')).scalar(), 0)

        # --- 4. idempotencia por sale_id -------------------------------------
        with with_db(schema) as db:
            sale = db.execute(
                select(Sale).where(Sale.cash_shift_id == shift_id).limit(1)
            ).scalar_one()
            inv1 = _factura(db, sale.id)
            inv2 = issue_for_sale(db, sale, user=db.get(User, user_id), prefix=PREFIJO)
            _check("emitir dos veces devuelve la misma factura", inv2.id, inv1.id)
            db.rollback()

        # --- 5. sin prefijo configurado no rompe -----------------------------
        with with_db(schema) as db:
            s = build_sale(
                db, lines=[_line(variant_id)], shift=db.get(CashShift, shift_id),
                cashier=db.get(User, user_id), payments=[_Pago(method_id, "10.00")],
            )
            _check("tenant sin prefijo → cadena vacía", _factura(db, s.id).prefix, "")
            db.rollback()

        # --- 6. una factura por VENTA (lo que hace posible el split) ---------
        with with_db(schema) as db:
            ventas = [
                build_sale(
                    db, lines=[_line(variant_id)], shift=db.get(CashShift, shift_id),
                    cashier=db.get(User, user_id), payments=[_Pago(method_id, "10.00")],
                    invoice_prefix=PREFIJO,
                )
                for _ in range(2)
            ]
            facturas = {_factura(db, v.id).id for v in ventas}
            _check("dos ventas (split) → dos facturas distintas", len(facturas), 2)
            db.rollback()

        print("\nTODO OK ✔")
    finally:
        _cleanup(schema, shift_id)


if __name__ == "__main__":
    main()
