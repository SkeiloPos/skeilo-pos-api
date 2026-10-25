"""Test de compra con conversión de unidades (spec 134).

No hay pytest en el proyecto, así que es un script autoejecutable:

    python -m app.scripts.test_purchase_unit_conversion

Cubre los Acceptance Scenarios de las 3 historias de usuario de la spec: registrar una
compra en la unidad del proveedor (US1), elegir solo unidades válidas para el insumo
(US2) y conservar el historial frente a cambios posteriores (US3) -- ver
quickstart.md Escenarios 1, 3, 4 y 5.

Trabaja sobre un tenant real con datos desechables: crea sus propias unidades e insumos, y
los borra al terminar (no basta con un rollback final porque varias rutas hacen commit por
dentro).
"""
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4, UUID

from sqlalchemy import select, text
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.db import with_db
from app.models.inventory_item import InventoryItem
from app.models.unit_measure import UnitMeasure
from app.models.purchase import Purchase
from app.api.v1.inventory import service
from app.api.v1.inventory.schemas import PurchaseCreate, PurchaseItemIn
from app.api.v1.unit_measures import router as unit_measures_router
from app.api.v1.unit_measures.schemas import UnitMeasureUpdate


def _schema() -> str:
    """Primer esquema de tenant que ya tiene las tablas base."""
    with with_db(None) as db:
        rows = db.execute(text("SELECT schema FROM shared.tenants ORDER BY id")).scalars().all()
    for schema in rows:
        with with_db(None) as db:
            exists = db.execute(
                text("SELECT to_regclass(:q)"), {"q": f"{schema}.inventory_items"}
            ).scalar() is not None
        if exists:
            return schema
    raise SystemExit("Ningún tenant tiene su esquema base creado todavía.")


def _fixture(db):
    masa = f"masa-test-{uuid4().hex[:6]}"
    volumen = f"volumen-test-{uuid4().hex[:6]}"

    g = UnitMeasure(name=f"gramo-test-{uuid4().hex[:6]}", abbreviation=f"g{uuid4().hex[:5]}", dimension=masa, factor_to_base=Decimal("1"))
    kg = UnitMeasure(name=f"kilogramo-test-{uuid4().hex[:6]}", abbreviation=f"kg{uuid4().hex[:5]}", dimension=masa, factor_to_base=Decimal("1000"))
    lb = UnitMeasure(name=f"libra-test-{uuid4().hex[:6]}", abbreviation=f"lb{uuid4().hex[:5]}", dimension=masa, factor_to_base=Decimal("453.592"))
    ml = UnitMeasure(name=f"mililitro-test-{uuid4().hex[:6]}", abbreviation=f"ml{uuid4().hex[:5]}", dimension=volumen, factor_to_base=Decimal("1"))
    db.add_all([g, kg, lb, ml])
    db.flush()

    azucar = InventoryItem(
        code=f"test-{uuid4().hex[:8]}", name=f"Azucar-test-{uuid4().hex[:8]}", type="raw_material",
        unit_measure_id=g.id, current_stock=Decimal("0"), unit_cost=Decimal("0"),
    )
    leche = InventoryItem(
        code=f"test-{uuid4().hex[:8]}", name=f"Leche-test-{uuid4().hex[:8]}", type="raw_material",
        unit_measure_id=ml.id, current_stock=Decimal("0"), unit_cost=Decimal("0"),
    )
    db.add_all([azucar, leche])
    db.flush()
    db.commit()
    return masa, volumen, g, kg, lb, ml, azucar, leche


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


def _set_tenant_unit_conversion(schema: str, enabled: bool) -> None:
    with with_db(None) as db:
        db.execute(
            text("UPDATE shared.tenants SET unit_conversion_enabled = :e WHERE schema = :s"),
            {"e": enabled, "s": schema},
        )
        db.commit()


def _cleanup(db, schema: str, purchase_ids, unit_ids, item_ids):
    if purchase_ids:
        ids = [str(i) for i in purchase_ids]
        db.execute(text(f'DELETE FROM "{schema}".inventory_movements WHERE reference_id = ANY(:p)'), {"p": ids})
        db.execute(text(f'DELETE FROM "{schema}".purchase_items WHERE purchase_id = ANY(:p)'), {"p": ids})
        db.execute(text(f'DELETE FROM "{schema}".purchases WHERE id = ANY(:p)'), {"p": ids})
    if unit_ids:
        ids = [str(i) for i in unit_ids]
        db.execute(text(f'DELETE FROM "{schema}".unit_measure_factor_changes WHERE unit_measure_id = ANY(:u)'), {"u": ids})
    for item_id in item_ids:
        db.execute(text(f'DELETE FROM "{schema}".inventory_items WHERE id = :i'), {"i": str(item_id)})
    if unit_ids:
        db.execute(text(f'DELETE FROM "{schema}".unit_measures WHERE id = ANY(:u)'), {"u": [str(i) for i in unit_ids]})
    db.commit()
    _set_tenant_unit_conversion(schema, True)


def main():
    schema = _schema()
    print(f"Compra con conversión de unidades (tenant: {schema})")

    with with_db(schema) as db:
        masa, volumen, g, kg, lb, ml, azucar, leche = _fixture(db)
        purchase_ids: list[UUID] = []

        try:
            # ===================== Historia 1 =====================
            compra1 = service.create_purchase(
                db, PurchaseCreate(items=[PurchaseItemIn(
                    inventory_item_id=azucar.id, unit_measure_id=kg.id,
                    quantity=Decimal("2"), unit_cost=Decimal("8000"),
                )]), user_id=None,
            )
            purchase_ids.append(compra1.id)
            pi1 = compra1.items[0]
            _check("KG→g: conversion_factor_applied", pi1.conversion_factor_applied, Decimal("1000.000000"))
            _check("KG→g: quantity_base", pi1.quantity_base, Decimal("2000.000000"))
            _check("KG→g: cost_base", pi1.cost_base, Decimal("8.000000"))
            azucar_reloaded = db.get(InventoryItem, azucar.id)
            _check("KG→g: stock sube a 2000 g", azucar_reloaded.current_stock, Decimal("2000.000000"))
            _check("KG→g: costo del insumo = cost_base", azucar_reloaded.unit_cost, Decimal("8.000000"))

            compra2 = service.create_purchase(
                db, PurchaseCreate(items=[PurchaseItemIn(
                    inventory_item_id=azucar.id, unit_measure_id=lb.id,
                    quantity=Decimal("3"), unit_cost=Decimal("3000"),
                )]), user_id=None,
            )
            purchase_ids.append(compra2.id)
            azucar_reloaded = db.get(InventoryItem, azucar.id)
            _check(
                "LB→g: stock acumulado sin redondeo intermedio",
                azucar_reloaded.current_stock, Decimal("3360.776000"),
            )

            _expect_validation_error(
                "cantidad <= 0 rechazada (Field gt=0)",
                lambda: PurchaseItemIn(
                    inventory_item_id=azucar.id, unit_measure_id=kg.id,
                    quantity=Decimal("0"), unit_cost=Decimal("8000"),
                ),
            )
            _expect_validation_error(
                "costo <= 0 rechazado",
                lambda: PurchaseItemIn(
                    inventory_item_id=azucar.id, unit_measure_id=kg.id,
                    quantity=Decimal("2"), unit_cost=Decimal("0"),
                ),
            )

            # ===================== Historia 2 =====================
            masa_list = unit_measures_router.list_unit_measures(active=True, dimension=masa, db=db, _=None)
            masa_ids = {u.id for u in masa_list}
            _check("selector filtrado por dimensión: incluye KG/LB/g", {kg.id, lb.id, g.id} <= masa_ids, True)
            _check("selector filtrado por dimensión: no incluye ml (otra dimensión)", ml.id in masa_ids, False)

            _expect_error(
                "dimensión incompatible (ml en insumo de masa): rechazado", 422,
                lambda: service.create_purchase(
                    db, PurchaseCreate(items=[PurchaseItemIn(
                        inventory_item_id=azucar.id, unit_measure_id=ml.id,
                        quantity=Decimal("1"), unit_cost=Decimal("1000"),
                    )]), user_id=None,
                ),
            )

            lb.active = False
            db.add(lb)
            db.commit()
            masa_list_tras_desactivar = unit_measures_router.list_unit_measures(active=True, dimension=masa, db=db, _=None)
            _check(
                "unidad desactivada desaparece del selector",
                lb.id in {u.id for u in masa_list_tras_desactivar}, False,
            )
            _expect_error(
                "unidad inactiva usada directamente: rechazada", 422,
                lambda: service.create_purchase(
                    db, PurchaseCreate(items=[PurchaseItemIn(
                        inventory_item_id=azucar.id, unit_measure_id=lb.id,
                        quantity=Decimal("1"), unit_cost=Decimal("1000"),
                    )]), user_id=None,
                ),
            )

            _set_tenant_unit_conversion(schema, False)
            tenant_off = SimpleNamespace(unit_conversion_enabled=False)
            _expect_error(
                "unit_conversion_enabled=false: unidad distinta a la base rechazada", 422,
                lambda: service.create_purchase(
                    db, PurchaseCreate(items=[PurchaseItemIn(
                        inventory_item_id=azucar.id, unit_measure_id=kg.id,
                        quantity=Decimal("1"), unit_cost=Decimal("1000"),
                    )]), user_id=None, tenant=tenant_off,
                ),
            )
            compra_base = service.create_purchase(
                db, PurchaseCreate(items=[PurchaseItemIn(
                    inventory_item_id=azucar.id, unit_measure_id=g.id,
                    quantity=Decimal("50"), unit_cost=Decimal("8"),
                )]), user_id=None, tenant=tenant_off,
            )
            purchase_ids.append(compra_base.id)
            _check(
                "unit_conversion_enabled=false: la unidad base sigue funcionando",
                compra_base.items[0].quantity_base, Decimal("50.000000"),
            )
            _set_tenant_unit_conversion(schema, True)

            # ===================== Historia 3 =====================
            admin = SimpleNamespace(id=uuid4(), role=SimpleNamespace(name="ADMIN"))
            cajero = SimpleNamespace(id=uuid4(), role=SimpleNamespace(name="CASHIER"))

            _expect_error(
                "cambiar factor sin rol ADMIN: rechazado", 403,
                lambda: unit_measures_router.update_unit_measure(
                    id=kg.id, body=UnitMeasureUpdate(factor_to_base=Decimal("1100")),
                    db=db, user=cajero,
                ),
            )
            unit_measures_router.update_unit_measure(
                id=kg.id, body=UnitMeasureUpdate(factor_to_base=Decimal("1100")), db=db, user=admin,
            )
            cambios = unit_measures_router.list_factor_changes(id=kg.id, db=db, _=None)
            _check("historial de factor: una fila", len(cambios), 1)
            _check("historial de factor: old_factor", cambios[0].old_factor, Decimal("1000.000000"))
            _check("historial de factor: new_factor", cambios[0].new_factor, Decimal("1100.000000"))
            _check("historial de factor: changed_by = admin", cambios[0].changed_by, admin.id)

            pi1_reloaded = db.get(Purchase, compra1.id).items[0]
            _check(
                "compra confirmada conserva su factor original tras el cambio",
                pi1_reloaded.conversion_factor_applied, Decimal("1000.000000"),
            )
            _check(
                "compra confirmada conserva su quantity_base original tras el cambio",
                pi1_reloaded.quantity_base, Decimal("2000.000000"),
            )

            stock_antes_void = db.get(InventoryItem, azucar.id).current_stock
            service.void_purchase(db, compra1.id, user_id=admin.id)
            stock_tras_void = db.get(InventoryItem, azucar.id).current_stock
            _check(
                "anulación: resta exactamente quantity_base guardada (no recalculada con 1100)",
                stock_antes_void - stock_tras_void, Decimal("2000.000000"),
            )

            _expect_error(
                "doble anulación de la misma compra: rechazada", 409,
                lambda: service.void_purchase(db, compra1.id, user_id=admin.id),
            )

            print("TODO OK ✔")
        finally:
            db.rollback()
            _cleanup(db, schema, purchase_ids, [g.id, kg.id, lb.id, ml.id], [azucar.id, leche.id])


if __name__ == "__main__":
    main()
