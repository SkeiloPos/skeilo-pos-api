"""Test de la relación proveedor-insumo (spec 106).

No hay pytest en el proyecto, así que es un script autoejecutable:

    python -m app.scripts.test_supplier_items

Cubre los Acceptance Scenarios de las 4 historias de usuario de la spec: vincular con
presentación/factor (US1), marcar preferido (US2), ver/gestionar desde el proveedor (US3),
desactivar (US4), más los edge cases y el aislamiento entre tenants.

Trabaja sobre un tenant real con datos desechables: crea su propio insumo y dos proveedores,
y los borra al terminar (no basta con un rollback final porque varias rutas hacen commit por
dentro).
"""
from decimal import Decimal
from uuid import uuid4, UUID
import threading

from sqlalchemy import select, text
from fastapi import HTTPException

from app.core.db import with_db
from app.models.inventory_item import InventoryItem
from app.models.supplier import Supplier
from app.models.supplier_item import SupplierItem
from app.models.purchase import Purchase, PurchaseItem
from app.models.unit_measure import UnitMeasure
from app.api.v1.inventory import supplier_items as svc
from app.api.v1.inventory.schemas import SupplierItemCreate, SupplierItemUpdate


def _schema() -> str:
    """Primer esquema de tenant que ya tiene las tablas base (evita tenants
    sembrados en `shared.tenants` cuyo esquema físico no llegó a crearse)."""
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
        code=f"test-{uuid4().hex[:8]}", name=f"Almendras-test-{uuid4().hex[:8]}", type="raw_material",
        unit_measure_id=unit.id, current_stock=Decimal("0"), unit_cost=Decimal("0"),
    )
    norte = Supplier(name=f"Distribuidora Norte-test-{uuid4().hex[:6]}", is_system=False)
    sur = Supplier(name=f"Agro Sur-test-{uuid4().hex[:6]}", is_system=False)
    db.add_all([item, norte, sur])
    db.flush()
    return item, norte, sur


def _system_supplier(db) -> Supplier:
    supplier = db.execute(select(Supplier).where(Supplier.is_system.is_(True))).scalar_one_or_none()
    if supplier is None:
        raise SystemExit("No se encontró el proveedor de sistema 'Compra ocasional' -- "
                          "¿se corrió la migración 99d57acd0216?")
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


def _check_concurrent_preferred_swap(schema: str, item_id, relation_a_id, relation_b_id):
    """RN-16 bajo concurrencia real (no verificable en la suite de characterization sobre
    SQLite, donde `FOR UPDATE` es un no-op): dos administradores marcan como preferido, al
    mismo tiempo, a dos proveedores distintos del mismo insumo. El `SELECT ... FOR UPDATE`
    sobre el insumo (research.md D2) debe serializarlos -- al terminar, exactamente una
    relación queda preferida, nunca cero ni dos."""
    barrier = threading.Barrier(2)
    errors = []

    def _worker(relation_id):
        try:
            barrier.wait(timeout=5)
            with with_db(schema) as db_thread:
                svc.set_preferred(db_thread, relation_id)
        except Exception as exc:  # noqa: BLE001 -- se reporta, no se traga
            errors.append(exc)

    threads = [
        threading.Thread(target=_worker, args=(relation_a_id,)),
        threading.Thread(target=_worker, args=(relation_b_id,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    if errors:
        raise AssertionError(f"swap concurrente: error en un hilo: {errors[0]!r}")

    with with_db(schema) as db_check:
        preferidas = db_check.execute(
            select(SupplierItem).where(
                SupplierItem.inventory_item_id == item_id, SupplierItem.preferred.is_(True),
            )
        ).scalars().all()
    _check("swap concurrente real (FOR UPDATE): exactamente una preferida", len(preferidas), 1)


def _cleanup(db, schema: str, relation_ids: list, purchase_ids: list, item_id, supplier_ids: list):
    ids = tuple(str(i) for i in relation_ids)
    pids = tuple(str(p) for p in purchase_ids)
    if pids:
        db.execute(text(f'DELETE FROM "{schema}".purchase_items WHERE purchase_id = ANY(:p)'),
                   {"p": list(pids)})
        db.execute(text(f'DELETE FROM "{schema}".purchases WHERE id = ANY(:p)'), {"p": list(pids)})
    if ids:
        db.execute(text(f'DELETE FROM "{schema}".supplier_items WHERE id = ANY(:i)'),
                   {"i": list(ids)})
    db.execute(text(f'DELETE FROM "{schema}".inventory_items WHERE id = :i'), {"i": str(item_id)})
    for sid in supplier_ids:
        db.execute(text(f'DELETE FROM "{schema}".suppliers WHERE id = :s'), {"s": str(sid)})
    db.commit()


def main():
    schema = _schema()
    print(f"Relación proveedor-insumo (tenant: {schema})")

    with with_db(schema) as db:
        item, norte, sur = _fixture(db)
        relation_ids: list[UUID] = []
        purchase_ids: list[UUID] = []

        try:
            # ===================== Historia 1 =====================
            rel, code = svc.create_or_reactivate(
                db, supplier_id=norte.id, inventory_item_id=item.id,
                body=SupplierItemCreate(
                    presentation="Bolsa x 500 g", conversion_factor=Decimal("500"),
                    last_cost=Decimal("8000"),
                ),
            )
            relation_ids.append(rel.id)
            _check("alta: código 201", code, 201)
            _check("alta: queda activa", rel.active, True)
            _check("alta: last_cost_at se fija", rel.last_cost_at is not None, True)
            resp = svc.to_response(db, rel)
            _check("alta: equivalencia", resp["equivalence"], "1 Bolsa x 500 g = 500 g · $16/g")
            _check(
                "alta: aparece en 'Insumos que vende' del proveedor",
                [r.id for r in svc.list_for_supplier(db, norte.id, include_inactive=False)],
                [rel.id],
            )

            rel2, code2 = svc.create_or_reactivate(
                db, supplier_id=sur.id, inventory_item_id=item.id,
                body=SupplierItemCreate(
                    presentation="Bolsa x 140 g", conversion_factor=Decimal("140"),
                ),
            )
            relation_ids.append(rel2.id)
            _check("segundo proveedor: código 201", code2, 201)
            _check(
                "segundo proveedor: dos relaciones activas en el insumo",
                len(svc.list_for_item(db, item.id, include_inactive=False)), 2,
            )
            _check(
                "segundo proveedor: no se creó un insumo nuevo",
                db.execute(
                    select(InventoryItem).where(InventoryItem.name == item.name)
                ).scalars().all().__len__(),
                1,
            )

            _expect_error(
                "factor 0 rechazado", 422,
                lambda: svc.create_or_reactivate(
                    db, supplier_id=norte.id, inventory_item_id=item.id,
                    body=SupplierItemCreate(
                        presentation="Caja x 1 kg test", conversion_factor=Decimal("0"),
                    ),
                ),
            )
            _expect_error(
                "factor negativo rechazado", 422,
                lambda: svc.create_or_reactivate(
                    db, supplier_id=norte.id, inventory_item_id=item.id,
                    body=SupplierItemCreate(
                        presentation="Caja x 1 kg test neg", conversion_factor=Decimal("-5"),
                    ),
                ),
            )

            _expect_error(
                "relación duplicada (mayúsculas/espacios distintos) rechazada", 409,
                lambda: svc.create_or_reactivate(
                    db, supplier_id=norte.id, inventory_item_id=item.id,
                    body=SupplierItemCreate(
                        presentation="bolsa x 500 g ", conversion_factor=Decimal("500"),
                    ),
                ),
            )

            svc.update(db, rel2.id, SupplierItemUpdate(active=False))
            rel2_reactivada, code3 = svc.create_or_reactivate(
                db, supplier_id=sur.id, inventory_item_id=item.id,
                body=SupplierItemCreate(presentation="Bolsa x 140 g", conversion_factor=Decimal("140")),
            )
            _check("reactivar en vez de duplicar: mismo id", rel2_reactivada.id, rel2.id)
            _check("reactivar en vez de duplicar: código 200", code3, 200)
            _check("reactivar en vez de duplicar: vuelve a estar activa", rel2_reactivada.active, True)

            purchase = Purchase(supplier_id=norte.id, status="received", total=Decimal("0"))
            db.add(purchase)
            db.flush()
            purchase_item = PurchaseItem(
                purchase_id=purchase.id, inventory_item_id=item.id,
                supplier_item_id=rel.id, quantity=Decimal("1"), unit_cost=Decimal("8000"),
            )
            db.add(purchase_item)
            db.commit()
            purchase_ids.append(purchase.id)

            _expect_error(
                "factor bloqueado tras uso en compra", 422,
                lambda: svc.update(db, rel.id, SupplierItemUpdate(conversion_factor=Decimal("501"))),
            )

            rel3, _ = svc.create_or_reactivate(
                db, supplier_id=norte.id, inventory_item_id=item.id,
                body=SupplierItemCreate(presentation="Caja x 24 bolsas", conversion_factor=Decimal("12000")),
            )
            relation_ids.append(rel3.id)
            _check(
                "mismo proveedor, dos presentaciones distintas: ambas activas",
                len([r for r in svc.list_for_supplier(db, norte.id, include_inactive=False)]),
                2,
            )

            # ===================== Historia 2 =====================
            svc.set_preferred(db, rel.id)
            _check("preferido inicial: Norte", db.get(SupplierItem, rel.id).preferred, True)
            svc.set_preferred(db, rel2.id)
            _check("swap: Norte deja de ser preferido", db.get(SupplierItem, rel.id).preferred, False)
            _check("swap: Agro Sur queda preferido", db.get(SupplierItem, rel2.id).preferred, True)
            _check(
                "swap: exactamente una preferida activa",
                db.execute(
                    select(SupplierItem).where(
                        SupplierItem.inventory_item_id == item.id, SupplierItem.preferred.is_(True),
                    )
                ).scalars().all().__len__(),
                1,
            )

            _check_concurrent_preferred_swap(schema, item.id, rel.id, rel2.id)
            db.expire_all()

            inactiva, _ = svc.create_or_reactivate(
                db, supplier_id=norte.id, inventory_item_id=item.id,
                body=SupplierItemCreate(presentation="Saco x 1000 g", conversion_factor=Decimal("1000")),
            )
            relation_ids.append(inactiva.id)
            svc.update(db, inactiva.id, SupplierItemUpdate(active=False))
            _expect_error(
                "preferir una relación inactiva rechazado", 422,
                lambda: svc.set_preferred(db, inactiva.id),
            )
            _expect_error(
                "preferred=true + active=false combinados, rechazado", 422,
                lambda: svc.update(
                    db, rel3.id, SupplierItemUpdate(active=False, preferred=True),
                ),
            )

            system_supplier = _system_supplier(db)
            _expect_error(
                "vincular al proveedor de sistema rechazado", 422,
                lambda: svc.create_or_reactivate(
                    db, supplier_id=system_supplier.id, inventory_item_id=item.id,
                    body=SupplierItemCreate(presentation="N/A", conversion_factor=Decimal("1")),
                ),
            )

            # ===================== Historia 3 =====================
            desde_proveedor = {r.id for r in svc.list_for_supplier(db, norte.id, include_inactive=False)}
            desde_insumo = {r.id for r in svc.list_for_item(db, item.id, include_inactive=False)
                            if r.supplier_id == norte.id}
            _check("ver desde el proveedor: misma relación que desde el insumo",
                   desde_proveedor, desde_insumo)
            rel_desde_proveedor, code4 = svc.create_or_reactivate(
                db, supplier_id=sur.id, inventory_item_id=item.id,
                body=SupplierItemCreate(presentation="Costal x 2000 g", conversion_factor=Decimal("2000")),
            )
            relation_ids.append(rel_desde_proveedor.id)
            _check(
                "crear desde el proveedor se ve desde el insumo",
                rel_desde_proveedor.id in [r.id for r in svc.list_for_item(db, item.id, include_inactive=False)],
                True,
            )

            # ===================== Historia 4 =====================
            # El swap concurrente de arriba deja preferida a rel o a rel2 según quién ganó
            # la carrera (ambos resultados son correctos) -- se consulta cuál es antes de
            # desactivarla, en vez de asumir cuál.
            preferida_id = db.execute(
                select(SupplierItem.id).where(
                    SupplierItem.inventory_item_id == item.id, SupplierItem.preferred.is_(True),
                )
            ).scalar_one()
            _, was_preferred = svc.update(db, preferida_id, SupplierItemUpdate(active=False))
            _check("desactivar la preferida: queda inactiva", db.get(SupplierItem, preferida_id).active, False)
            _check("desactivar la preferida: ya no es preferida", db.get(SupplierItem, preferida_id).preferred, False)
            _check("desactivar la preferida: was_preferred=true", was_preferred, True)

            _, was_preferred2 = svc.update(db, rel3.id, SupplierItemUpdate(active=False))
            _check("desactivar una no-preferida: was_preferred=false", was_preferred2, False)

            # ===================== Polish: aislamiento de tenant (FR-015) =====================
            # No hay un segundo tenant con su esquema completo ya provisionado en esta base
            # (ver nota de memoria "no local Postgres" -- ahora sí hay Postgres, pero solo un
            # tenant con tablas base). Se simula un segundo tenant con una tabla
            # `supplier_items` propia y vacía: el mecanismo de aislamiento real es el
            # `schema_translate_map` de `with_db`, no un filtro manual por tenant_id, así que
            # basta con que la tabla exista en otro esquema para probarlo de verdad.
            tmp_schema = f"pos106_isolation_test_{uuid4().hex[:8]}"
            with with_db(None) as db_global:
                db_global.execute(text(f'CREATE SCHEMA "{tmp_schema}"'))
                db_global.execute(
                    text(f'CREATE TABLE "{tmp_schema}".supplier_items '
                         f'(LIKE "{schema}".supplier_items)')
                )
                db_global.commit()
            try:
                with with_db(tmp_schema) as db_otro:
                    _expect_error(
                        "aislamiento: editar relación de otro tenant responde 404", 404,
                        lambda: svc.update(db_otro, rel.id, SupplierItemUpdate()),
                    )
            finally:
                with with_db(None) as db_global:
                    db_global.execute(text(f'DROP SCHEMA "{tmp_schema}" CASCADE'))
                    db_global.commit()

            print("TODO OK ✔")
        finally:
            db.rollback()
            _cleanup(db, schema, relation_ids, purchase_ids, item.id, [norte.id, sur.id])


if __name__ == "__main__":
    main()
