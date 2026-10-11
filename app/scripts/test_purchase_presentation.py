"""Test de comprar en la presentación real, no en la unidad base (spec 107).

No hay pytest en el proyecto, así que es un script autoejecutable:

    python -m app.scripts.test_purchase_presentation

Cubre los Acceptance Scenarios de las 3 historias de usuario de la spec: alta directa en
presentación (US1), último costo del proveedor actualizado solo (US2), orden + recepción
parcial en presentación (US3) -- más los errores de forma (quantity y supplier_item_id a la
vez) y de negocio (presentación que no corresponde al insumo/proveedor de la línea/compra).

Trabaja sobre un tenant real con datos desechables: crea su propio insumo, proveedores y
relación, y los borra al terminar.
"""
from decimal import Decimal
from uuid import uuid4, UUID

from sqlalchemy import select, text
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.db import with_db
from app.models.inventory_item import InventoryItem
from app.models.supplier import Supplier
from app.models.supplier_item import SupplierItem
from app.models.purchase import Purchase, PurchaseItem
from app.models.unit_measure import UnitMeasure
from app.api.v1.inventory import service, supplier_items as svc
from app.api.v1.inventory.schemas import (
    PurchaseCreate, PurchaseItemIn, PurchaseReceiveIn, PurchaseReceiveItemIn,
    SupplierItemCreate,
)


def _schema() -> str:
    with with_db(None) as db:
        rows = db.execute(text("SELECT schema FROM shared.tenants ORDER BY id")).scalars().all()
    for schema in rows:
        with with_db(None) as db:
            exists = db.execute(
                text("SELECT to_regclass(:q)"), {"q": f"{schema}.suppliers"}
            ).scalar() is not None
        if exists:
            return schema
    raise SystemExit("Ningún tenant tiene su esquema base creado todavía.")


def _fixture(db):
    unit = db.execute(select(UnitMeasure).where(UnitMeasure.abbreviation == "g")).scalar_one_or_none()
    if unit is None:
        unit = UnitMeasure(name=f"gramo-test-{uuid4().hex[:6]}", abbreviation="g")
        db.add(unit)
        db.flush()

    item = InventoryItem(
        code=f"test-{uuid4().hex[:8]}", name=f"Azucar-test-{uuid4().hex[:8]}", type="raw_material",
        unit_measure_id=unit.id, current_stock=Decimal("0"), unit_cost=Decimal("0"),
    )
    norte = Supplier(name=f"Distribuidora-test-{uuid4().hex[:6]}", is_system=False)
    otro_proveedor = Supplier(name=f"OtroProveedor-test-{uuid4().hex[:6]}", is_system=False)
    otro_insumo = InventoryItem(
        code=f"test-{uuid4().hex[:8]}", name=f"Otro-insumo-test-{uuid4().hex[:8]}", type="raw_material",
        unit_measure_id=unit.id, current_stock=Decimal("0"), unit_cost=Decimal("0"),
    )
    db.add_all([item, norte, otro_proveedor, otro_insumo])
    db.flush()

    rel, _ = svc.create_or_reactivate(
        db, supplier_id=norte.id, inventory_item_id=item.id,
        body=SupplierItemCreate(presentation="Bolsa x 500 g", conversion_factor=Decimal("500")),
    )
    db.commit()
    return item, norte, otro_proveedor, otro_insumo, rel


def _system_supplier(db) -> Supplier:
    supplier = db.execute(select(Supplier).where(Supplier.is_system.is_(True))).scalar_one_or_none()
    if supplier is None:
        raise SystemExit("No se encontró el proveedor de sistema 'Compra ocasional'.")
    return supplier


def _check(label, actual, esperado):
    if actual != esperado:
        raise AssertionError(f"{label}: esperado {esperado!r}, obtenido {actual!r}")
    print(f"  ok  · {label}")


def _expect_error(label, status_code, fn):
    try:
        fn()
    except HTTPException as exc:
        _check(label, exc.status_code, status_code)
    else:
        raise AssertionError(f"{label}: se esperaba HTTPException {status_code}, no se lanzó nada")


def _expect_validation_error(label, fn):
    try:
        fn()
    except (ValidationError, ValueError):
        print(f"  ok  · {label}")
    else:
        raise AssertionError(f"{label}: se esperaba un error de validación, no se lanzó nada")


def _cleanup(db, schema: str, purchase_ids, relation_ids, item_ids, supplier_ids):
    if purchase_ids:
        ids = [str(i) for i in purchase_ids]
        db.execute(text(f'DELETE FROM "{schema}".inventory_movements WHERE reference_id = ANY(:p)'), {"p": ids})
        db.execute(text(f'DELETE FROM "{schema}".purchase_items WHERE purchase_id = ANY(:p)'), {"p": ids})
        db.execute(text(f'DELETE FROM "{schema}".purchases WHERE id = ANY(:p)'), {"p": ids})
    if relation_ids:
        db.execute(text(f'DELETE FROM "{schema}".supplier_items WHERE id = ANY(:i)'),
                   {"i": [str(i) for i in relation_ids]})
    for item_id in item_ids:
        db.execute(text(f'DELETE FROM "{schema}".inventory_items WHERE id = :i'), {"i": str(item_id)})
    for supplier_id in supplier_ids:
        db.execute(text(f'DELETE FROM "{schema}".suppliers WHERE id = :s'), {"s": str(supplier_id)})
    db.commit()


def main():
    schema = _schema()
    print(f"Comprar en presentación real (tenant: {schema})")

    with with_db(schema) as db:
        item, norte, otro_proveedor, otro_insumo, rel = _fixture(db)
        purchase_ids: list[UUID] = []

        try:
            # ===================== Historia 1 =====================
            purchase = service.create_purchase(
                db, PurchaseCreate(
                    supplier_id=norte.id,
                    items=[PurchaseItemIn(
                        inventory_item_id=item.id, supplier_item_id=rel.id,
                        presentation_quantity=Decimal("1"), presentation_unit_cost=Decimal("4000"),
                    )],
                ), user_id=None,
            )
            purchase_ids.append(purchase.id)
            pi = purchase.items[0]
            _check("alta en presentación: quantity en unidad base", pi.quantity, Decimal("500.000"))
            _check("alta en presentación: unit_cost derivado", pi.unit_cost, Decimal("8.00"))
            _check("alta en presentación: presentation_quantity conservada", pi.presentation_quantity, Decimal("1.000"))
            _check("alta en presentación: presentation_label (snapshot)", pi.presentation_label, "Bolsa x 500 g")
            _check("alta en presentación: total de la línea con valores originales", purchase.total, Decimal("4000.00"))
            item_reloaded = db.get(InventoryItem, item.id)
            _check("alta en presentación: stock sube en 500 g", item_reloaded.current_stock, Decimal("500.000"))
            _check("alta en presentación: costo del insumo = 8.00/g", item_reloaded.unit_cost, Decimal("8.00"))

            purchase2 = service.create_purchase(
                db, PurchaseCreate(
                    supplier_id=norte.id,
                    items=[PurchaseItemIn(
                        inventory_item_id=item.id, supplier_item_id=rel.id,
                        presentation_quantity=Decimal("3"), presentation_unit_cost=Decimal("4000"),
                    )],
                ), user_id=None,
            )
            purchase_ids.append(purchase2.id)
            _check("3 bolsas: quantity = 1500 g", purchase2.items[0].quantity, Decimal("1500.000"))
            _check("3 bolsas: total = 12000", purchase2.total, Decimal("12000.00"))
            item_reloaded = db.get(InventoryItem, item.id)
            _check("3 bolsas: stock acumulado = 2000 g", item_reloaded.current_stock, Decimal("2000.000"))

            # Compra sin relación (insumo sin presentación con este proveedor) -- sin cambios.
            purchase3 = service.create_purchase(
                db, PurchaseCreate(
                    supplier_id=norte.id,
                    items=[PurchaseItemIn(
                        inventory_item_id=otro_insumo.id, quantity=Decimal("10"), unit_cost=Decimal("2"),
                    )],
                ), user_id=None,
            )
            purchase_ids.append(purchase3.id)
            _check("sin relación: comportamiento directo intacto", purchase3.items[0].quantity, Decimal("10.000"))
            _check("sin relación: sin presentación", purchase3.items[0].presentation_quantity, None)

            # Compra ocasional -- idem, sin selector de presentación posible.
            compra_ocasional = _system_supplier(db)
            purchase4 = service.create_purchase(
                db, PurchaseCreate(
                    supplier_id=compra_ocasional.id,
                    items=[PurchaseItemIn(
                        inventory_item_id=otro_insumo.id, quantity=Decimal("5"), unit_cost=Decimal("3"),
                    )],
                ), user_id=None,
            )
            purchase_ids.append(purchase4.id)
            _check("Compra ocasional: comportamiento directo intacto", purchase4.items[0].quantity, Decimal("5.000"))

            # Errores de forma y de negocio.
            _expect_validation_error(
                "quantity y supplier_item_id a la vez: rechazado",
                lambda: PurchaseItemIn(
                    inventory_item_id=item.id, quantity=Decimal("1"), supplier_item_id=rel.id,
                    presentation_quantity=Decimal("1"), presentation_unit_cost=Decimal("1"),
                ),
            )
            _expect_validation_error(
                "ni quantity ni supplier_item_id: rechazado",
                lambda: PurchaseItemIn(inventory_item_id=item.id),
            )
            _expect_error(
                "presentación de otro proveedor en esta compra: rechazado", 422,
                lambda: service.create_purchase(
                    db, PurchaseCreate(
                        supplier_id=otro_proveedor.id,
                        items=[PurchaseItemIn(
                            inventory_item_id=item.id, supplier_item_id=rel.id,
                            presentation_quantity=Decimal("1"), presentation_unit_cost=Decimal("4000"),
                        )],
                    ), user_id=None,
                ),
            )

            # ===================== Historia 2 =====================
            rel_reloaded = db.get(SupplierItem, rel.id)
            _check("último costo del proveedor actualizado", rel_reloaded.last_cost, Decimal("4000.00"))
            _check("fecha de último costo fijada", rel_reloaded.last_cost_at is not None, True)

            # ===================== Historia 3 =====================
            orden = service.create_purchase_order(
                db, PurchaseCreate(
                    supplier_id=norte.id,
                    items=[PurchaseItemIn(
                        inventory_item_id=item.id, supplier_item_id=rel.id,
                        presentation_quantity=Decimal("10"), presentation_unit_cost=Decimal("4200"),
                    )],
                ), user_id=None,
            )
            purchase_ids.append(orden.id)
            orden_item_id = orden.items[0].id
            _check("orden: sin alta de stock todavía", orden.status, "draft")
            _check("orden: quantity pactada = 5000 g (10 bolsas)", orden.items[0].quantity, Decimal("5000.000"))

            stock_antes_recepcion = db.get(InventoryItem, item.id).current_stock

            orden = service.receive_purchase(
                db, orden.id,
                PurchaseReceiveIn(items=[PurchaseReceiveItemIn(
                    purchase_item_id=orden_item_id, presentation_quantity=Decimal("5"),
                )]), user_id=None,
            )
            _check("recepción parcial (5 bolsas): status 'partial'", orden.status, "partial")
            stock_tras_primera = db.get(InventoryItem, item.id).current_stock
            _check("recepción parcial: stock sube 2500 g", stock_tras_primera - stock_antes_recepcion, Decimal("2500.000"))

            _expect_error(
                "recibir más de lo pendiente (en presentaciones): rechazado", 422,
                lambda: service.receive_purchase(
                    db, orden.id,
                    PurchaseReceiveIn(items=[PurchaseReceiveItemIn(
                        purchase_item_id=orden_item_id, presentation_quantity=Decimal("6"),
                    )]), user_id=None,
                ),
            )

            orden = service.receive_purchase(
                db, orden.id,
                PurchaseReceiveIn(items=[PurchaseReceiveItemIn(
                    purchase_item_id=orden_item_id, presentation_quantity=Decimal("5"),
                )]), user_id=None,
            )
            _check("recepción total: status 'received'", orden.status, "received")
            stock_tras_segunda = db.get(InventoryItem, item.id).current_stock
            _check("recepción total: stock sube otros 2500 g", stock_tras_segunda - stock_tras_primera, Decimal("2500.000"))

            rel_reloaded = db.get(SupplierItem, rel.id)
            _check(
                "recepción: el último costo del proveedor NO se vuelve a tocar (sigue el de la orden)",
                rel_reloaded.last_cost, Decimal("4200.00"),
            )

            print("TODO OK ✔")
        finally:
            db.rollback()
            _cleanup(
                db, schema, purchase_ids, [rel.id], [item.id, otro_insumo.id],
                [norte.id, otro_proveedor.id],
            )


if __name__ == "__main__":
    main()
