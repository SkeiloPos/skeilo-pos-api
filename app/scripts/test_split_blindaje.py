"""Test de las defensas del cierre de sesión con cuenta dividida.

    python -m app.scripts.test_split_blindaje

Estos agujeros existían desde que se implementó el split, pero eran difíciles de
alcanzar: nadie podía construir un bloque de pago a mano. Al abrir el reparto de
cuenta desde el POS, el cajero pasa a ser quien arma esos bloques, así que se cierran
**antes** de dar esa capacidad.

Lo que se comprueba:

  1. **Comensales repetidos** en `splits` → 422 y **cero ventas**. Antes los dos
     chequeos de cobertura trabajaban con conjuntos, así que dos bloques del mismo
     comensal pasaban y el bucle cobraba sus mismas líneas dos veces: doble venta,
     doble factura, doble consecutivo fiscal.
  2. **Importes en la raíz** con `billing_mode='split'` → 422. Antes se ignoraban en
     silencio y el cajero perdía la propina sin enterarse.
  3. **Importes negativos** → 422 de validación (faltaba `ge=0`).
  4. El bloque **sin comensal** (lo que teclea el mesero) produce una factura **con
     nombre**; antes salía literalmente sin nombre.

Trabaja sobre un tenant real con datos desechables y los borra al terminar.
"""
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, text

from app.core.db import with_db
from app.core.models import User
from app.models.cash_register import CashRegister
from app.models.cash_shift import CashShift
from app.models.category import Category
from app.models.customer_order import CustomerOrder
from app.models.dining_table import DiningTable
from app.models.inventory_item import InventoryItem
from app.models.order_item import OrderItem
from app.models.payment import PaymentMethod
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.recipe_item import RecipeItem
from app.models.sale import Sale
from app.models.session_participant import SessionParticipant
from app.models.table_session import TableSession
from app.models.unit_measure import UnitMeasure
from app.api.v1.table_sessions import service as sessions
from app.api.v1.table_sessions.schemas import CloseSessionIn

PRECIO = Decimal("10.00")


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


def _expect_422(fn, label):
    try:
        fn()
    except HTTPException as e:
        if e.status_code != 422:
            raise AssertionError(f"{label}: esperado 422, vino {e.status_code}")
        print(f"  ok  · {label}")
        return e
    raise AssertionError(f"{label}: no lanzó 422")


def _fixture(db, suf: str):
    """Mesa con sesión activa, dos comensales y un pedido listo de 3 líneas:
    una de cada comensal y una sin asignar (la que teclearía el mesero)."""
    user = db.execute(select(User).limit(1)).scalar_one_or_none()
    if user is None:
        raise SystemExit("No hay usuarios en shared.users.")
    method = db.execute(select(PaymentMethod).where(PaymentMethod.is_cash)).scalars().first()
    if method is None:
        raise SystemExit("El tenant no tiene método de pago en efectivo.")

    reg = db.execute(select(CashRegister).limit(1)).scalar_one_or_none()
    if reg is None:
        reg = CashRegister(name=f"caja-split-{suf}"); db.add(reg); db.flush()
    shift = CashShift(
        cash_register_id=reg.id, user_id=user.id, user_name=user.name,
        opening_amount=Decimal("100000"), status="open",
    )
    db.add(shift); db.flush()

    # Producto con receta: el cierre no descuenta stock, pero la variante debe existir.
    unit = db.execute(select(UnitMeasure).limit(1)).scalar_one()
    cat = db.execute(select(Category).limit(1)).scalar_one()
    item = InventoryItem(
        code=f"test-{suf}", name=f"insumo-split-{suf}", type="raw_material", unit_measure_id=unit.id,
        current_stock=Decimal("500"), unit_cost=Decimal("1"),
    )
    db.add(item); db.flush()
    product = Product(name=f"prod-split-{suf}", code=f"cod-split-{suf}", category_id=cat.id, preparation_type="prepared")
    db.add(product); db.flush()
    variant = ProductVariant(product_id=product.id, name="Única", price=PRECIO, active=True,
                             display_order=1)
    db.add(variant); db.flush()
    db.add(RecipeItem(product_variant_id=variant.id, inventory_item_id=item.id,
                      quantity=Decimal("1")))

    table = DiningTable(number=9000 + int(suf[:3], 16) % 900, name=f"mesa-split-{suf}", active=True)
    db.add(table); db.flush()
    ts = TableSession(dining_table_id=table.id, status="active")
    db.add(ts); db.flush()

    ana = SessionParticipant(table_session_id=ts.id, dining_table_id=table.id,
                             display_name="Ana", display_label="Ana", status="open")
    beto = SessionParticipant(table_session_id=ts.id, dining_table_id=table.id,
                              display_name="Beto", display_label="Beto", status="open")
    db.add_all([ana, beto]); db.flush()

    order = CustomerOrder(channel="POS", order_type="DINE_IN", status="abierta",
                          dining_table_id=table.id, table_session_id=ts.id)
    db.add(order); db.flush()
    for participant_id in (ana.id, beto.id, None):
        db.add(OrderItem(
            order_id=order.id, product_variant_id=variant.id, participant_id=participant_id,
            quantity=1, unit_price=PRECIO, estado_cocina="listo",
        ))
    db.flush()
    return dict(shift=shift.id, user=user.id, method=method.id, table=table.id,
                ts=ts.id, ana=ana.id, beto=beto.id, product=product.id,
                variant=variant.id, item=item.id, order=order.id)


def _pago(method_id, amount=PRECIO) -> dict:
    return {"payment_method_id": str(method_id), "amount": str(amount)}


def _ventas(db, ts_id) -> list[Sale]:
    db.flush()
    return list(db.execute(select(Sale).where(Sale.table_session_id == ts_id)).scalars())


def _cleanup(schema, fx):
    with with_db(schema) as db:
        ts, table, order = str(fx["ts"]), str(fx["table"]), str(fx["order"])
        db.execute(text(f'''DELETE FROM "{schema}".invoices WHERE sale_id IN (
            SELECT id FROM "{schema}".sales WHERE table_session_id = :t)'''), {"t": ts})
        for tabla in ("payments", "sale_items"):
            db.execute(text(f'''DELETE FROM "{schema}".{tabla} WHERE sale_id IN (
                SELECT id FROM "{schema}".sales WHERE table_session_id = :t)'''), {"t": ts})
        db.execute(text(f'DELETE FROM "{schema}".sales WHERE table_session_id = :t'), {"t": ts})
        db.execute(text(f'DELETE FROM "{schema}".order_items WHERE order_id = :o'), {"o": order})
        db.execute(text(f'DELETE FROM "{schema}".customer_orders WHERE id = :o'), {"o": order})
        db.execute(text(f'DELETE FROM "{schema}".carts WHERE participant_id IN ('
                        f'SELECT id FROM "{schema}".session_participants WHERE table_session_id = :t)'),
                   {"t": ts})
        db.execute(text(f'DELETE FROM "{schema}".session_participants WHERE table_session_id = :t'),
                   {"t": ts})
        db.execute(text(f'DELETE FROM "{schema}".table_sessions WHERE id = :t'), {"t": ts})
        db.execute(text(f'DELETE FROM "{schema}".dining_tables WHERE id = :d'), {"d": table})
        db.execute(text(f'DELETE FROM "{schema}".recipe_items WHERE product_variant_id = :v'),
                   {"v": str(fx["variant"])})
        db.execute(text(f'DELETE FROM "{schema}".product_variants WHERE product_id = :p'),
                   {"p": str(fx["product"])})
        db.execute(text(f'DELETE FROM "{schema}".products WHERE id = :p'), {"p": str(fx["product"])})
        db.execute(text(f'DELETE FROM "{schema}".inventory_items WHERE id = :i'), {"i": str(fx["item"])})
        db.execute(text(f'DELETE FROM "{schema}".cash_shifts WHERE id = :s'), {"s": str(fx["shift"])})
        db.commit()


def main():
    schema = _schema()
    suf = uuid4().hex[:8]
    print(f"Blindaje del cierre con cuenta dividida (tenant: {schema})")

    with with_db(schema) as db:
        fx = _fixture(db, suf)
        db.commit()

    try:
        # --- 1. comensal repetido → 422 y ninguna venta ----------------------
        with with_db(schema) as db:
            data = CloseSessionIn.model_validate({
                "cash_shift_id": str(fx["shift"]),
                "billing_mode": "split",
                "splits": [
                    {"participant_id": str(fx["ana"]), "payments": [_pago(fx["method"])]},
                    {"participant_id": str(fx["ana"]), "payments": [_pago(fx["method"])]},
                    {"participant_id": str(fx["beto"]), "payments": [_pago(fx["method"])]},
                    {"participant_id": None, "payments": [_pago(fx["method"])]},
                ],
            })
            e = _expect_422(
                lambda: sessions.close_session(db, fx["ts"], data, db.get(User, fx["user"])),
                "comensal repetido en el split → 422",
            )
            detalle = e.detail if isinstance(e.detail, dict) else {}
            if "repetidos" not in str(detalle).lower() and "una sola vez" not in str(detalle):
                raise AssertionError(f"el 422 no explica el duplicado: {detalle}")
            print("  ok  · el error dice que cada comensal paga una sola vez")
            db.rollback()

        with with_db(schema) as db:
            _check("no se creó ninguna venta", len(_ventas(db, fx["ts"])), 0)
            _check("la sesión sigue activa",
                   db.get(TableSession, fx["ts"]).status, "active")

        # --- 2. importes en la raíz con split → 422 --------------------------
        with with_db(schema) as db:
            data = CloseSessionIn.model_validate({
                "cash_shift_id": str(fx["shift"]),
                "billing_mode": "split",
                "tip": "5.00",
                "splits": [
                    {"participant_id": str(fx["ana"]), "payments": [_pago(fx["method"])]},
                    {"participant_id": str(fx["beto"]), "payments": [_pago(fx["method"])]},
                    {"participant_id": None, "payments": [_pago(fx["method"])]},
                ],
            })
            _expect_422(
                lambda: sessions.close_session(db, fx["ts"], data, db.get(User, fx["user"])),
                "propina en la raíz con split → 422 (antes se perdía en silencio)",
            )
            db.rollback()

        # --- 3. importes negativos → los rechaza el esquema ------------------
        try:
            CloseSessionIn.model_validate({
                "cash_shift_id": str(fx["shift"]),
                "billing_mode": "unified",
                "payments": [_pago(fx["method"])],
                "discount": "-5.00",
            })
            raise AssertionError("un descuento negativo pasó la validación")
        except ValidationError:
            print("  ok  · descuento negativo rechazado por el esquema")

        # --- 4. el bloque sin comensal factura CON nombre --------------------
        with with_db(schema) as db:
            data = CloseSessionIn.model_validate({
                "cash_shift_id": str(fx["shift"]),
                "billing_mode": "split",
                "splits": [
                    {"participant_id": str(fx["ana"]), "payments": [_pago(fx["method"])]},
                    {"participant_id": str(fx["beto"]), "payments": [_pago(fx["method"])]},
                    {"participant_id": None, "payments": [_pago(fx["method"])]},
                ],
            })
            res = sessions.close_session(db, fx["ts"], data, db.get(User, fx["user"]))
            _check("el cierre válido emite 3 ventas", len(res.sale_ids), 3)

        with with_db(schema) as db:
            ventas = _ventas(db, fx["ts"])
            nombres = sorted((v.customer_name or "") for v in ventas)
            _check("ninguna venta sale sin nombre", all(nombres), True)
            _check("y las de los comensales llevan su etiqueta",
                   {"Ana", "Beto"} <= set(nombres), True)
            _check("cada venta cobró una sola línea",
                   sorted(v.subtotal for v in ventas), [PRECIO] * 3)

        print("\n✔ El split rechaza duplicados e importes mal puestos, y no factura sin nombre.")
        return 0
    finally:
        _cleanup(schema, fx)
        print("  (datos de prueba eliminados)")


def _expect_409(fn, label):
    try:
        fn()
    except HTTPException as e:
        if e.status_code != 409:
            raise AssertionError(f"{label}: esperado 409, vino {e.status_code}")
        print(f"  ok  · {label}")
        return e
    raise AssertionError(f"{label}: no lanzó 409")


def reparto():
    """El caso que motivó todo: una sola persona pidió, y ahora se reparte la cuenta."""
    schema = _schema()
    suf = uuid4().hex[:8]
    print(f"\nReparto de cuenta sin QR (tenant: {schema})")

    with with_db(schema) as db:
        fx = _fixture(db, suf)
        # Punto de partida real: TODO sin asignar, como cuando pide una sola persona.
        db.execute(text(f'''UPDATE "{schema}".order_items SET participant_id = NULL
                            WHERE order_id = :o'''), {"o": str(fx["order"])})
        db.execute(text(f'''DELETE FROM "{schema}".session_participants
                            WHERE table_session_id = :t'''), {"t": str(fx["ts"])})
        db.commit()

    try:
        with with_db(schema) as db:
            bill = sessions.compute_bill(db, fx["ts"])
            _check("de entrada la cuenta tiene una sola línea", len(bill.split), 1)
            _check("y es la de 'sin asignar'", bill.split[0].participant_id, None)

        # --- comensales creados por el staff ---------------------------------
        with with_db(schema) as db:
            ana = sessions.add_participant(db, fx["ts"], "Ana")
            beto = sessions.add_participant(db, fx["ts"], "Ana")  # mismo nombre
            _check("el segundo 'Ana' se desambigua", beto.display_label, "Ana (2)")
            _check("y no se le crea token de QR", ana.expires_at, None)
            ana_id, beto_id = ana.id, beto.id

        # --- reparto ----------------------------------------------------------
        with with_db(schema) as db:
            items = list(db.execute(
                select(OrderItem).where(OrderItem.order_id == fx["order"])
                .order_by(OrderItem.id)
            ).scalars())
            asignaciones = [
                _asig(items[0].id, ana_id),
                _asig(items[1].id, beto_id),
            ]
            bill = sessions.set_assignments(db, fx["ts"], asignaciones)
            porcomensal = {l.participant_id: l.subtotal for l in bill.split}
            _check("tras repartir hay tres líneas", len(bill.split), 3)
            _check("Ana debe su producto", porcomensal[ana_id], PRECIO)
            _check("Ana (2) el suyo", porcomensal[beto_id], PRECIO)
            _check("y queda uno sin asignar", porcomensal[None], PRECIO)

        # --- defensas del reparto ---------------------------------------------
        with with_db(schema) as db:
            _expect_409(
                lambda: sessions.remove_participant(db, fx["ts"], ana_id),
                "quitar un comensal con productos → 409",
            )
        with with_db(schema) as db:
            ajeno = _asig(uuid4(), None)
            _expect_422(
                lambda: sessions.set_assignments(db, fx["ts"], [ajeno]),
                "asignar un producto de otra mesa → 422",
            )

        # --- y ahora el cobro dividido funciona SIN tocarlo --------------------
        with with_db(schema) as db:
            data = CloseSessionIn.model_validate({
                "cash_shift_id": str(fx["shift"]),
                "billing_mode": "split",
                "splits": [
                    {"participant_id": str(ana_id), "payments": [_pago(fx["method"])]},
                    {"participant_id": str(beto_id), "payments": [_pago(fx["method"])]},
                    {"participant_id": None, "payments": [_pago(fx["method"])]},
                ],
            })
            res = sessions.close_session(db, fx["ts"], data, db.get(User, fx["user"]))
            _check("el cobro dividido emite una venta por persona", len(res.sale_ids), 3)

        with with_db(schema) as db:
            ventas = _ventas(db, fx["ts"])
            _check("cada una con su propio consumo",
                   sorted(v.subtotal for v in ventas), [PRECIO] * 3)
            _check("y todas a nombre de alguien",
                   all(v.customer_name for v in ventas), True)

        print("\n✔ Una sola persona pidió, el staff repartió sin QR y cada quien pagó lo suyo.")
        return 0
    finally:
        _cleanup(schema, fx)
        print("  (datos de prueba eliminados)")


def _asig(item_id, participant_id, quantity=None):
    """Imita `ItemAssignmentIn` sin pasar por el router."""
    return type("A", (), {
        "order_item_id": item_id, "participant_id": participant_id, "quantity": quantity,
    })()


def unidades():
    """Repartir las unidades de una MISMA línea entre dos personas.

    Lo que hay que proteger aquí es que la cantidad total no cambie: el inventario se
    descontó al confirmar el pedido según esa cantidad, así que partir 2 en 1+1 es
    seguro, pero un reparto que sume de más o de menos descuadraría el stock."""
    schema = _schema()
    suf = uuid4().hex[:8]
    print(f"\nReparto por unidades de una misma línea (tenant: {schema})")

    with with_db(schema) as db:
        fx = _fixture(db, suf)
        # Una sola línea de 2 unidades, sin asignar y ya entregada.
        db.execute(text(f'DELETE FROM "{schema}".order_items WHERE order_id = :o'),
                   {"o": str(fx["order"])})
        db.add(OrderItem(
            order_id=fx["order"], product_variant_id=fx["variant"], participant_id=None,
            quantity=2, unit_price=PRECIO, estado_cocina="listo",
        ))
        db.commit()

    try:
        with with_db(schema) as db:
            item = db.execute(
                select(OrderItem).where(OrderItem.order_id == fx["order"])
            ).scalar_one()
            item_id = item.id
            _check("la mesa arranca con una sola línea de 2", item.quantity, 2)

        # --- la suma tiene que cuadrar ----------------------------------------
        with with_db(schema) as db:
            _expect_422(
                lambda: sessions.set_assignments(db, fx["ts"], [
                    _asig(item_id, fx["ana"], 1),
                    _asig(item_id, fx["beto"], 2),   # 1 + 2 sobre una línea de 2
                ]),
                "un reparto que no suma la cantidad de la línea → 422",
            )
            db.rollback()
        with with_db(schema) as db:
            total = db.execute(
                select(func.sum(OrderItem.quantity)).where(OrderItem.order_id == fx["order"])
            ).scalar()
            _check("y la cantidad no se tocó", total, 2)

        # --- reparto válido: 1 para cada uno ----------------------------------
        with with_db(schema) as db:
            bill = sessions.set_assignments(db, fx["ts"], [
                _asig(item_id, fx["ana"], 1),
                _asig(item_id, fx["beto"], 1),
            ])
            porcomensal = {l.participant_id: l.subtotal for l in bill.split}
            _check("cada uno paga su unidad", porcomensal[fx["ana"]], PRECIO)
            _check("y el otro la suya", porcomensal[fx["beto"]], PRECIO)
            _check("el total de la mesa no cambia", bill.total, PRECIO * 2)

        with with_db(schema) as db:
            filas = list(db.execute(
                select(OrderItem).where(OrderItem.order_id == fx["order"])
            ).scalars())
            _check("la línea quedó partida en dos", len(filas), 2)
            _check("con una unidad cada una", sorted(f.quantity for f in filas), [1, 1])
            _check("la cantidad total sigue siendo la misma (el stock no se toca)",
                   sum(f.quantity for f in filas), 2)
            _check("y ambas conservan el precio",
                   {f.unit_price for f in filas}, {PRECIO})

        # --- no se parte lo que cocina aún no terminó -------------------------
        with with_db(schema) as db:
            otra = OrderItem(
                order_id=fx["order"], product_variant_id=fx["variant"], participant_id=None,
                quantity=2, unit_price=PRECIO, estado_cocina="en_preparacion",
            )
            db.add(otra); db.commit()
            otra_id = otra.id
        with with_db(schema) as db:
            _expect_422(
                lambda: sessions.set_assignments(db, fx["ts"], [
                    _asig(otra_id, fx["ana"], 1),
                    _asig(otra_id, fx["beto"], 1),
                ]),
                "partir por unidades algo en cocina → 422",
            )
            db.rollback()
        with with_db(schema) as db:
            # Pero asignarlo ENTERO sí se puede: no parte nada.
            sessions.set_assignments(db, fx["ts"], [_asig(otra_id, fx["ana"])])
            _check("asignar la línea entera sigue funcionando",
                   db.get(OrderItem, otra_id).participant_id, fx["ana"])

        print("\n✔ Las unidades de una línea se reparten sin alterar la cantidad total.")
        return 0
    finally:
        _cleanup(schema, fx)
        print("  (datos de prueba eliminados)")


if __name__ == "__main__":
    raise SystemExit(main() or reparto() or unidades())
