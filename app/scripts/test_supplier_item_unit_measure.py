"""Relación proveedor-insumo vinculada a una UnitMeasure del mismo grupo (spec 140).

No hay pytest en el proyecto, así que es un script autoejecutable:

    python -m app.scripts.test_supplier_item_unit_measure

Antes de esta spec, la "presentación" de una relación proveedor-insumo era texto libre +
un factor escrito a mano, sin relación con el catálogo de `unit_measures`. Ahora el admin
elige una `UnitMeasure` del mismo grupo que la unidad base del insumo (incluida la base
misma, por pedido explícito del usuario) y el factor efectivo pasa a ser
`unit_measures.factor_to_base` EN VIVO -- si alguien cambia ese factor después, la
relación lo refleja, pero una compra ya hecha con esa relación queda congelada
(`PurchaseItem.conversion_factor_applied`/`quantity_base`), sin verse afectada.

El modo legado (`presentation`/`conversion_factor` a mano) se conserva para relaciones ya
creadas -- no se tocan hasta que alguien las edite.

Trabaja sobre un tenant real con datos desechables: crea su propio grupo/unidades/insumo/
proveedor y los borra al terminar.
"""
from decimal import Decimal
from uuid import uuid4, UUID

from fastapi import HTTPException
from sqlalchemy import text

from app.core.db import with_db
from app.models.inventory_item import InventoryItem
from app.models.supplier import Supplier
from app.models.unit_measure import UnitMeasure
from app.models.unit_measure_group import UnitMeasureGroup
from app.api.v1.inventory import service, supplier_items
from app.api.v1.inventory.schemas import PurchaseCreate, PurchaseItemIn, SupplierItemCreate, SupplierItemUpdate


def _schema() -> str:
    with with_db(None) as db:
        rows = db.execute(text("SELECT schema FROM shared.tenants ORDER BY id")).scalars().all()
    for schema in rows:
        with with_db(None) as db:
            exists = db.execute(
                text("SELECT to_regclass(:q)"), {"q": f"{schema}.supplier_items"}
            ).scalar() is not None
        if exists:
            return schema
    raise SystemExit("Ningún tenant tiene la tabla supplier_items todavía.")


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


def _cleanup(db, schema: str, purchase_ids, supplier_ids, item_ids, unit_ids, group_ids):
    if purchase_ids:
        ids = [str(i) for i in purchase_ids]
        db.execute(text(f'DELETE FROM "{schema}".inventory_movements WHERE reference_id = ANY(:p)'), {"p": ids})
        db.execute(text(f'DELETE FROM "{schema}".purchase_items WHERE purchase_id = ANY(:p)'), {"p": ids})
        db.execute(text(f'DELETE FROM "{schema}".purchases WHERE id = ANY(:p)'), {"p": ids})
    if item_ids:
        db.execute(
            text(f'DELETE FROM "{schema}".supplier_items WHERE inventory_item_id = ANY(:i)'),
            {"i": [str(i) for i in item_ids]},
        )
    for item_id in item_ids:
        db.execute(text(f'DELETE FROM "{schema}".inventory_items WHERE id = :i'), {"i": str(item_id)})
    for supplier_id in supplier_ids:
        db.execute(text(f'DELETE FROM "{schema}".suppliers WHERE id = :s'), {"s": str(supplier_id)})
    if unit_ids:
        db.execute(
            text(f'DELETE FROM "{schema}".unit_measures WHERE id = ANY(:u)'), {"u": [str(i) for i in unit_ids]},
        )
    if group_ids:
        db.execute(
            text(f'DELETE FROM "{schema}".unit_measure_groups WHERE id = ANY(:g)'), {"g": [str(i) for i in group_ids]},
        )
    db.commit()


def main():
    schema = _schema()
    print(f"Relación proveedor-insumo vinculada a UnitMeasure (tenant: {schema})")

    with with_db(schema) as db:
        sufijo = uuid4().hex[:6]
        grupo = UnitMeasureGroup(name=f"conteo-test-{sufijo}")
        otro_grupo = UnitMeasureGroup(name=f"masa-test-{sufijo}")
        db.add_all([grupo, otro_grupo])
        db.flush()

        unidad = UnitMeasure(
            name=f"UNIDAD-test-{sufijo}", abbreviation=f"und{sufijo}",
            factor_to_base=Decimal("1"), group_id=grupo.id, is_base=True,
        )
        paca = UnitMeasure(
            name=f"PACA-test-{sufijo}", abbreviation=f"pca{sufijo}",
            factor_to_base=Decimal("24"), group_id=grupo.id,
        )
        otro_grupo_unidad = UnitMeasure(
            name=f"GR-test-{sufijo}", abbreviation=f"gr{sufijo}",
            factor_to_base=Decimal("1"), group_id=otro_grupo.id, is_base=True,
        )
        db.add_all([unidad, paca, otro_grupo_unidad])
        db.flush()

        insumo = InventoryItem(
            code=f"test-{uuid4().hex[:8]}", name=f"Insumo-test-{sufijo}", type="raw_material",
            unit_measure_id=unidad.id, current_stock=Decimal("0"), unit_cost=Decimal("0"),
        )
        proveedor = Supplier(name=f"Proveedor-test-{sufijo}")
        db.add_all([insumo, proveedor])
        db.flush()
        db.commit()

        purchase_ids: list[UUID] = []
        unidad_inactiva = None
        try:
            # ===================== Crear vinculada a una UnitMeasure del grupo =====================
            relacion, status_code = supplier_items.create_or_reactivate(
                db, supplier_id=proveedor.id, inventory_item_id=insumo.id,
                body=SupplierItemCreate(unit_measure_id=paca.id, last_cost=Decimal("12000")),
            )
            _check("crear con unit_measure_id: status 201", status_code, 201)
            resp = supplier_items.to_response(db, relacion)
            _check("respuesta: unit_measure_id", resp["unit_measure_id"], paca.id)
            _check("respuesta: presentation = nombre de la unidad (en vivo)", resp["presentation"], paca.name)
            _check("respuesta: conversion_factor = factor_to_base (en vivo)", resp["conversion_factor"], Decimal("24.000000"))

            # ===================== Incluir la unidad BASE como opción válida =====================
            relacion_base, _ = supplier_items.create_or_reactivate(
                db, supplier_id=proveedor.id, inventory_item_id=insumo.id,
                body=SupplierItemCreate(unit_measure_id=unidad.id),
            )
            resp_base = supplier_items.to_response(db, relacion_base)
            _check("unidad base permitida como presentación", resp_base["conversion_factor"], Decimal("1.000000"))

            # ===================== Rechazos =====================
            _expect_error(
                "unidad de OTRO grupo: rechazada (422)", 422,
                lambda: supplier_items.create_or_reactivate(
                    db, supplier_id=proveedor.id, inventory_item_id=insumo.id,
                    body=SupplierItemCreate(unit_measure_id=otro_grupo_unidad.id),
                ),
            )
            otro_grupo_unidad.active = False
            db.add(otro_grupo_unidad)
            db.commit()
            unidad_inactiva = UnitMeasure(
                name=f"INACTIVA-test-{sufijo}", abbreviation=f"ina{sufijo}",
                factor_to_base=Decimal("12"), group_id=grupo.id, active=False,
            )
            db.add(unidad_inactiva)
            db.commit()
            _expect_error(
                "unidad inactiva: rechazada (422)", 422,
                lambda: supplier_items.create_or_reactivate(
                    db, supplier_id=proveedor.id, inventory_item_id=insumo.id,
                    body=SupplierItemCreate(unit_measure_id=unidad_inactiva.id),
                ),
            )
            _expect_error(
                "misma unit_measure_id ya activa: 409", 409,
                lambda: supplier_items.create_or_reactivate(
                    db, supplier_id=proveedor.id, inventory_item_id=insumo.id,
                    body=SupplierItemCreate(unit_measure_id=paca.id),
                ),
            )

            # ===================== Usar la relación en una compra =====================
            compra = service.create_purchase(
                db,
                PurchaseCreate(supplier_id=proveedor.id, items=[PurchaseItemIn(
                    inventory_item_id=insumo.id, supplier_item_id=relacion.id,
                    presentation_quantity=Decimal("3"), presentation_unit_cost=Decimal("12000"),
                )]),
                user_id=None,
            )
            purchase_ids.append(compra.id)
            pi = compra.items[0]
            _check("compra: quantity_base = 3 * 24 (factor en vivo)", pi.quantity_base, Decimal("72.000000"))
            _check("compra: presentation_label = nombre de la unidad", pi.presentation_label, paca.name)
            insumo_reloaded = db.get(InventoryItem, insumo.id)
            _check("compra: stock sube 72", insumo_reloaded.current_stock, Decimal("72.000000"))

            # ===================== Factor congelado tras usarse (FR-014) =====================
            _expect_error(
                "cambiar unit_measure_id de una relación ya usada: rechazada (422)", 422,
                lambda: supplier_items.update(db, relacion.id, SupplierItemUpdate(unit_measure_id=unidad.id)),
            )

            # Cambiar el factor de la UnitMeasure vinculada (en vivo) NO altera la compra ya hecha.
            paca.factor_to_base = Decimal("30")
            db.add(paca)
            db.commit()
            pi_reloaded = db.get(InventoryItem, insumo.id)
            _check(
                "cambiar factor_to_base después no recalcula el stock ya dado de alta",
                pi_reloaded.current_stock, Decimal("72.000000"),
            )
            compra_reloaded = compra
            db.refresh(compra_reloaded)
            _check(
                "la línea ya comprada conserva su quantity_base congelada",
                compra_reloaded.items[0].quantity_base, Decimal("72.000000"),
            )
            # pero la relación, consultada de nuevo, SÍ refleja el nuevo factor en vivo:
            relacion_reloaded = db.get(type(relacion), relacion.id)
            _check(
                "la relación (no usada otra vez) sí refleja el nuevo factor en vivo",
                relacion_reloaded.effective_conversion_factor, Decimal("30"),
            )

            print("TODO OK ✔")
        finally:
            db.rollback()
            unit_ids = [unidad.id, paca.id, otro_grupo_unidad.id]
            if unidad_inactiva is not None:
                unit_ids.append(unidad_inactiva.id)
            _cleanup(
                db, schema, purchase_ids, [proveedor.id], [insumo.id],
                unit_ids, [grupo.id, otro_grupo.id],
            )


if __name__ == "__main__":
    main()
