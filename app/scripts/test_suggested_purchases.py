"""Test de "Compra Sugerida de Insumos" (spec 158, Historias 1 y 2, P1).

No hay pytest en el proyecto, así que es un script autoejecutable:

    python -m app.scripts.test_suggested_purchases

Cubre los Acceptance Scenarios de la Historia 1 (listado: preseleccionado con proveedor,
desmarcado sin proveedor con el aviso correspondiente, "sin insumos bajo mínimo", orden por
déficit descendente, paginación) y de la Historia 2 (generación agrupada por proveedor,
línea inválida sin crear ninguna orden, rollback total ante fallo simulado a mitad de la
creación, reenvío de la misma idempotency_key sin duplicar, `SupplierItem.last_cost` sin
cambios tras generar con un precio editado distinto al guardado).

Trabaja sobre un tenant real con datos desechables: crea sus propios insumos, proveedores y
relaciones, y los borra al terminar.
"""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4, UUID

from sqlalchemy import select, text
from fastapi import HTTPException

from app.core.db import with_db
from app.models.inventory_item import InventoryItem
from app.models.inventory_movement import InventoryMovement
from app.models.supplier import Supplier
from app.models.supplier_item import SupplierItem
from app.models.purchase import Purchase
from app.models.purchase_generation_request import PurchaseGenerationRequest
from app.models.unit_measure import UnitMeasure
from app.api.v1.inventory import router as inventory_router, service, supplier_items as svc
from app.api.v1.inventory.schemas import (
    PurchaseOrderCreate, PurchaseOrderItemIn, SupplierItemCreate, SuggestedPurchaseLineIn,
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


def _movements_for(db, inventory_item_id, purchase_id) -> list[InventoryMovement]:
    return db.execute(
        select(InventoryMovement).where(
            InventoryMovement.inventory_item_id == inventory_item_id,
            InventoryMovement.reference_type == "purchase",
            InventoryMovement.reference_id == purchase_id,
        )
    ).scalars().all()


def _unit(db) -> UnitMeasure:
    unit = db.execute(
        select(UnitMeasure).where(UnitMeasure.active.is_(True), UnitMeasure.is_base.is_(True))
    ).scalars().first()
    if unit is None:
        raise SystemExit("El tenant no tiene ninguna unidad base activa.")
    return unit


def _item(db, unit, current_stock, min_stock, unit_cost=Decimal("0")) -> InventoryItem:
    item = InventoryItem(
        code=f"test-{uuid4().hex[:8]}", name=f"Insumo-test-{uuid4().hex[:8]}", type="raw_material",
        unit_measure_id=unit.id, current_stock=current_stock, min_stock=min_stock, unit_cost=unit_cost,
    )
    db.add(item)
    db.flush()
    return item


def _preferred_relation(
    db, supplier: Supplier, item: InventoryItem, last_cost: Decimal | None,
) -> SupplierItem:
    relation, _ = svc.create_or_reactivate(
        db, supplier_id=supplier.id, inventory_item_id=item.id,
        body=SupplierItemCreate(
            presentation="Bolsa x 500 g", conversion_factor=Decimal("500"), last_cost=last_cost,
        ),
    )
    svc.set_preferred(db, relation.id)
    db.refresh(relation)
    return relation


def _cleanup(db, schema: str, purchase_ids, relation_ids, item_ids, supplier_ids, idempotency_keys):
    if idempotency_keys:
        db.execute(
            text(f'DELETE FROM "{schema}".purchase_generation_requests WHERE idempotency_key = ANY(:k)'),
            {"k": idempotency_keys},
        )
    if purchase_ids:
        ids = [str(i) for i in purchase_ids]
        db.execute(text(f'DELETE FROM "{schema}".purchase_items WHERE purchase_id = ANY(:p)'), {"p": ids})
        db.execute(text(f'DELETE FROM "{schema}".purchases WHERE id = ANY(:p)'), {"p": ids})
    if relation_ids:
        db.execute(text(f'DELETE FROM "{schema}".supplier_items WHERE id = ANY(:i)'),
                   {"i": [str(i) for i in relation_ids]})
    if item_ids:
        db.execute(text(f'DELETE FROM "{schema}".inventory_movements WHERE inventory_item_id = ANY(:i)'),
                   {"i": [str(i) for i in item_ids]})
    for item_id in item_ids:
        db.execute(text(f'DELETE FROM "{schema}".inventory_items WHERE id = :i'), {"i": str(item_id)})
    for supplier_id in supplier_ids:
        db.execute(text(f'DELETE FROM "{schema}".suppliers WHERE id = :s'), {"s": str(supplier_id)})
    db.commit()


def main():
    schema = _schema()
    print(f"Compra sugerida de insumos (tenant: {schema})")

    with with_db(schema) as db:
        unit = _unit(db)
        proveedor_a = Supplier(name=f"ProveedorA-test-{uuid4().hex[:6]}", is_system=False)
        proveedor_b = Supplier(name=f"ProveedorB-test-{uuid4().hex[:6]}", is_system=False)
        db.add_all([proveedor_a, proveedor_b])
        db.flush()

        purchase_ids: list[UUID] = []
        relation_ids: list[UUID] = []
        item_ids: list[UUID] = []
        supplier_ids = [proveedor_a.id, proveedor_b.id]
        idempotency_keys: list[str] = []

        try:
            # ===================== Historia 1: listado =====================
            # Déficit grande: bajo mínimo por mucho (preseleccionado).
            almendras = _item(db, unit, current_stock=Decimal("10"), min_stock=Decimal("500"))
            item_ids.append(almendras.id)
            relacion_almendras = _preferred_relation(db, proveedor_a, almendras, Decimal("8000"))
            relation_ids.append(relacion_almendras.id)

            # Déficit pequeño: bajo mínimo, sin proveedor preferido.
            agua = _item(db, unit, current_stock=Decimal("90"), min_stock=Decimal("100"))
            item_ids.append(agua.id)

            # No bajo mínimo: no debe aparecer en el listado.
            harina = _item(db, unit, current_stock=Decimal("100"), min_stock=Decimal("50"))
            item_ids.append(harina.id)

            # Relación preferida SIN last_cost propio (nunca se compró con ESE proveedor por
            # este sistema) pero el insumo SÍ tiene un último costo de compra conocido
            # (InventoryItem.unit_cost) -- el precio debe precargarse a partir de ese costo
            # convertido a la presentación (10 * 500 = 5000), no quedar en 0.
            sin_costo_relacion = _item(
                db, unit, current_stock=Decimal("0"), min_stock=Decimal("5"), unit_cost=Decimal("10"),
            )
            item_ids.append(sin_costo_relacion.id)
            relacion_sin_costo = _preferred_relation(db, proveedor_a, sin_costo_relacion, None)
            relation_ids.append(relacion_sin_costo.id)

            # Ni la relación ni el insumo tienen ningún costo conocido -- no hay de dónde
            # precargar un precio; sigue sin preseleccionarse, con un aviso de precio (no de
            # proveedor, que sí está configurado).
            sin_costo_nada = _item(
                db, unit, current_stock=Decimal("0"), min_stock=Decimal("5"), unit_cost=Decimal("0"),
            )
            item_ids.append(sin_costo_nada.id)
            relacion_sin_costo_nada = _preferred_relation(db, proveedor_a, sin_costo_nada, None)
            relation_ids.append(relacion_sin_costo_nada.id)
            db.commit()

            result = service.generate_suggested_purchase_lines(db, page=1, size=20)
            lines_by_item = {line.inventory_item_id: line for line in result["items"]}

            _check("listado: harina (no bajo mínimo) no aparece", harina.id in lines_by_item, False)
            _check("listado: almendras preseleccionada", lines_by_item[almendras.id].is_preselected, True)
            _check(
                "listado: almendras trae proveedor/unidad/precio",
                (
                    lines_by_item[almendras.id].supplier_id,
                    lines_by_item[almendras.id].purchase_unit_label,
                    lines_by_item[almendras.id].unit_price,
                ),
                (proveedor_a.id, "Bolsa x 500 g", Decimal("8000")),
            )
            _check("listado: agua desmarcada (sin proveedor)", lines_by_item[agua.id].is_preselected, False)
            _check(
                "listado: agua con aviso", lines_by_item[agua.id].missing_supplier_reason,
                "Sin proveedor configurado. Elige uno.",
            )
            _check(
                "listado: sin last_cost propio -> precio precargado desde el costo del insumo (10*500)",
                lines_by_item[sin_costo_relacion.id].unit_price, Decimal("5000.00"),
            )
            _check(
                "listado: sin last_cost propio -> preseleccionada igual (el precio se pudo derivar)",
                lines_by_item[sin_costo_relacion.id].is_preselected, True,
            )
            _check(
                "listado: sin ningún costo conocido -> no preseleccionada",
                lines_by_item[sin_costo_nada.id].is_preselected, False,
            )
            _check(
                "listado: sin ningún costo conocido -> aviso de precio, no de proveedor",
                lines_by_item[sin_costo_nada.id].missing_supplier_reason,
                "Sin precio de compra registrado. Ingresa uno para incluir este insumo.",
            )
            _check(
                "listado: orden por déficit descendente (almendras primero)",
                [line.inventory_item_id for line in result["items"][:2]],
                [almendras.id, agua.id],
            )

            # Déficit de almendras (490) y de agua (10) son muy superiores a los de cualquier
            # otro insumo real del tenant (deficit máximo observado ≈5) -- así que, aunque el
            # tenant tenga sus propios insumos bajo mínimo, ambos quedan en las primeras
            # posiciones del listado, lo que permite paginar size=1 de forma determinista.
            paginado = service.generate_suggested_purchase_lines(db, page=1, size=1)
            _check("listado: size=1 trae 1 fila", len(paginado["items"]), 1)
            _check("listado: página 1 (size=1) trae almendras", paginado["items"][0].inventory_item_id, almendras.id)
            pagina_2 = service.generate_suggested_purchase_lines(db, page=2, size=1)
            _check("listado: página 2 (size=1) trae agua", pagina_2["items"][0].inventory_item_id, agua.id)
            _check(
                "listado: 'total' es el mismo sin importar page/size",
                paginado["total"], pagina_2["total"],
            )

            # "Sin insumos bajo mínimo" (para ESTOS insumos de prueba, no para el tenant
            # entero -- tiene sus propios datos reales bajo mínimo ajenos a este test): subir
            # el stock de ambos por encima del mínimo y verificar que desaparecen del listado.
            almendras.current_stock = Decimal("1000")
            agua.current_stock = Decimal("1000")
            db.commit()
            result_sin_estos = service.generate_suggested_purchase_lines(db, page=1, size=100)
            ids_sin_estos = {line.inventory_item_id for line in result_sin_estos["items"]}
            _check("listado: almendras ya no aparece tras subir el stock", almendras.id in ids_sin_estos, False)
            _check("listado: agua ya no aparece tras subir el stock", agua.id in ids_sin_estos, False)

            # Volver a dejarlos bajo mínimo para la Historia 2.
            almendras.current_stock = Decimal("10")
            agua.current_stock = Decimal("90")
            db.commit()

            # ===================== Historia 2: generación =====================
            # Dos insumos de dos proveedores distintos -> dos Purchase draft.
            relacion_agua = _preferred_relation(db, proveedor_b, agua, Decimal("500"))
            relation_ids.append(relacion_agua.id)

            almendras_stock_antes = almendras.current_stock
            agua_stock_antes = agua.current_stock

            idem_key_1 = f"test-{uuid4().hex}"
            idempotency_keys.append(idem_key_1)
            purchases, was_created = service.generate_suggested_purchases(
                db, idem_key_1,
                [
                    SuggestedPurchaseLineIn(
                        inventory_item_id=almendras.id, supplier_item_id=relacion_almendras.id,
                        presentation_quantity=Decimal("2"), presentation_unit_cost=Decimal("9000"),
                    ),
                    SuggestedPurchaseLineIn(
                        inventory_item_id=agua.id, supplier_item_id=relacion_agua.id,
                        presentation_quantity=Decimal("1"), presentation_unit_cost=Decimal("500"),
                    ),
                ],
            )
            purchase_ids.extend(p.id for p in purchases)
            _check("generar: 2 proveedores -> 2 Purchase recibidas", len(purchases), 2)
            _check("generar: was_created=True (primera vez)", was_created, True)
            for p in purchases:
                _check(f"generar: Purchase {p.id} status=received", p.status, "received")
                for purchase_item in p.items:
                    _check(
                        f"generar: PurchaseItem {purchase_item.id} received_quantity == quantity",
                        purchase_item.received_quantity, purchase_item.quantity,
                    )
                    movimientos = _movements_for(db, purchase_item.inventory_item_id, p.id)
                    _check(
                        f"generar: PurchaseItem {purchase_item.id} tiene 1 InventoryMovement type=in",
                        [(m.type, m.quantity) for m in movimientos],
                        [("in", purchase_item.quantity_base)],
                    )
            _check(
                "generar: last_cost de almendras NO cambia aunque el precio generado fue distinto (9000 vs 8000)",
                db.get(SupplierItem, relacion_almendras.id).last_cost, Decimal("8000.00"),
            )
            db.refresh(almendras)
            db.refresh(agua)
            almendras_item = next(p for p in purchases if any(
                it.inventory_item_id == almendras.id for it in p.items
            ))
            almendras_purchase_item = next(it for it in almendras_item.items if it.inventory_item_id == almendras.id)
            agua_item = next(p for p in purchases if any(it.inventory_item_id == agua.id for it in p.items))
            agua_purchase_item = next(it for it in agua_item.items if it.inventory_item_id == agua.id)
            _check(
                "generar: current_stock de almendras aumentó exactamente quantity_base",
                almendras.current_stock, almendras_stock_antes + almendras_purchase_item.quantity_base,
            )
            _check(
                "generar: current_stock de agua aumentó exactamente quantity_base",
                agua.current_stock, agua_stock_antes + agua_purchase_item.quantity_base,
            )

            # ===================== Historia 2: trazabilidad en el historial =====================
            historial_almendras = inventory_router.item_movements(
                almendras.id, page=1, size=100, db=db, _=None,
            )
            movimiento_almendras = next(
                m for m in historial_almendras["items"]
                if m.reference_type == "purchase" and m.reference_id == almendras_item.id
            )
            _check(
                "historial: movimiento de la compra sugerida visible en el kardex del insumo",
                (movimiento_almendras.type, movimiento_almendras.quantity, movimiento_almendras.reference_id),
                ("in", almendras_purchase_item.quantity_base, almendras_item.id),
            )

            # Dos líneas con el mismo proveedor -> una sola Purchase con dos PurchaseItem.
            tapas = _item(db, unit, current_stock=Decimal("0"), min_stock=Decimal("10"))
            item_ids.append(tapas.id)
            relacion_tapas = _preferred_relation(db, proveedor_a, tapas, Decimal("100"))
            relation_ids.append(relacion_tapas.id)

            idem_key_2 = f"test-{uuid4().hex}"
            idempotency_keys.append(idem_key_2)
            purchases_2, _ = service.generate_suggested_purchases(
                db, idem_key_2,
                [
                    SuggestedPurchaseLineIn(
                        inventory_item_id=almendras.id, supplier_item_id=relacion_almendras.id,
                        presentation_quantity=Decimal("1"), presentation_unit_cost=Decimal("8000"),
                    ),
                    SuggestedPurchaseLineIn(
                        inventory_item_id=tapas.id, supplier_item_id=relacion_tapas.id,
                        presentation_quantity=Decimal("3"), presentation_unit_cost=Decimal("100"),
                    ),
                ],
            )
            purchase_ids.extend(p.id for p in purchases_2)
            _check("generar: mismo proveedor -> 1 sola Purchase", len(purchases_2), 1)
            _check("generar: mismo proveedor -> 2 PurchaseItem", len(purchases_2[0].items), 2)
            _check("generar: mismo proveedor -> Purchase status=received", purchases_2[0].status, "received")
            for purchase_item in purchases_2[0].items:
                movimientos = _movements_for(db, purchase_item.inventory_item_id, purchases_2[0].id)
                _check(
                    f"generar: mismo proveedor -> movimiento independiente para PurchaseItem {purchase_item.id}",
                    [(m.type, m.quantity) for m in movimientos],
                    [("in", purchase_item.quantity_base)],
                )

            # Línea inválida (cantidad < 1 vía SimpleNamespace, ya que el DTO lo rechaza
            # antes de llegar aquí) -> 422, ninguna orden creada.
            idem_key_invalida = f"test-{uuid4().hex}"
            cantidad_invalida = SimpleNamespace(
                inventory_item_id=agua.id, supplier_item_id=relacion_agua.id,
                presentation_quantity=Decimal("0"), presentation_unit_cost=Decimal("500"),
            )
            _expect_error(
                "generar: cantidad < 1 -> 422, ninguna orden creada", 422,
                lambda: service.generate_suggested_purchases(db, idem_key_invalida, [cantidad_invalida]),
            )
            _check(
                "generar: cantidad inválida no deja idempotency_key registrada",
                db.execute(
                    select(PurchaseGenerationRequest).where(
                        PurchaseGenerationRequest.idempotency_key == idem_key_invalida
                    )
                ).scalar_one_or_none(),
                None,
            )

            relacion_inactiva = SupplierItem(
                supplier_id=proveedor_b.id, inventory_item_id=agua.id,
                presentation="Caja x 12", conversion_factor=Decimal("12"), active=False,
            )
            db.add(relacion_inactiva)
            db.flush()
            relation_ids.append(relacion_inactiva.id)
            idem_key_inactiva = f"test-{uuid4().hex}"
            _expect_error(
                "generar: supplier_item inactivo -> 422, ninguna orden creada", 422,
                lambda: service.generate_suggested_purchases(
                    db, idem_key_inactiva,
                    [SuggestedPurchaseLineIn(
                        inventory_item_id=agua.id, supplier_item_id=relacion_inactiva.id,
                        presentation_quantity=Decimal("1"), presentation_unit_cost=Decimal("500"),
                    )],
                ),
            )

            idem_key_no_encontrada = f"test-{uuid4().hex}"
            _expect_error(
                "generar: supplier_item inexistente -> 422", 422,
                lambda: service.generate_suggested_purchases(
                    db, idem_key_no_encontrada,
                    [SuggestedPurchaseLineIn(
                        inventory_item_id=agua.id, supplier_item_id=uuid4(),
                        presentation_quantity=Decimal("1"), presentation_unit_cost=Decimal("500"),
                    )],
                ),
            )

            # Fallo simulado a mitad de la creación (2º grupo falla) -> rollback total,
            # ninguna orden parcial creada, sin fila de idempotencia (reintentable).
            fila_1 = _item(db, unit, current_stock=Decimal("0"), min_stock=Decimal("5"))
            item_ids.append(fila_1.id)
            rel_1 = _preferred_relation(db, proveedor_a, fila_1, Decimal("50"))
            relation_ids.append(rel_1.id)
            fila_2 = _item(db, unit, current_stock=Decimal("0"), min_stock=Decimal("5"))
            item_ids.append(fila_2.id)
            rel_2 = _preferred_relation(db, proveedor_b, fila_2, Decimal("50"))
            relation_ids.append(rel_2.id)
            db.commit()
            fila_1_stock_antes, fila_2_stock_antes = fila_1.current_stock, fila_2.current_stock

            idem_key_fallo = f"test-{uuid4().hex}"
            original_build = service._build_purchase_order
            calls = {"n": 0}

            def _build_que_falla(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise RuntimeError("fallo simulado de servidor a mitad de la creación")
                return original_build(*args, **kwargs)

            total_purchases_antes = db.execute(select(Purchase)).scalars().all()
            with patch.object(service, "_build_purchase_order", side_effect=_build_que_falla):
                try:
                    service.generate_suggested_purchases(
                        db, idem_key_fallo,
                        [
                            SuggestedPurchaseLineIn(
                                inventory_item_id=fila_1.id, supplier_item_id=rel_1.id,
                                presentation_quantity=Decimal("1"), presentation_unit_cost=Decimal("50"),
                            ),
                            SuggestedPurchaseLineIn(
                                inventory_item_id=fila_2.id, supplier_item_id=rel_2.id,
                                presentation_quantity=Decimal("1"), presentation_unit_cost=Decimal("50"),
                            ),
                        ],
                    )
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("se esperaba RuntimeError propagado tras el rollback")
            total_purchases_despues = db.execute(select(Purchase)).scalars().all()
            _check(
                "generar: fallo a mitad de la creación -> ninguna orden parcial creada",
                len(total_purchases_despues), len(total_purchases_antes),
            )
            _check(
                "generar: fallo a mitad de la creación -> sin fila de idempotencia (reintentable)",
                db.execute(
                    select(PurchaseGenerationRequest).where(
                        PurchaseGenerationRequest.idempotency_key == idem_key_fallo
                    )
                ).scalar_one_or_none(),
                None,
            )
            db.refresh(fila_1)
            db.refresh(fila_2)
            _check(
                "generar: fallo a mitad de la creación -> current_stock sin cambios",
                (fila_1.current_stock, fila_2.current_stock),
                (fila_1_stock_antes, fila_2_stock_antes),
            )
            _check(
                "generar: fallo a mitad de la creación -> sin InventoryMovement para ningún insumo del lote",
                db.execute(
                    select(InventoryMovement).where(
                        InventoryMovement.inventory_item_id.in_([fila_1.id, fila_2.id]),
                    )
                ).scalars().all(),
                [],
            )

            # Reenvío de la misma idempotency_key -> misma respuesta, sin duplicar.
            db.refresh(almendras)
            almendras_stock_antes_reenvio = almendras.current_stock
            movimientos_antes_reenvio = len(_movements_for(db, almendras.id, almendras_item.id))
            purchases_reenvio, was_created_reenvio = service.generate_suggested_purchases(
                db, idem_key_1,
                [
                    SuggestedPurchaseLineIn(
                        inventory_item_id=almendras.id, supplier_item_id=relacion_almendras.id,
                        presentation_quantity=Decimal("99"), presentation_unit_cost=Decimal("99999"),
                    ),
                ],
            )
            _check("reenvío: was_created=False", was_created_reenvio, False)
            _check(
                "reenvío: mismas Purchase que la primera vez",
                sorted(p.id for p in purchases_reenvio), sorted(p.id for p in purchases),
            )
            count_idem = db.execute(
                text(f'SELECT COUNT(*) FROM "{schema}".purchase_generation_requests WHERE idempotency_key = :k'),
                {"k": idem_key_1},
            ).scalar_one()
            _check("reenvío: COUNT(*) en purchase_generation_requests sigue siendo 1", count_idem, 1)
            db.refresh(almendras)
            _check(
                "reenvío: current_stock de almendras no vuelve a subir",
                almendras.current_stock, almendras_stock_antes_reenvio,
            )
            _check(
                "reenvío: no se insertó un segundo InventoryMovement para la orden",
                len(_movements_for(db, almendras.id, almendras_item.id)), movimientos_antes_reenvio,
            )

            # Creación manual de órdenes (fuera de esta pantalla) sigue sin cambios (FR-011):
            # nace en 'draft', sin recepción, sin movimiento ni cambio de stock.
            tapas_stock_antes = db.get(InventoryItem, tapas.id).current_stock
            orden_manual = service.create_purchase_order(
                db,
                PurchaseOrderCreate(
                    supplier_id=proveedor_a.id,
                    items=[PurchaseOrderItemIn(
                        inventory_item_id=tapas.id, supplier_item_id=relacion_tapas.id,
                        presentation_quantity=Decimal("1"), presentation_unit_cost=Decimal("100"),
                    )],
                ),
                user_id=None,
            )
            purchase_ids.append(orden_manual.id)
            _check("creación manual: Purchase nace status=draft (sin regresión, FR-011)", orden_manual.status, "draft")
            _check(
                "creación manual: PurchaseItem received_quantity=0 (sin regresión, FR-011)",
                orden_manual.items[0].received_quantity, Decimal("0"),
            )
            db.refresh(tapas)
            _check(
                "creación manual: current_stock de tapas sin cambios (sin regresión, FR-011)",
                tapas.current_stock, tapas_stock_antes,
            )
            _check(
                "creación manual: sin InventoryMovement nuevo (sin regresión, FR-011)",
                _movements_for(db, tapas.id, orden_manual.id), [],
            )

            print("TODO OK ✔")
        finally:
            db.rollback()
            _cleanup(db, schema, purchase_ids, relation_ids, item_ids, supplier_ids, idempotency_keys)


if __name__ == "__main__":
    main()
