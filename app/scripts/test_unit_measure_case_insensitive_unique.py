"""Unicidad de nombre de unidad de medida (insensible a mayúsculas); la abreviatura NO
tiene restricción de unicidad (spec 140).

No hay pytest en el proyecto, así que es un script autoejecutable:

    python -m app.scripts.test_unit_measure_case_insensitive_unique

Historial de esta misma spec (tres ajustes sucesivos del alcance de la abreviatura, por
feedback directo del usuario tras cada prueba):
1. Caso original: "BLS" y "bls" coexistían como abreviaturas "distintas" porque la única
   protección era `ensure_unique`/índice único, ambos de comparación EXACTA.
2. Primer ajuste: insensible a mayúsculas, pero único en TODO el tenant -- bloqueaba
   reutilizar una abreviatura entre grupos sin relación (ej. "BOL").
3. Segundo ajuste: único solo DENTRO del grupo -- seguía bloqueando renombrar una unidad a
   una abreviatura ya usada por OTRA unidad del mismo grupo.
4. Alcance final (este): la abreviatura puede repetirse libremente, sin ninguna
   restricción, ni dentro del mismo grupo. Solo el NOMBRE sigue siendo único en todo el
   tenant (insensible a mayúsculas, `ux_unit_measures_name_ci`).

Trabaja sobre un tenant real con datos desechables: crea sus propios grupos/unidades y
los borra al terminar.
"""
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import text

from app.core.db import with_db
from app.api.v1.unit_measure_groups import router as groups_router
from app.api.v1.unit_measure_groups.schemas import (
    UnitMeasureGroupAddUnit, UnitMeasureGroupCreate, UnitMeasureGroupUnitCreate,
)


def _schema() -> str:
    with with_db(None) as db:
        rows = db.execute(text("SELECT schema FROM shared.tenants ORDER BY id")).scalars().all()
    for schema in rows:
        with with_db(None) as db:
            exists = db.execute(
                text("SELECT to_regclass(:q)"), {"q": f"{schema}.unit_measures"}
            ).scalar() is not None
        if exists:
            return schema
    raise SystemExit("Ningún tenant tiene la tabla unit_measures todavía.")


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


def _cleanup(db, schema: str, group_ids):
    if group_ids:
        ids = [str(i) for i in group_ids]
        db.execute(text(f'DELETE FROM "{schema}".unit_measures WHERE group_id = ANY(:g)'), {"g": ids})
        db.execute(text(f'DELETE FROM "{schema}".unit_measure_groups WHERE id = ANY(:g)'), {"g": ids})
    db.commit()


def main():
    schema = _schema()
    print(f"Unicidad de unidad de medida -- nombre sí, abreviatura no (tenant: {schema})")

    with with_db(schema) as db:
        group_ids: list = []
        try:
            sufijo = uuid4().hex[:6]
            grupo = groups_router.create_unit_measure_group(
                UnitMeasureGroupCreate(
                    name=f"Grupo-ci-test-{sufijo}",
                    base_unit=UnitMeasureGroupUnitCreate(
                        name=f"Base-ci-{sufijo}", abbreviation=f"BLS{sufijo}",
                    ),
                ),
                db=db, _=None,
            )
            group_ids.append(grupo.id)
            base_name = grupo.units[0].name
            abreviatura = grupo.units[0].abbreviation  # ej. "BLS<sufijo>"

            # ===================== Nombre: sigue siendo único, insensible a mayúsculas =====================
            _expect_http_error(
                "nombre con distinta mayúscula/minúscula de uno ya existente: 409", 409,
                lambda: groups_router.add_unit_to_group(
                    grupo.id,
                    UnitMeasureGroupAddUnit(
                        name=base_name.upper() if base_name.islower() else base_name.lower(),
                        abbreviation=f"otra-{sufijo}", factor_to_base=Decimal("1"),
                    ),
                    db=db, _=None,
                ),
            )

            # ===================== Abreviatura: sin restricción =====================
            misma_abrev_mismo_grupo = groups_router.add_unit_to_group(
                grupo.id,
                UnitMeasureGroupAddUnit(
                    name=f"reusa-mismo-grupo-{sufijo}", abbreviation=abreviatura, factor_to_base=Decimal("1"),
                ),
                db=db, _=None,
            )
            _check(
                "reutilizar la misma abreviatura (exacta) DENTRO del mismo grupo: permitido",
                misma_abrev_mismo_grupo.abbreviation, abreviatura,
            )

            minuscula_mismo_grupo = groups_router.add_unit_to_group(
                grupo.id,
                UnitMeasureGroupAddUnit(
                    name=f"reusa-minuscula-{sufijo}", abbreviation=abreviatura.lower(), factor_to_base=Decimal("1"),
                ),
                db=db, _=None,
            )
            _check(
                "reutilizar la misma abreviatura (otra mayúscula/minúscula) DENTRO del mismo grupo: permitido",
                minuscula_mismo_grupo.abbreviation, abreviatura.lower(),
            )

            otro_grupo = groups_router.create_unit_measure_group(
                UnitMeasureGroupCreate(
                    name=f"Otro-grupo-ci-test-{sufijo}",
                    base_unit=UnitMeasureGroupUnitCreate(
                        name=f"Base-otro-{sufijo}", abbreviation=f"xx{sufijo}",
                    ),
                ),
                db=db, _=None,
            )
            group_ids.append(otro_grupo.id)

            reutilizada_otro_grupo = groups_router.add_unit_to_group(
                otro_grupo.id,
                UnitMeasureGroupAddUnit(
                    name=f"reusa-otro-grupo-{sufijo}", abbreviation=abreviatura, factor_to_base=Decimal("1"),
                ),
                db=db, _=None,
            )
            _check(
                "reutilizar la misma abreviatura en OTRO grupo: permitido",
                reutilizada_otro_grupo.abbreviation, abreviatura,
            )

            print("TODO OK ✔")
        finally:
            db.rollback()
            _cleanup(db, schema, group_ids)


if __name__ == "__main__":
    main()
