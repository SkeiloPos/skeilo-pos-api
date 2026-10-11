"""Test de la política de inventario al cancelar un pedido.

No hay pytest en el proyecto, así que es un script autoejecutable:

    python -m app.scripts.test_cancel_inventory

Cubre el defecto que motivó este trabajo: `cancel_order` devolvía al stock **todos**
los ítems sin mirar el estado de cocina, así que cancelar un pedido ya en
preparación reingresaba insumos que físicamente ya se habían usado y sobrestimaba
el inventario en silencio hasta el conteo físico.

Reglas verificadas:
  - pedido `recibida`              → cero movimientos (nunca se descontó);
  - ítem `pendiente` en `abierta`  → entrada real, el stock vuelve;
  - ítem `en_preparacion`+         → sin movimiento, el stock NO vuelve (pérdida);
  - mixto                          → solo se revierte la parte pendiente.

Trabaja sobre un tenant real con datos desechables: crea su propio insumo,
producto, variante y receta, y los borra al terminar. No basta con un rollback
final porque `cancel_order` hace commit por dentro.
"""
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, text

from app.core.db import with_db
from app.core.models import User
from app.models.category import Category
from app.models.customer_order import CustomerOrder
from app.models.dining_table import DiningTable
from app.models.inventory_item import InventoryItem
from app.models.inventory_movement import InventoryMovement
from app.models.order_item import OrderItem
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.recipe_item import RecipeItem
from app.models.unit_measure import UnitMeasure
from app.api.v1.orders import checkout
from app.api.v1.orders.consumption import deduct_order_items
from app.api.v1.orders.schemas import CancelIn

STOCK_INICIAL = Decimal("100.000")
POR_UNIDAD = Decimal("2.000")


def _schema() -> str:
    with with_db(None) as db:
        row = db.execute(
            text("SELECT schema FROM shared.tenants ORDER BY id LIMIT 1")
        ).scalar()
    if row is None:
        raise SystemExit("No hay tenants en shared.tenants; crea uno antes de correr esto.")
    return row


def _fixture(db):
    """Insumo + variante con receta (2 unidades de insumo por unidad vendida)."""
    unit = db.execute(select(UnitMeasure).limit(1)).scalar_one_or_none()
    if unit is None:
        unit = UnitMeasure(name=f"ud-{uuid4().hex[:6]}", abbreviation=uuid4().hex[:4])
        db.add(unit)
        db.flush()

    item = InventoryItem(
        code=f"test-{uuid4().hex[:8]}", name=f"insumo-test-{uuid4().hex[:8]}", type="raw_material",
        unit_measure_id=unit.id, current_stock=STOCK_INICIAL, unit_cost=Decimal("1.00"),
    )
    db.add(item)

    cat = db.execute(select(Category).limit(1)).scalar_one_or_none()
    if cat is None:
        cat = Category(name=f"cat-{uuid4().hex[:6]}")
        db.add(cat)
        db.flush()

    product = Product(name=f"prod-test-{uuid4().hex[:8]}", category_id=cat.id,
                      preparation_type="prepared")
    db.add(product)
    db.flush()

    variant = ProductVariant(product_id=product.id, name="única",
                             price=Decimal("10.00"), active=True, display_order=1)
    db.add(variant)
    db.flush()

    db.add(RecipeItem(product_variant_id=variant.id, inventory_item_id=item.id,
                      quantity=POR_UNIDAD))

    table = db.execute(select(DiningTable).limit(1)).scalar_one_or_none()
    if table is None:
        table = DiningTable(number=9999, name="mesa-test")
        db.add(table)
    db.flush()
    return item, variant, table


def _staff(db) -> User:
    user = db.execute(select(User).limit(1)).scalar_one_or_none()
    if user is None:
        raise SystemExit("No hay usuarios en shared.users.")
    return user


def _make_order(db, variant, table, *, status: str, estados: list[str]):
    """Pedido con una línea por cada estado de cocina pedido."""
    order = CustomerOrder(dining_table_id=table.id, channel="QR_MENU", order_type="DINE_IN", status=status)
    db.add(order)
    db.flush()
    for est in estados:
        db.add(OrderItem(
            order_id=order.id, product_variant_id=variant.id, quantity=1,
            unit_price=Decimal("10.00"), estado_cocina=est,
        ))
    db.flush()
    db.refresh(order)  # carga order.items recién insertados
    return order


def _stock(db, item_id) -> Decimal:
    # La sesión tiene autoflush=False: hay que forzar el flush para que el
    # decremento pendiente de `record_movement` se vea en la lectura.
    db.flush()
    return Decimal(db.get(InventoryItem, item_id).current_stock)


def _movimientos(db, order_id) -> list[tuple[str, Decimal]]:
    db.flush()
    return [
        (m.type, Decimal(m.quantity))
        for m in db.execute(
            select(InventoryMovement).where(InventoryMovement.reference_id == order_id)
        ).scalars()
    ]


def _check(label, actual, esperado):
    if actual != esperado:
        raise AssertionError(f"{label}: esperado {esperado}, obtenido {actual}")
    print(f"  ok  · {label}")


def _cleanup(db, schema: str, order_ids: list, item_id, variant_id, product_id) -> None:
    """Borra lo que creó el test. En orden de dependencia (los movimientos y las
    líneas primero, el catálogo al final)."""
    ids = tuple(str(o) for o in order_ids)
    if ids:
        db.execute(text(f'DELETE FROM "{schema}".inventory_movements '
                        f'WHERE reference_id = ANY(:ids)'), {"ids": list(ids)})
        db.execute(text(f'DELETE FROM "{schema}".audit_logs '
                        f'WHERE entity_id = ANY(:ids)'), {"ids": list(ids)})
        db.execute(text(f'DELETE FROM "{schema}".order_cancel_logs '
                        f'WHERE order_id = ANY(:ids)'), {"ids": list(ids)})
        db.execute(text(f'DELETE FROM "{schema}".order_items '
                        f'WHERE order_id = ANY(:ids)'), {"ids": list(ids)})
        db.execute(text(f'DELETE FROM "{schema}".customer_orders '
                        f'WHERE id = ANY(:ids)'), {"ids": list(ids)})
    db.execute(text(f'DELETE FROM "{schema}".recipe_items '
                    f'WHERE product_variant_id = :v'), {"v": str(variant_id)})
    db.execute(text(f'DELETE FROM "{schema}".product_variants WHERE id = :v'),
               {"v": str(variant_id)})
    db.execute(text(f'DELETE FROM "{schema}".products WHERE id = :p'),
               {"p": str(product_id)})
    db.execute(text(f'DELETE FROM "{schema}".inventory_items WHERE id = :i'),
               {"i": str(item_id)})
    db.commit()


def main():
    schema = _schema()
    print(f"Política de inventario al cancelar (tenant: {schema})")

    with with_db(schema) as db:
        item, variant, table = _fixture(db)
        user = _staff(db)
        item_id, variant_id, product_id = item.id, variant.id, variant.product_id
        creados: list = []

        try:
            # --- 1. pedido 'recibida': nunca descontó, cancelar no mueve nada -----
            order = _make_order(db, variant, table, status="recibida",
                                estados=["pendiente", "pendiente"])
            creados.append(order.id)
            antes = _stock(db, item_id)
            checkout.cancel_order(db, order.id, CancelIn(motivo="test"), user)
            _check("cancelar 'recibida' no genera movimientos", _movimientos(db, order.id), [])
            _check("cancelar 'recibida' no cambia el stock", _stock(db, item_id), antes)

            # --- 2. 'abierta' con todo pendiente: reversa completa ----------------
            order = _make_order(db, variant, table, status="abierta",
                                estados=["pendiente", "pendiente"])
            creados.append(order.id)
            entries = [(it, []) for it in order.items]
            deduct_order_items(db, entries, user.id, reference_id=order.id)
            tras_descuento = _stock(db, item_id)
            _check("confirmar descuenta 2 líneas × 2 ud",
                   antes - tras_descuento, POR_UNIDAD * 2)

            checkout.cancel_order(db, order.id, CancelIn(motivo="test"), user)
            _check("cancelar con todo 'pendiente' devuelve el stock",
                   _stock(db, item_id), antes)
            _check("y escribe 2 entradas",
                   sorted(_movimientos(db, order.id)),
                   sorted([("out", POR_UNIDAD)] * 2 + [("in", POR_UNIDAD)] * 2))

            # --- 3. 'abierta' ya en cocina: NO vuelve al stock --------------------
            order = _make_order(db, variant, table, status="abierta",
                                estados=["en_preparacion", "listo"])
            creados.append(order.id)
            entries = [(it, []) for it in order.items]
            deduct_order_items(db, entries, user.id, reference_id=order.id)
            tras_descuento = _stock(db, item_id)

            checkout.cancel_order(db, order.id, CancelIn(motivo="test"), user)
            _check("cancelar en cocina NO devuelve el stock (es pérdida)",
                   _stock(db, item_id), tras_descuento)
            _check("y no escribe ninguna entrada 'in'",
                   [m for m in _movimientos(db, order.id) if m[0] == "in"], [])

            # --- 4. mixto: solo se revierte lo pendiente --------------------------
            order = _make_order(db, variant, table, status="abierta",
                                estados=["pendiente", "en_preparacion"])
            creados.append(order.id)
            entries = [(it, []) for it in order.items]
            deduct_order_items(db, entries, user.id, reference_id=order.id)
            tras_descuento = _stock(db, item_id)

            checkout.cancel_order(db, order.id, CancelIn(motivo="test"), user)
            _check("mixto: vuelve solo la línea pendiente",
                   _stock(db, item_id) - tras_descuento, POR_UNIDAD)

            print("TODO OK ✔")
        finally:
            _cleanup(db, schema, creados, item_id, variant_id, product_id)


if __name__ == "__main__":
    main()
