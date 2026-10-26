"""Test de grupos de unidades de medida (spec 137).

No hay pytest en el proyecto (ver app/scripts/test_purchase_unit_conversion.py), así
que es un script autoejecutable:

    python -m app.scripts.test_unit_measure_groups

Cubre los Acceptance Scenarios de User Story 2: crear grupo con unidad base (FR-005),
agregar unidad adicional (FR-006), exactamente una base por grupo (FR-007), bloqueos de
"unidad base"/"en uso" (FR-008/FR-009/FR-011/FR-014), reasignación de base recalculando
factor_to_base (FR-009/D6) y eliminación de grupo (FR-011).

Trabaja sobre un tenant real con datos desechables: crea sus propios grupos/unidades/
insumos y los borra al terminar.
"""
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import text

from app.core.db import with_db
from app.models.inventory_item import InventoryItem
from app.models.unit_measure import UnitMeasure
from app.models.unit_measure_group import UnitMeasureGroup
from app.api.v1.unit_measure_groups import router as groups_router
from app.api.v1.unit_measure_groups.schemas import (
    UnitMeasureGroupCreate, UnitMeasureGroupUnitCreate, UnitMeasureGroupAddUnit,
    UnitMeasureGroupRename, UnitMeasureGroupBaseReassign,
)
from app.api.v1.unit_measures import router as units_router
from app.api.v1.unit_measures.schemas import UnitMeasureUpdate


def _schema() -> str:
    with with_db(None) as db:
        rows = db.execute(text("SELECT schema FROM shared.tenants ORDER BY id")).scalars().all()
    for schema in rows:
        with with_db(None) as db:
            exists = db.execute(
                text("SELECT to_regclass(:q)"), {"q": f"{schema}.unit_measure_groups"}
            ).scalar() is not None
        if exists:
            return schema
    raise SystemExit("Ningún tenant tiene la tabla unit_measure_groups todavía.")


def _check(label, actual, esperado):
    if actual != esperado:
        raise AssertionError(f"{label}: esperado {esperado!r}, obtenido {actual!r}")
    print(f"  ok  · {label}")


def _expect_http_error(label, status_code, fn):
    try:
        fn()
    except HTTPException as exc:
        _check(label, exc.status_code, status_code)
    else:
        raise AssertionError(f"{label}: se esperaba HTTPException {status_code}, no se lanzó nada")


def _cleanup(db, schema: str, group_ids, item_ids):
    for item_id in item_ids:
        db.execute(text(f'DELETE FROM "{schema}".inventory_items WHERE id = :i'), {"i": str(item_id)})
    if group_ids:
        ids = [str(i) for i in group_ids]
        db.execute(text(f'DELETE FROM "{schema}".unit_measure_factor_changes WHERE unit_measure_id IN '
                        f'(SELECT id FROM "{schema}".unit_measures WHERE group_id = ANY(:g))'), {"g": ids})
        db.execute(text(f'DELETE FROM "{schema}".unit_measures WHERE group_id = ANY(:g)'), {"g": ids})
        db.execute(text(f'DELETE FROM "{schema}".unit_measure_groups WHERE id = ANY(:g)'), {"g": ids})
    db.commit()


def main():
    schema = _schema()
    print(f"Grupos de unidades de medida (tenant: {schema})")
    admin = type("U", (), {"id": uuid4(), "role": type("R", (), {"name": "ADMIN"})()})()

    with with_db(schema) as db:
        group_ids: list = []
        item_ids: list = []
        try:
            sufijo = uuid4().hex[:6]

            # ===================== FR-005: crear grupo + unidad base =====================
            grupo = groups_router.create_unit_measure_group(
                UnitMeasureGroupCreate(
                    name=f"Volumen-test-{sufijo}",
                    base_unit=UnitMeasureGroupUnitCreate(name=f"Mililitro-test-{sufijo}", abbreviation=f"ml{sufijo}"),
                ),
                db=db, _=None,
            )
            group_ids.append(grupo.id)
            _check("crear grupo: 1 unidad (la base)", len(grupo.units), 1)
            _check("crear grupo: unidad base factor=1", grupo.units[0].factor_to_base, Decimal("1"))
            _check("crear grupo: unidad base is_base", grupo.units[0].is_base, True)
            ml = grupo.units[0]

            # Nombre de grupo duplicado -> 409
            _expect_http_error(
                "nombre de grupo duplicado: 409", 409,
                lambda: groups_router.create_unit_measure_group(
                    UnitMeasureGroupCreate(
                        name=grupo.name,
                        base_unit=UnitMeasureGroupUnitCreate(name=f"Otra-{sufijo}", abbreviation=f"ot{sufijo}"),
                    ),
                    db=db, _=None,
                ),
            )

            # ===================== FR-006: agregar unidad adicional =====================
            litro = groups_router.add_unit_to_group(
                grupo.id,
                UnitMeasureGroupAddUnit(name=f"Litro-test-{sufijo}", abbreviation=f"L{sufijo}", factor_to_base=Decimal("1000")),
                db=db, _=None,
            )
            _check("agregar unidad: no es base", litro.is_base, False)
            _check("agregar unidad: factor_to_base", litro.factor_to_base, Decimal("1000"))

            # Nombre/abreviatura de unidad duplicado en el tenant -> 409
            _expect_http_error(
                "nombre de unidad duplicado en el tenant: 409", 409,
                lambda: groups_router.add_unit_to_group(
                    grupo.id,
                    UnitMeasureGroupAddUnit(name=litro.name, abbreviation=f"xx{sufijo}", factor_to_base=Decimal("1")),
                    db=db, _=None,
                ),
            )

            # ===================== FR-007: exactamente una base por grupo =====================
            grupo_reloaded = db.get(UnitMeasureGroup, grupo.id)
            bases = [u for u in grupo_reloaded.units if u.is_base]
            _check("exactamente 1 base en el grupo", len(bases), 1)

            # ===================== FR-008: la base no se edita/desactiva =====================
            _expect_http_error(
                "PATCH factor de la unidad base: 409", 409,
                lambda: units_router.update_unit_measure(
                    id=ml.id, body=UnitMeasureUpdate(factor_to_base=Decimal("2")), db=db, user=admin,
                ),
            )
            _expect_http_error(
                "PATCH active=False de la unidad base: 409", 409,
                lambda: units_router.update_unit_measure(
                    id=ml.id, body=UnitMeasureUpdate(active=False), db=db, user=admin,
                ),
            )
            _expect_http_error(
                "DELETE (soft) de la unidad base: 409", 409,
                lambda: units_router.delete_unit_measure(id=ml.id, db=db, _=None),
            )

            # ===================== FR-009/D6: reasignar base sin uso =====================
            groups_router.reassign_base_unit(
                grupo.id, UnitMeasureGroupBaseReassign(unit_measure_id=litro.id), db=db, _=None,
            )
            ml_reloaded = db.get(UnitMeasure, ml.id)
            litro_reloaded = db.get(UnitMeasure, litro.id)
            _check("tras reasignar: litro es la nueva base", litro_reloaded.is_base, True)
            _check("tras reasignar: litro factor=1", litro_reloaded.factor_to_base, Decimal("1"))
            _check("tras reasignar: ml ya no es base", ml_reloaded.is_base, False)
            _check(
                "tras reasignar: factor de ml recalculado relativo a litro (1/1000)",
                ml_reloaded.factor_to_base, Decimal("1") / Decimal("1000"),
            )

            # Devolver la base a ml para el resto del escenario.
            groups_router.reassign_base_unit(
                grupo.id, UnitMeasureGroupBaseReassign(unit_measure_id=ml.id), db=db, _=None,
            )
            litro_reloaded = db.get(UnitMeasure, litro.id)
            _check("tras volver a reasignar: factor de litro vuelve a 1000", litro_reloaded.factor_to_base, Decimal("1000"))

            # ===================== FR-014: renombrar unidad en uso bloqueado =====================
            insumo = InventoryItem(
                code=f"test-{uuid4().hex[:8]}", name=f"Insumo-test-{uuid4().hex[:8]}", type="raw_material",
                unit_measure_id=litro.id, current_stock=Decimal("0"), unit_cost=Decimal("0"),
            )
            db.add(insumo)
            db.commit()
            item_ids.append(insumo.id)

            _expect_http_error(
                "renombrar unidad en uso: 409", 409,
                lambda: units_router.update_unit_measure(
                    id=litro.id, body=UnitMeasureUpdate(name=f"Litro-renombrado-{sufijo}"), db=db, user=admin,
                ),
            )

            # ===================== FR-009: reasignar base con unidad en uso bloqueado =====================
            _expect_http_error(
                "reasignar base con unidad del grupo en uso: 409", 409,
                lambda: groups_router.reassign_base_unit(
                    grupo.id, UnitMeasureGroupBaseReassign(unit_measure_id=litro.id), db=db, _=None,
                ),
            )

            # ===================== FR-011: eliminar grupo con unidad en uso bloqueado =====================
            _expect_http_error(
                "eliminar grupo con unidad en uso: 409", 409,
                lambda: groups_router.delete_unit_measure_group(grupo.id, db=db, _=None),
            )

            # Liberar el insumo y confirmar que ahora sí se puede eliminar el grupo.
            db.delete(db.get(InventoryItem, insumo.id))
            db.commit()
            item_ids.remove(insumo.id)

            groups_router.delete_unit_measure_group(grupo.id, db=db, _=None)
            _check("grupo eliminado: ya no existe", db.get(UnitMeasureGroup, grupo.id), None)
            group_ids.remove(grupo.id)

            print("TODO OK ✔")
        finally:
            db.rollback()
            _cleanup(db, schema, group_ids, item_ids)


if __name__ == "__main__":
    main()
