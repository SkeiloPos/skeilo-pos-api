"""Test de "Guardar borrador" (spec 140, User Story 3).

No hay pytest en el proyecto, así que es un script autoejecutable:

    python -m app.scripts.test_purchase_draft_incomplete

Cubre el Acceptance Scenario de la User Story 3 (quickstart.md Escenario 3):
`allow_incomplete=true` persiste una captura incompleta (sin costo o sin ninguna línea)
como `status='draft', is_incomplete=true` sin alta de stock; `allow_incomplete=false`
(u omitido) conserva el comportamiento actual de "orden de compra pendiente"
(`is_incomplete=false`) y sigue exigiendo ≥1 línea válida.

Trabaja sobre un tenant real con datos desechables: crea su propio insumo y lo borra al
terminar (no basta con un rollback final porque el service hace commit por dentro).
"""
from decimal import Decimal
from uuid import uuid4, UUID

from sqlalchemy import text
from pydantic import ValidationError

from fastapi import HTTPException

from app.core.db import with_db
from app.models.inventory_item import InventoryItem
from app.models.purchase import Purchase
from app.models.unit_measure import UnitMeasure
from app.api.v1.inventory import service
from app.api.v1.inventory.schemas import PurchaseCreate, PurchaseItemIn, PurchaseOrderCreate, PurchaseOrderItemIn


def _expect_error(label, status_code, fn):
    try:
        fn()
    except HTTPException as exc:
        _check(label, exc.status_code, status_code)
    else:
        raise AssertionError(f"{label}: se esperaba HTTPException {status_code}, no se lanzó nada")


def _schema() -> str:
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


def _check(label, actual, esperado):
    if actual != esperado:
        raise AssertionError(f"{label}: esperado {esperado!r}, obtenido {actual!r}")
    print(f"  ok  · {label}")


def _expect_validation_error(label, fn):
    try:
        fn()
    except ValidationError:
        print(f"  ok  · {label}")
    else:
        raise AssertionError(f"{label}: se esperaba un error de validación, no se lanzó nada")


def _cleanup(db, schema: str, purchase_ids: list[UUID], item_ids: list[UUID]):
    if purchase_ids:
        ids = [str(i) for i in purchase_ids]
        db.execute(text(f'DELETE FROM "{schema}".inventory_movements WHERE reference_id = ANY(:p)'), {"p": ids})
        db.execute(text(f'DELETE FROM "{schema}".purchase_items WHERE purchase_id = ANY(:p)'), {"p": ids})
        db.execute(text(f'DELETE FROM "{schema}".purchases WHERE id = ANY(:p)'), {"p": ids})
    for item_id in item_ids:
        db.execute(text(f'DELETE FROM "{schema}".inventory_items WHERE id = :i'), {"i": str(item_id)})
    db.commit()


def main():
    schema = _schema()
    print(f"Guardar borrador incompleto (tenant: {schema})")

    with with_db(schema) as db:
        base_unit = db.query(UnitMeasure).filter(UnitMeasure.active.is_(True)).first()
        if base_unit is None:
            raise SystemExit("El tenant no tiene ninguna unidad de medida activa.")

        insumo = InventoryItem(
            code=f"test-{uuid4().hex[:8]}", name=f"Insumo-test-{uuid4().hex[:8]}", type="raw_material",
            unit_measure_id=base_unit.id, current_stock=Decimal("0"), unit_cost=Decimal("0"),
        )
        db.add(insumo)
        db.flush()
        db.commit()
        purchase_ids: list[UUID] = []

        try:
            # ===================== allow_incomplete=True =====================
            borrador_sin_costo = service.create_purchase_order(
                db,
                PurchaseOrderCreate(
                    allow_incomplete=True,
                    items=[PurchaseOrderItemIn(inventory_item_id=insumo.id, quantity=Decimal("3"))],
                ),
                user_id=None,
            )
            purchase_ids.append(borrador_sin_costo.id)
            _check("borrador sin costo: status='draft'", borrador_sin_costo.status, "draft")
            _check("borrador sin costo: is_incomplete=true", borrador_sin_costo.is_incomplete, True)
            _check("borrador sin costo: no sube stock", db.get(InventoryItem, insumo.id).current_stock, Decimal("0"))
            _check("borrador sin costo: unit_cost default 0", borrador_sin_costo.items[0].unit_cost, Decimal("0.00"))

            borrador_sin_lineas = service.create_purchase_order(
                db, PurchaseOrderCreate(allow_incomplete=True, items=[]), user_id=None,
            )
            purchase_ids.append(borrador_sin_lineas.id)
            _check("borrador sin líneas: status='draft'", borrador_sin_lineas.status, "draft")
            _check("borrador sin líneas: is_incomplete=true", borrador_sin_lineas.is_incomplete, True)
            _check("borrador sin líneas: 0 ítems", len(borrador_sin_lineas.items), 0)

            # ===================== allow_incomplete=False (comportamiento actual) =====================
            _expect_validation_error(
                "allow_incomplete=false sin líneas: rechazado (sigue exigiendo ≥1 línea)",
                lambda: PurchaseOrderCreate(allow_incomplete=False, items=[]),
            )
            _expect_validation_error(
                "allow_incomplete omitido con línea sin quantity: rechazado",
                lambda: PurchaseOrderCreate(
                    items=[PurchaseOrderItemIn(inventory_item_id=insumo.id)],
                ),
            )

            orden_normal = service.create_purchase_order(
                db,
                PurchaseOrderCreate(
                    items=[PurchaseOrderItemIn(
                        inventory_item_id=insumo.id, quantity=Decimal("5"), unit_cost=Decimal("1000"),
                    )],
                ),
                user_id=None,
            )
            purchase_ids.append(orden_normal.id)
            _check("orden normal: status='draft'", orden_normal.status, "draft")
            _check("orden normal: is_incomplete=false", orden_normal.is_incomplete, False)
            _check("orden normal: no sube stock", db.get(InventoryItem, insumo.id).current_stock, Decimal("0"))

            # ===================== Editar en lugar de duplicar (spec 140) =====================
            # Reabrir un borrador y guardar de nuevo actualiza ESE registro -- nunca crea uno
            # nuevo que el usuario tendría que borrar a mano.
            borrador_a_convertir = service.create_purchase_order(
                db,
                PurchaseOrderCreate(
                    allow_incomplete=True,
                    items=[PurchaseOrderItemIn(inventory_item_id=insumo.id, quantity=Decimal("2"))],
                ),
                user_id=None,
            )
            purchase_ids.append(borrador_a_convertir.id)
            total_antes = len(purchase_ids)

            convertido = service.update_purchase(
                db, borrador_a_convertir.id,
                PurchaseCreate(items=[PurchaseItemIn(
                    inventory_item_id=insumo.id, quantity=Decimal("4"), unit_cost=Decimal("2000"),
                )]),
                user_id=None,
            )
            _check("editar borrador -> Registrar compra: mismo id (no duplica)", convertido.id, borrador_a_convertir.id)
            _check("editar borrador -> Registrar compra: status='received'", convertido.status, "received")
            _check("editar borrador -> Registrar compra: is_incomplete=false", convertido.is_incomplete, False)
            _check("editar borrador -> Registrar compra: sube stock (4)", db.get(InventoryItem, insumo.id).current_stock, Decimal("4.000000"))
            _check(
                "editar borrador -> Registrar compra: no quedó ningún registro adicional",
                len(purchase_ids), total_antes,
            )

            _expect_error(
                "editar una compra ya recibida: rechazada (409, historial inmutable)", 409,
                lambda: service.update_purchase(
                    db, convertido.id,
                    PurchaseCreate(items=[PurchaseItemIn(
                        inventory_item_id=insumo.id, quantity=Decimal("1"), unit_cost=Decimal("1"),
                    )]),
                    user_id=None,
                ),
            )

            orden_a_editar = service.create_purchase_order(
                db,
                PurchaseOrderCreate(items=[PurchaseOrderItemIn(
                    inventory_item_id=insumo.id, quantity=Decimal("1"), unit_cost=Decimal("500"),
                )]),
                user_id=None,
            )
            purchase_ids.append(orden_a_editar.id)
            actualizada = service.update_purchase_order(
                db, orden_a_editar.id,
                PurchaseOrderCreate(items=[PurchaseOrderItemIn(
                    inventory_item_id=insumo.id, quantity=Decimal("9"), unit_cost=Decimal("700"),
                )]),
                user_id=None,
            )
            _check("editar orden -> mismo id (no duplica)", actualizada.id, orden_a_editar.id)
            _check("editar orden -> sigue en draft", actualizada.status, "draft")
            _check("editar orden -> línea reemplazada (1 sola)", len(actualizada.items), 1)
            _check("editar orden -> cantidad actualizada", actualizada.items[0].quantity, Decimal("9.000"))
            _check("editar orden -> no sube stock", db.get(InventoryItem, insumo.id).current_stock, Decimal("4.000000"))

            # ===================== Eliminar orden/borrador pendiente (spec 140) =====================
            service.delete_purchase(db, actualizada.id)
            purchase_ids.remove(actualizada.id)
            _check("eliminar orden draft: ya no existe", db.get(Purchase, actualizada.id), None)

            borrador_a_eliminar = service.create_purchase_order(
                db, PurchaseOrderCreate(allow_incomplete=True, items=[]), user_id=None,
            )
            service.delete_purchase(db, borrador_a_eliminar.id)
            _check("eliminar borrador incompleto: ya no existe", db.get(Purchase, borrador_a_eliminar.id), None)

            _expect_error(
                "eliminar una compra ya recibida: rechazada (409, no es draft)", 409,
                lambda: service.delete_purchase(db, convertido.id),
            )
            _check(
                "eliminar rechazada: la compra sigue existiendo",
                db.get(Purchase, convertido.id) is not None, True,
            )

            print("TODO OK ✔")
        finally:
            db.rollback()
            _cleanup(db, schema, purchase_ids, [insumo.id])


if __name__ == "__main__":
    main()
