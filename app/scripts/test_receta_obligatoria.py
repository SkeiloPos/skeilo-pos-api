"""Test de la guarda que impide vender sin descontar inventario.

No hay pytest en el proyecto, así que es un script autoejecutable:

    python -m app.scripts.test_receta_obligatoria

Cubre el defecto que lo motivó: una variante **sin receta** se vendía sin mover el
stock. No fallaba, no avisaba, no dejaba rastro en el kardex — el inventario
quedaba sobrestimado hasta el conteo físico. Con 7 de 13 variantes activas sin
receta en un tenant real, eso ocurría a diario.

La guarda vive en `deduct_order_items`, que es el paso común de los tres caminos
que descuentan. Este test comprueba justamente eso: que **ninguno** de los tres
puede colarse, porque validarlo solo en la confirmación fue como se coló antes.

Trabaja sobre un tenant real con datos desechables y los borra al terminar.
"""
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
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

STOCK_INICIAL = Decimal("100.000")
POR_UNIDAD = Decimal("2.000")


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


def _expect_409(fn, label):
    try:
        fn()
    except HTTPException as e:
        if e.status_code != 409:
            raise AssertionError(f"{label}: esperado 409, vino {e.status_code}")
        print(f"  ok  · {label}")
        return e
    raise AssertionError(f"{label}: no lanzó 409 (¡se vendió sin descontar!)")


def _fixture(db):
    """Dos variantes del mismo producto: una CON receta y otra SIN."""
    unit = db.execute(select(UnitMeasure).limit(1)).scalar_one_or_none()
    if unit is None:
        unit = UnitMeasure(name=f"ud-{uuid4().hex[:6]}", abbreviation=uuid4().hex[:4])
        db.add(unit); db.flush()

    item = InventoryItem(
        code=f"test-{uuid4().hex[:8]}", name=f"insumo-rec-{uuid4().hex[:8]}", type="raw_material",
        unit_measure_id=unit.id, current_stock=STOCK_INICIAL, unit_cost=Decimal("1.00"),
    )
    db.add(item)

    cat = db.execute(select(Category).limit(1)).scalar_one_or_none()
    if cat is None:
        cat = Category(name=f"cat-{uuid4().hex[:6]}"); db.add(cat); db.flush()

    product = Product(name=f"prod-rec-{uuid4().hex[:8]}", category_id=cat.id,
                      preparation_type="prepared")
    db.add(product); db.flush()

    con = ProductVariant(product_id=product.id, name="con receta",
                         price=Decimal("10.00"), active=True, display_order=1)
    sin = ProductVariant(product_id=product.id, name="sin receta",
                         price=Decimal("10.00"), active=True, display_order=2)
    db.add_all([con, sin]); db.flush()

    db.add(RecipeItem(product_variant_id=con.id, inventory_item_id=item.id,
                      quantity=POR_UNIDAD))
    db.flush()
    return item, con, sin, product.id


def _staff(db) -> User:
    user = db.execute(select(User).limit(1)).scalar_one_or_none()
    if user is None:
        raise SystemExit("No hay usuarios en shared.users.")
    return user


def _order(db, variants, *, status="recibida"):
    """Pedido con una línea por variante dada."""
    order = CustomerOrder(channel="QR_MENU", order_type="DINE_IN", status=status)
    db.add(order); db.flush()
    for v in variants:
        db.add(OrderItem(order_id=order.id, product_variant_id=v.id, quantity=1,
                         unit_price=Decimal("10.00"), estado_cocina="pendiente"))
    db.flush(); db.refresh(order)
    return order


def _movs(db, order_id) -> int:
    db.flush()
    return len(db.execute(
        select(InventoryMovement).where(InventoryMovement.reference_id == order_id)
    ).scalars().all())


def _stock(db, item_id) -> Decimal:
    db.flush()
    return Decimal(db.get(InventoryItem, item_id).current_stock)


def _cleanup(schema, product_id, item_id):
    with with_db(schema) as db:
        db.execute(text(f'''
            DELETE FROM "{schema}".inventory_movements WHERE inventory_item_id = :i
        '''), {"i": str(item_id)})
        db.execute(text(f'''
            DELETE FROM "{schema}".order_items WHERE product_variant_id IN (
                SELECT id FROM "{schema}".product_variants WHERE product_id = :p)
        '''), {"p": str(product_id)})
        db.execute(text(f'''
            DELETE FROM "{schema}".customer_orders WHERE id NOT IN (
                SELECT order_id FROM "{schema}".order_items) AND channel = 'QR_MENU'
                AND status = 'recibida'
        '''))
        db.execute(text(f'''DELETE FROM "{schema}".recipe_items WHERE product_variant_id IN (
                SELECT id FROM "{schema}".product_variants WHERE product_id = :p)'''),
                   {"p": str(product_id)})
        db.execute(text(f'DELETE FROM "{schema}".product_variants WHERE product_id = :p'),
                   {"p": str(product_id)})
        db.execute(text(f'DELETE FROM "{schema}".products WHERE id = :p'), {"p": str(product_id)})
        db.execute(text(f'DELETE FROM "{schema}".inventory_items WHERE id = :i'), {"i": str(item_id)})
        db.commit()


def main():
    schema = _schema()
    print(f"Receta obligatoria para descontar (tenant: {schema})")

    with with_db(schema) as db:
        item, con, sin, product_id = _fixture(db)
        db.commit()
        item_id, con_id, sin_id = item.id, con.id, sin.id

    try:
        # --- 1. confirmar un pedido cuya variante NO tiene receta -------------
        with with_db(schema) as db:
            user = _staff(db)
            sin_v = db.get(ProductVariant, sin_id)
            order = _order(db, [sin_v])
            db.commit()
            oid = order.id

        with with_db(schema) as db:
            e = _expect_409(
                lambda: checkout.confirm_order(db, oid, _staff(db)),
                "confirmar sin receta → 409",
            )
            detalle = e.detail if isinstance(e.detail, dict) else {}
            if "variantes_sin_receta" not in detalle:
                raise AssertionError("el 409 no dice qué variante falta configurar")
            print("  ok  · el error nombra la variante a configurar")

        with with_db(schema) as db:
            _check("el pedido sigue 'recibida'", db.get(CustomerOrder, oid).status, "recibida")
            _check("y no escribió ningún movimiento", _movs(db, oid), 0)
            _check("el stock no se movió", _stock(db, item_id), STOCK_INICIAL)

        # --- 2. con receta sí descuenta --------------------------------------
        with with_db(schema) as db:
            order2 = _order(db, [db.get(ProductVariant, con_id)])
            db.commit()
            oid2 = order2.id

        with with_db(schema) as db:
            checkout.confirm_order(db, oid2, _staff(db))
        with with_db(schema) as db:
            _check("con receta el pedido pasa a 'abierta'",
                   db.get(CustomerOrder, oid2).status, "abierta")
            _check("y escribe un movimiento por insumo", _movs(db, oid2), 1)
            _check("descontando la cantidad de la receta",
                   _stock(db, item_id), STOCK_INICIAL - POR_UNIDAD)

        # --- 3. lote mixto: rechaza TODO, sin descuentos a medias -------------
        stock_antes = None
        with with_db(schema) as db:
            stock_antes = _stock(db, item_id)
            order3 = _order(db, [db.get(ProductVariant, con_id), db.get(ProductVariant, sin_id)])
            db.commit()
            oid3 = order3.id

        with with_db(schema) as db:
            _expect_409(
                lambda: checkout.confirm_order(db, oid3, _staff(db)),
                "una línea sin receta invalida el pedido entero → 409",
            )
        with with_db(schema) as db:
            _check("no queda ningún movimiento a medias", _movs(db, oid3), 0)
            _check("y el stock quedó intacto", _stock(db, item_id), stock_antes)

        # --- 4. la guarda vive en deduct_order_items, no en confirm ----------
        # Es lo que garantiza que consolidación y adición de ítem tampoco puedan
        # colarse: comparten esta función.
        with with_db(schema) as db:
            order4 = _order(db, [db.get(ProductVariant, sin_id)], status="abierta")
            db.commit()
            entries = [(it, []) for it in order4.items]
            _expect_409(
                lambda: deduct_order_items(db, entries, _staff(db).id, reference_id=order4.id),
                "deduct_order_items rechaza directamente (cubre los 3 caminos)",
            )

        print("\nTODO OK ✔")
    finally:
        _cleanup(schema, product_id, item_id)


if __name__ == "__main__":
    main()
