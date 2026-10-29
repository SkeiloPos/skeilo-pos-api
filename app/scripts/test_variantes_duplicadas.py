"""Test del contrato de nombres duplicados en las variantes de un producto.

No hay pytest en el proyecto, así que es un script autoejecutable:

    python -m app.scripts.test_variantes_duplicadas

Cubre el defecto que lo motivó: `DELETE /variants/{id}` es un **soft-delete**, así que la
fila desactivada sigue ocupando el nombre en `uq__product_variants__product_id__name`. El
editor del frontend no lista las desactivadas, el dueño vuelve a agregar «Pequeña», y el
`POST` llegaba hasta el `commit()` y salía como **500** (`UniqueViolation`) — un error que
el frontend no puede manejar y que no dice qué hacer.

Reglas verificadas:
  - nombre repetido de una variante activa      → 409 con `active: true`;
  - nombre repetido de una **desactivada**      → 409 con `active: false` + `variant_id`,
    que es la vía para ofrecer «reactivar» en vez de crear otra;
  - « pequeña » (espacios y mayúsculas)         → 409, no una segunda fila casi idéntica;
  - renombrar una variante al nombre de otra    → 409, no 500;
  - reactivar con `PATCH {active: true}`        → vuelve con su receta intacta;
  - `list_variants(active=True)`                → sin las desactivadas; sin filtro, todas.

Trabaja sobre un tenant real con datos desechables: crea su propio insumo, producto y
variantes, y los borra al terminar.
"""
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text

from app.core.db import with_db
from app.core.models import User
from app.models.category import Category
from app.models.inventory_item import InventoryItem
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.recipe_item import RecipeItem
from app.models.unit_measure import UnitMeasure
from app.api.v1.catalog.router import (
    create_variant,
    delete_variant,
    list_variants,
    update_variant,
)
from app.api.v1.catalog.schemas import VariantCreate, VariantUpdate


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


def _expect_409(fn, label) -> HTTPException:
    try:
        fn()
    except HTTPException as e:
        if e.status_code != 409:
            raise AssertionError(f"{label}: esperado 409, vino {e.status_code}")
        print(f"  ok  · {label}")
        return e
    raise AssertionError(f"{label}: no lanzó 409")


def _detalle(e: HTTPException) -> dict:
    if not isinstance(e.detail, dict):
        raise AssertionError(
            f"el 409 trae texto plano ({e.detail!r}); el frontend necesita "
            "`variant_id` y `active` para ofrecer reactivar sin parsear el mensaje"
        )
    return e.detail


def _staff(db) -> User:
    user = db.execute(select(User).limit(1)).scalar_one_or_none()
    if user is None:
        raise SystemExit("No hay usuarios en shared.users.")
    return user


def _fixture(db):
    """Un producto vacío (sin variantes) y un insumo para darle receta a una de ellas."""
    unit = db.execute(select(UnitMeasure).limit(1)).scalar_one_or_none()
    if unit is None:
        unit = UnitMeasure(name=f"ud-{uuid4().hex[:6]}", abbreviation=uuid4().hex[:4])
        db.add(unit); db.flush()

    insumo = InventoryItem(
        code=f"test-{uuid4().hex[:8]}", name=f"insumo-dup-{uuid4().hex[:8]}", type="raw_material",
        unit_measure_id=unit.id, current_stock=Decimal("100.000"),
        unit_cost=Decimal("1.00"),
    )
    db.add(insumo); db.flush()

    cat = db.execute(select(Category).limit(1)).scalar_one_or_none()
    if cat is None:
        cat = Category(name=f"cat-{uuid4().hex[:6]}"); db.add(cat); db.flush()

    product = Product(name=f"prod-dup-{uuid4().hex[:8]}", code=f"cod-dup-{uuid4().hex[:8]}", category_id=cat.id,
                      preparation_type="prepared")
    db.add(product); db.flush()
    return product.id, insumo.id


def _cleanup(schema, product_id, insumo_id):
    with with_db(schema) as db:
        db.execute(text(f'''DELETE FROM "{schema}".recipe_items WHERE product_variant_id IN (
                SELECT id FROM "{schema}".product_variants WHERE product_id = :p)'''),
                   {"p": str(product_id)})
        db.execute(text(f'DELETE FROM "{schema}".product_variants WHERE product_id = :p'),
                   {"p": str(product_id)})
        db.execute(text(f'DELETE FROM "{schema}".products WHERE id = :p'),
                   {"p": str(product_id)})
        db.execute(text(f'DELETE FROM "{schema}".inventory_items WHERE id = :i'),
                   {"i": str(insumo_id)})
        db.commit()


def main():
    schema = _schema()
    print(f"Nombres duplicados de variantes: 409 accionable, nunca 500 (tenant: {schema})")

    with with_db(schema) as db:
        product_id, insumo_id = _fixture(db)
        db.commit()

    try:
        # --- 1. crear las dos presentaciones ---------------------------------
        with with_db(schema) as db:
            user = _staff(db)
            pequena = create_variant(
                product_id, VariantCreate(name="Pequeña", price=Decimal("6.00")), db, user
            )
            mediana = create_variant(
                product_id, VariantCreate(name="Mediana", price=Decimal("9.00")), db, user
            )
            _check("se crea «Pequeña»", pequena.name, "Pequeña")
            _check("y «Mediana»", mediana.name, "Mediana")
            pequena_id, mediana_id = pequena.id, mediana.id
            # Receta, para comprobar más abajo que reactivar no la pierde.
            db.add(RecipeItem(product_variant_id=pequena_id,
                              inventory_item_id=insumo_id, quantity=Decimal("2.000")))
            db.commit()

        # --- 2. repetir el nombre de una ACTIVA → 409 ------------------------
        with with_db(schema) as db:
            user = _staff(db)
            e = _expect_409(
                lambda: create_variant(
                    product_id, VariantCreate(name="Pequeña", price=Decimal("7.00")), db, user
                ),
                "nombre repetido de una variante activa → 409",
            )
            d = _detalle(e)
            _check("señala la variante que estorba", d["variant_id"], str(pequena_id))
            _check("y dice que está activa", d["active"], True)

        # --- 3. EL BUG: recrear una DESACTIVADA → 409, no 500 -----------------
        with with_db(schema) as db:
            delete_variant(pequena_id, db, _staff(db))
        with with_db(schema) as db:
            _check("el soft-delete la deja inactiva",
                   db.get(ProductVariant, pequena_id).active, False)
            user = _staff(db)
            e = _expect_409(
                lambda: create_variant(
                    product_id, VariantCreate(name="Pequeña", price=Decimal("7.00")), db, user
                ),
                "recrear una desactivada → 409 (antes: 500 UniqueViolation)",
            )
            d = _detalle(e)
            _check("con el id de la desactivada", d["variant_id"], str(pequena_id))
            _check("y `active: false` para ofrecer reactivarla", d["active"], False)

        # --- 4. espacios y mayúsculas no crean una casi-gemela ---------------
        with with_db(schema) as db:
            user = _staff(db)
            _expect_409(
                lambda: create_variant(
                    product_id, VariantCreate(name="  pequeña  ", price=Decimal("7.00")), db, user
                ),
                "«  pequeña  » es la misma presentación → 409",
            )
            _check("no se coló una fila extra",
                   len(list_variants(product_id, None, db, user)), 2)

        # --- 5. renombrar al nombre de otra → 409, no 500 --------------------
        with with_db(schema) as db:
            user = _staff(db)
            _expect_409(
                lambda: update_variant(mediana_id, VariantUpdate(name="Pequeña"), db, user),
                "renombrar «Mediana» → «Pequeña» → 409",
            )
        with with_db(schema) as db:
            _check("y «Mediana» conserva su nombre",
                   db.get(ProductVariant, mediana_id).name, "Mediana")

        # --- 6. la vía de recuperación: reactivar en una sola llamada --------
        with with_db(schema) as db:
            user = _staff(db)
            revivida = update_variant(
                pequena_id, VariantUpdate(active=True, price=Decimal("7.00")), db, user
            )
            _check("PATCH {active: true} la devuelve a la carta", revivida.active, True)
            _check("con el precio nuevo", Decimal(revivida.price), Decimal("7.00"))
            receta = db.execute(
                select(RecipeItem).where(RecipeItem.product_variant_id == pequena_id)
            ).scalars().all()
            _check("y su receta intacta (por eso no se borra la fila)", len(receta), 1)

        # --- 7. el filtro de estado en el listado ---------------------------
        with with_db(schema) as db:
            user = _staff(db)
            delete_variant(mediana_id, db, user)
            _check("sin filtro salen todas",
                   len(list_variants(product_id, None, db, user)), 2)
            _check("?active=true deja fuera las desactivadas",
                   [v.name for v in list_variants(product_id, True, db, user)], ["Pequeña"])
            _check("?active=false devuelve solo las desactivadas",
                   [v.name for v in list_variants(product_id, False, db, user)], ["Mediana"])

        print("\n✔ El nombre duplicado responde 409 con el id y el estado de la variante "
              "que lo ocupa; recrear una desactivada ya no revienta en 500.")
        return 0
    finally:
        _cleanup(schema, product_id, insumo_id)
        print("  (datos de prueba eliminados)")


if __name__ == "__main__":
    raise SystemExit(main())
