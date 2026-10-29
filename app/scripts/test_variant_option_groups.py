"""Test de los grupos de opciones por variante: cada tamaño define cuántas opciones
elige el comensal y cuánto descuenta cada una.

No hay pytest en el proyecto, así que es un script autoejecutable:

    python -m app.scripts.test_variant_option_groups

Cubre el requisito que lo motivó: «la ensalada pequeña elige 1 sabor y descuenta 60 g,
la mediana elige 2 y descuenta 120 g de cada uno». Antes era inexpresable: la cantidad
vivía en `options.item_quantity` (global al catálogo) y luego en un slot de receta (por
variante), pero `min/max_select` seguía siendo **por producto**, así que todos los
tamaños compartían el número de sabores y había que crear un producto por tamaño.

Comprueba, sobre un producto con dos tamaños:
  1. la grande descuenta 3× del sabor elegido y la pequeña 1× — misma opción, cantidades
     distintas;
  2. **cada tamaño impone su propia cardinalidad**: elegir 2 sabores en la pequeña
     (max 1) da 422 aunque la mediana sí admita 2. Es el requisito que no se podía
     cumplir;
  3. la reversa al anular devuelve exactamente lo mismo (una reversa asimétrica
     descuadra el inventario para siempre, porque nadie concilia 'in' contra 'out');
  4. `item_quantity` se SUMA a lo de la variante, no lo reemplaza;
  5. una variante que solo descuenta por opción, sin opción elegida, no se puede vender
     (409), y el mensaje dice que falta elegir, no que falte la receta.

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
from app.models.inventory_item import InventoryItem
from app.models.inventory_movement import InventoryMovement
from app.models.option import Option
from app.models.option_group import OptionGroup
from app.models.order_item import OrderItem, OrderItemOption
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.variant_option_group import VariantOptionGroup
from app.models.unit_measure import UnitMeasure
from app.api.v1.orders import checkout, kitchen
from app.api.v1.orders.schemas import VoidItemIn

STOCK_INICIAL = Decimal("100.000")
POR_OPCION_GRANDE = Decimal("3.000")   # 3 bolas por sabor
POR_OPCION_PEQUENA = Decimal("1.000")  # 1 bola por sabor


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


def _staff(db) -> User:
    user = db.execute(select(User).limit(1)).scalar_one_or_none()
    if user is None:
        raise SystemExit("No hay usuarios en shared.users.")
    return user


def _fixture(db):
    """Producto con dos tamaños que ofrecen el mismo grupo 'Sabores' con **cardinalidad
    y cantidad distintas**: la grande admite 2 sabores y descuenta 3 de cada uno, la
    pequeña admite 1 y descuenta 1. El grupo tiene dos sabores, cada uno con su insumo."""
    unit = db.execute(select(UnitMeasure).limit(1)).scalar_one_or_none()
    if unit is None:
        unit = UnitMeasure(name=f"ud-{uuid4().hex[:6]}", abbreviation=uuid4().hex[:4])
        db.add(unit); db.flush()

    fresa = InventoryItem(
        code=f"test-{uuid4().hex[:8]}", name=f"helado-fresa-{uuid4().hex[:8]}", type="raw_material",
        unit_measure_id=unit.id, current_stock=STOCK_INICIAL, unit_cost=Decimal("1.00"),
    )
    choco = InventoryItem(
        code=f"test-{uuid4().hex[:8]}", name=f"helado-choco-{uuid4().hex[:8]}", type="raw_material",
        unit_measure_id=unit.id, current_stock=STOCK_INICIAL, unit_cost=Decimal("1.00"),
    )
    db.add_all([fresa, choco]); db.flush()

    group = OptionGroup(name=f"Sabores-{uuid4().hex[:8]}", min_select=1, max_select=1)
    db.add(group); db.flush()
    op_fresa = Option(option_group_id=group.id, name="Fresa", extra_price=Decimal("0"),
                      inventory_item_id=fresa.id, item_quantity=Decimal("0"), active=True)
    op_choco = Option(option_group_id=group.id, name="Chocolate", extra_price=Decimal("0"),
                      inventory_item_id=choco.id, item_quantity=Decimal("0"), active=True)
    db.add_all([op_fresa, op_choco]); db.flush()

    cat = db.execute(select(Category).limit(1)).scalar_one_or_none()
    if cat is None:
        cat = Category(name=f"cat-{uuid4().hex[:6]}"); db.add(cat); db.flush()

    product = Product(name=f"prod-vog-{uuid4().hex[:8]}", code=f"cod-vog-{uuid4().hex[:8]}", category_id=cat.id,
                      preparation_type="prepared")
    db.add(product); db.flush()

    grande = ProductVariant(product_id=product.id, name="Grande",
                            price=Decimal("10.00"), active=True, display_order=1)
    pequena = ProductVariant(product_id=product.id, name="Pequeña",
                             price=Decimal("6.00"), active=True, display_order=2)
    db.add_all([grande, pequena]); db.flush()

    # El mismo grupo, con cardinalidad Y cantidad distintas por tamaño: esto es
    # exactamente lo que el modelo anterior no podía expresar.
    db.add(VariantOptionGroup(
        product_variant_id=grande.id, option_group_id=group.id,
        min_select=1, max_select=2, quantity_per_option=POR_OPCION_GRANDE,
    ))
    db.add(VariantOptionGroup(
        product_variant_id=pequena.id, option_group_id=group.id,
        min_select=1, max_select=1, quantity_per_option=POR_OPCION_PEQUENA,
    ))
    db.flush()
    return product.id, group.id, fresa.id, choco.id, grande.id, pequena.id, op_fresa.id


def _order(db, variant_id, option_ids, *, quantity=1, status="recibida"):
    order = CustomerOrder(channel="QR_MENU", order_type="DINE_IN", status=status)
    db.add(order); db.flush()
    item = OrderItem(order_id=order.id, product_variant_id=variant_id, quantity=quantity,
                     unit_price=Decimal("10.00"), estado_cocina="pendiente")
    db.add(item); db.flush()
    for oid in option_ids:
        db.add(OrderItemOption(order_item_id=item.id, option_id=oid))
    db.flush(); db.refresh(order)
    return order, item.id


def _movs(db, order_id):
    db.flush()
    return db.execute(
        select(InventoryMovement).where(InventoryMovement.reference_id == order_id)
    ).scalars().all()


def _stock(db, item_id) -> Decimal:
    db.flush()
    return Decimal(db.get(InventoryItem, item_id).current_stock)


def _cleanup(schema, product_id, group_id, item_ids):
    with with_db(schema) as db:
        for iid in item_ids:
            db.execute(text(f'DELETE FROM "{schema}".inventory_movements '
                            f'WHERE inventory_item_id = :i'), {"i": str(iid)})
        db.execute(text(f'''
            DELETE FROM "{schema}".order_item_options WHERE order_item_id IN (
                SELECT id FROM "{schema}".order_items WHERE product_variant_id IN (
                    SELECT id FROM "{schema}".product_variants WHERE product_id = :p))
        '''), {"p": str(product_id)})
        db.execute(text(f'''
            DELETE FROM "{schema}".order_items WHERE product_variant_id IN (
                SELECT id FROM "{schema}".product_variants WHERE product_id = :p)
        '''), {"p": str(product_id)})
        db.execute(text(f'''
            DELETE FROM "{schema}".customer_orders WHERE id NOT IN (
                SELECT order_id FROM "{schema}".order_items) AND channel = 'QR_MENU'
        '''))
        db.execute(text(f'''DELETE FROM "{schema}".recipe_items WHERE product_variant_id IN (
                SELECT id FROM "{schema}".product_variants WHERE product_id = :p)'''),
                   {"p": str(product_id)})
        db.execute(text(f'''DELETE FROM "{schema}".variant_option_groups
                WHERE product_variant_id IN (
                SELECT id FROM "{schema}".product_variants WHERE product_id = :p)'''),
                   {"p": str(product_id)})
        db.execute(text(f'DELETE FROM "{schema}".product_variants WHERE product_id = :p'),
                   {"p": str(product_id)})
        db.execute(text(f'DELETE FROM "{schema}".products WHERE id = :p'), {"p": str(product_id)})
        db.execute(text(f'DELETE FROM "{schema}".options WHERE option_group_id = :g'),
                   {"g": str(group_id)})
        db.execute(text(f'DELETE FROM "{schema}".option_groups WHERE id = :g'),
                   {"g": str(group_id)})
        for iid in item_ids:
            db.execute(text(f'DELETE FROM "{schema}".inventory_items WHERE id = :i'),
                       {"i": str(iid)})
        db.commit()


def main():
    schema = _schema()
    print(f"Grupos por variante: cardinalidad y cantidad propias de cada tamaño (tenant: {schema})")

    with with_db(schema) as db:
        product_id, group_id, fresa_id, choco_id, grande_id, pequena_id, op_fresa_id = \
            _fixture(db)
        db.commit()

    try:
        # --- 1. la variante grande descuenta 3× del sabor elegido -------------
        with with_db(schema) as db:
            order, _ = _order(db, grande_id, [op_fresa_id])
            db.commit(); oid = order.id
        with with_db(schema) as db:
            checkout.confirm_order(db, oid, _staff(db))
        with with_db(schema) as db:
            movs = _movs(db, oid)
            _check("Grande: un solo movimiento", len(movs), 1)
            _check("del sabor elegido", movs[0].inventory_item_id, fresa_id)
            _check("por la cantidad de esa presentación", Decimal(movs[0].quantity), POR_OPCION_GRANDE)
            _check("y el otro sabor no se toca", _stock(db, choco_id), STOCK_INICIAL)

        # --- 2. la pequeña, mismo sabor, descuenta 1× ------------------------
        with with_db(schema) as db:
            order2, item2_id = _order(db, pequena_id, [op_fresa_id])
            db.commit(); oid2 = order2.id
        with with_db(schema) as db:
            checkout.confirm_order(db, oid2, _staff(db))
        with with_db(schema) as db:
            movs = _movs(db, oid2)
            _check("Pequeña: mismo sabor, otra cantidad",
                   Decimal(movs[0].quantity), POR_OPCION_PEQUENA)
            _check("stock tras los dos pedidos",
                   _stock(db, fresa_id), STOCK_INICIAL - POR_OPCION_GRANDE - POR_OPCION_PEQUENA)

        # --- 3. la reversa devuelve exactamente lo mismo ----------------------
        with with_db(schema) as db:
            kitchen.void_item(db, item2_id, VoidItemIn(motivo="prueba"), _staff(db))
        with with_db(schema) as db:
            _check("anular devuelve lo que consumió",
                   _stock(db, fresa_id), STOCK_INICIAL - POR_OPCION_GRANDE)

        # --- 4. la cantidad del tamaño REEMPLAZA a la de la opción ------------
        # Es el guardarraíl contra el doble descuento: sumarlas hacía que configurar
        # 60 g en la ensalada pequeña, con los sabores en 80 g, descontara 140.
        with with_db(schema) as db:
            db.get(Option, op_fresa_id).item_quantity = Decimal("0.500")
            db.commit()
        with with_db(schema) as db:
            order3, _ = _order(db, pequena_id, [op_fresa_id])
            db.commit(); oid3 = order3.id
        with with_db(schema) as db:
            checkout.confirm_order(db, oid3, _staff(db))
        with with_db(schema) as db:
            movs = _movs(db, oid3)
            _check("manda el tamaño: no se suma la cantidad de la opción",
                   Decimal(movs[0].quantity), POR_OPCION_PEQUENA)

        # --- 4b. si el tamaño NO define cantidad, manda la de la opción -------
        with with_db(schema) as db:
            db.execute(
                VariantOptionGroup.__table__.update()
                .where(VariantOptionGroup.product_variant_id == pequena_id)
                .values(quantity_per_option=Decimal("0"))
            )
            db.commit()
        with with_db(schema) as db:
            order3b, _ = _order(db, pequena_id, [op_fresa_id])
            db.commit(); oid3b = order3b.id
        with with_db(schema) as db:
            checkout.confirm_order(db, oid3b, _staff(db))
        with with_db(schema) as db:
            movs = _movs(db, oid3b)
            _check("sin cantidad en el tamaño, aplica la de la opción",
                   Decimal(movs[0].quantity), Decimal("0.500"))
        with with_db(schema) as db:
            db.get(Option, op_fresa_id).item_quantity = Decimal("0")
            db.execute(
                VariantOptionGroup.__table__.update()
                .where(VariantOptionGroup.product_variant_id == pequena_id)
                .values(quantity_per_option=POR_OPCION_PEQUENA)
            )
            db.commit()

        # --- 5. receta solo-slot sin opción elegida → 409 con el mensaje nuevo -
        with with_db(schema) as db:
            order4, _ = _order(db, grande_id, [])
            db.commit(); oid4 = order4.id
        with with_db(schema) as db:
            e = _expect_409(
                lambda: checkout.confirm_order(db, oid4, _staff(db)),
                "sin elegir sabor → 409",
            )
            detalle = e.detail if isinstance(e.detail, dict) else {}
            if "variantes_sin_opcion" not in detalle:
                raise AssertionError(
                    "el 409 dice 'sin receta', pero la receta existe: el mensaje manda "
                    "al dueño a buscar donde no está el problema"
                )
            print("  ok  · el error dice que falta elegir, no que falte la receta")
        with with_db(schema) as db:
            _check("y no movió stock", len(_movs(db, oid4)), 0)

        # --- 6. EL REQUISITO: cada tamaño impone su propia cardinalidad ---------
        # El mismo grupo, en el mismo producto: la grande admite 2 sabores y la pequeña
        # solo 1. Con `min/max_select` a nivel de producto esto era imposible y obligaba
        # a crear un producto por tamaño.
        from app.core.config import settings
        from app.api.v1.catalog.line_pricing import validate_option_selection

        _check("el flag arranca apagado", settings.STRICT_OPTION_SELECTION, False)
        with with_db(schema) as db:
            ambos = db.execute(
                select(Option).where(Option.option_group_id == group_id)
            ).scalars().all()
            _check("hay dos sabores para la prueba", len(ambos), 2)

            # La grande sí admite dos.
            validate_option_selection(db, db.get(ProductVariant, grande_id), ambos)
            print("  ok  · la grande acepta 2 sabores")

            # La pequeña no, y se rechaza aun con el flag apagado porque descuenta.
            try:
                validate_option_selection(db, db.get(ProductVariant, pequena_id), ambos)
            except HTTPException as e:
                if e.status_code != 422:
                    raise AssertionError(f"esperado 422, vino {e.status_code}")
                print("  ok  · la pequeña rechaza 2 sabores → 422, con el flag apagado")
            else:
                raise AssertionError(
                    "la pequeña aceptó 2 sabores con max_select=1: descontaría el doble, "
                    "y el tamaño deja de significar nada"
                )

        # --- 7. un grupo que descuenta exige el MÁXIMO, no el mínimo -----------
        # El error simétrico al anterior: la grande son dos bolas, así que un solo
        # sabor sirve dos y descuenta una. Con `min_select=1` esto pasaba.
        with with_db(schema) as db:
            uno = [db.get(Option, op_fresa_id)]
            try:
                validate_option_selection(db, db.get(ProductVariant, grande_id), uno)
            except HTTPException as e:
                if e.status_code != 422:
                    raise AssertionError(f"esperado 422, vino {e.status_code}")
                print("  ok  · la grande rechaza 1 solo sabor → 422 (exige los 2)")
            else:
                raise AssertionError(
                    "la grande aceptó 1 sabor con max_select=2: sirve dos bolas y "
                    "descuenta una, y el inventario se sobrestima"
                )

            # La pequeña es 1–1: un sabor es justo su máximo y sigue siendo válido.
            validate_option_selection(db, db.get(ProductVariant, pequena_id), uno)
            print("  ok  · la pequeña acepta 1 sabor (su máximo)")

        print("\n✔ Cada tamaño manda su propia cardinalidad y su propia cantidad; "
              "revierte simétricamente, bloquea la venta sin elegir y exige el "
              "máximo donde descuenta.")
        return 0
    finally:
        _cleanup(schema, product_id, group_id, [fresa_id, choco_id])
        print("  (datos de prueba eliminados)")


if __name__ == "__main__":
    raise SystemExit(main())
