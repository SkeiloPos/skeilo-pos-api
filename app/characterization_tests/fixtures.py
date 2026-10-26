"""Infraestructura compartida — no es código de producción.

Crea sesiones SQLAlchemy reales sobre SQLite en memoria, usando los
modelos ORM tal cual los define el proyecto (`app/models/*.py`), para poder
ejecutar las funciones de negocio sin mocks y sin necesitar PostgreSQL. Usa
el mismo truco de `schema_translate_map` que `app/core/db.py:with_db` usa en
producción para resolver el schema "tenant" por tenant real — aquí lo
resuelve al schema por defecto de SQLite.

No se toca ningún fichero de producción para lograr esto: es exclusivamente
infraestructura de test, y usa SQLAlchemy, que ya es una dependencia
existente del proyecto (Constitución, principio IV: cero dependencias
nuevas).
"""
from __future__ import annotations

import os
import uuid
from decimal import Decimal

# `app.core.config.Settings` exige varias variables sin default (ver
# `app/core/config.py`). En un entorno real las toma de `pos-backend/.env`;
# aquí se rellenan con valores inertes solo si no existen ya, exactamente
# como hace el precedente del propio proyecto en
# `app/scripts/test_promotions_rules.py:26-38` (el único script de test que
# hoy corre en CI). Ningún test de este paquete abre conexión de red ni de
# Redis/Postgres real: son valores de relleno para que el import no falle.
for _k, _v in {
    "DATABASE_URL": "postgresql+psycopg://x:x@localhost/x",
    "JWT_SECRET": "test",
    "REDIS_URL": "redis://localhost:6379/0",
    "EMAIL_API_URL": "https://example.invalid",
    "MAIL_FROM_NAME": "t",
    "MAIL_FROM": "t@example.invalid",
    "SUPER_ADMIN_NAME": "t",
    "SUPER_ADMIN_EMAIL": "t@example.invalid",
    "SUPER_ADMIN_PASSWORD": "t",
    "R2_ACCOUNT_ID": "x",
    "R2_ACCESS_KEY_ID": "x",
    "R2_SECRET_ACCESS_KEY": "x",
    "R2_BUCKET_NAME": "x",
    "R2_ENDPOINT_URL": "https://example.invalid",
    "R2_PUBLIC_BASE_URL": "https://example.invalid",
    "ASSETS_BASE_URL": "https://assets.example.invalid",
}.items():
    os.environ.setdefault(_k, _v)

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.core.models import Base, Tenant
import app.models  # noqa: F401  - registra todas las tablas de negocio en Base.metadata

from app.models.category import Category
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.option_group import OptionGroup
from app.models.option import Option
from app.models.variant_option_group import VariantOptionGroup
from app.models.recipe_item import RecipeItem
from app.models.inventory_item import InventoryItem
from app.models.unit_measure import UnitMeasure
from app.models.unit_measure_group import UnitMeasureGroup
from app.models.presentation import Presentation
from app.models.category_presentation import CategoryPresentation
from app.models.supplier import Supplier
from app.models.supplier_item import SupplierItem

_TABLE_NAMES = [
    "categories",
    "products",
    "product_toppings",
    "product_variants",
    "option_groups",
    "options",
    "variant_option_groups",
    "recipe_items",
    "inventory_items",
    "inventory_movements",
    "unit_measures",
    "unit_measure_groups",
    # spec 083: catálogo de presentaciones + asociación categoría<->presentación.
    "presentations",
    "category_presentations",
    # spec 088: las cuatro fuentes de referencias a archivos de R2 que consulta
    # `asset_refs.is_key_referenced` -- `products` (arriba), estas dos del esquema del
    # negocio y `shared.tenants` (ver `attach_shared_tenants`).
    "payment_methods",
    "order_payment_attempts",
    # spec 106: relación N:M proveedor-insumo, usada por el filtro de proveedor de spec 133.
    "suppliers",
    "supplier_items",
]


# `payment_methods.payment_info` es `postgresql.JSONB`: sin este shim `create_all`
# falla sobre SQLite (mismo mecanismo que `cart_fixtures.py`/`payment_catalog_fixtures.py`;
# registrarlo varias veces es inocuo).
@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json_on_sqlite(element, compiler, **kw):  # pragma: no cover
    return "JSON"


def attach_shared_tenants(conn) -> None:
    """spec 088, research D9 (opción a): `is_key_referenced` consulta
    `shared.tenants.logo_url` por nombre calificado. SQLite no tiene esquemas de
    Postgres; se simula adjuntando una segunda base en memoria como `shared` y
    creando ahí `tenants`, igual que `payment_catalog_fixtures.py` hace con
    `payment_method_catalog`. La conexión conserva `schema_translate_map={"tenant": None}`
    (solo `tenant` se remapea), así que `shared.tenants` se resuelve a la base adjunta."""
    conn.execute(text("ATTACH DATABASE ':memory:' AS shared"))
    Tenant.__table__.create(bind=conn)


def new_session() -> Session:
    """Sesión SQLAlchemy real, limpia, sobre SQLite en memoria."""
    tables = [t for t in Base.metadata.tables.values() if t.name in _TABLE_NAMES]
    engine = create_engine("sqlite:///:memory:")
    conn = engine.connect().execution_options(schema_translate_map={"tenant": None})
    Base.metadata.create_all(bind=conn, tables=tables)
    attach_shared_tenants(conn)
    conn.commit()
    return Session(bind=conn)


def _uid() -> uuid.UUID:
    return uuid.uuid4()


def make_unit_measure_group(db: Session, **kw) -> UnitMeasureGroup:
    kw.setdefault("id", _uid())
    kw.setdefault("name", f"grupo-unidad-{kw['id']}")
    obj = UnitMeasureGroup(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_unit(db: Session, group: UnitMeasureGroup | None = None, **kw) -> UnitMeasure:
    if "group_id" not in kw:
        if group is None:
            group = make_unit_measure_group(db)
        kw["group_id"] = group.id
    kw.setdefault("id", _uid())
    kw.setdefault("name", f"unidad-{kw['id']}")
    kw.setdefault("abbreviation", str(kw["id"])[:10])
    kw.setdefault("active", True)
    kw.setdefault("is_base", True)
    kw.setdefault("factor_to_base", Decimal("1"))
    obj = UnitMeasure(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_category(db: Session, **kw) -> Category:
    kw.setdefault("id", _uid())
    kw.setdefault("name", f"categoria-{kw['id']}")
    kw.setdefault("active", True)
    kw.setdefault("display_order", 0)
    obj = Category(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_product(db: Session, category: Category | None = None, **kw) -> Product:
    if category is None:
        category = make_category(db)
    kw.setdefault("id", _uid())
    kw.setdefault("category_id", category.id)
    kw.setdefault("name", f"producto-{kw['id']}")
    kw.setdefault("preparation_type", "prepared")
    kw.setdefault("active", True)
    kw.setdefault("available", True)
    obj = Product(**kw)
    db.add(obj)
    db.flush()
    return obj


def _presentation_named(db: Session, name: str) -> Presentation:
    """Get-or-create por nombre (`presentations.name` es UNIQUE): varias variantes de
    tests distintos, o de productos distintos, comparten la fila de catálogo."""
    existing = db.execute(
        select(Presentation).where(Presentation.name == name)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    obj = Presentation(id=_uid(), name=name, active=True)
    db.add(obj)
    db.flush()
    return obj


def make_variant(db: Session, product: Product | None = None, **kw) -> ProductVariant:
    if product is None:
        product = make_product(db)
    kw.setdefault("id", _uid())
    kw.setdefault("product_id", product.id)
    # spec 084 (A-79): la variante ya no tiene columna `name`; su nombre es el de su
    # presentación. `name=` sigue aceptándose aquí como atajo de los tests: se traduce a
    # la presentación del catálogo con ese nombre (se crea si no existe).
    if "presentation_id" not in kw:
        kw["presentation_id"] = _presentation_named(
            db, kw.pop("name", f"variante-{kw['id']}")
        ).id
    else:
        kw.pop("name", None)
    kw.setdefault("price", Decimal("0"))
    kw.setdefault("active", True)
    if "display_order" not in kw:
        # spec 042: sin default de ORM -- se asigna aquí igual que lo hace la app
        # real al crear una presentación (MAX(display_order) del producto + 1).
        current_max = db.execute(
            select(func.max(ProductVariant.display_order)).where(
                ProductVariant.product_id == kw["product_id"]
            )
        ).scalar()
        kw["display_order"] = (current_max or 0) + 1
    obj = ProductVariant(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_inventory_item(db: Session, unit: UnitMeasure | None = None, **kw) -> InventoryItem:
    if unit is None:
        unit = make_unit(db)
    kw.setdefault("id", _uid())
    kw.setdefault("unit_measure_id", unit.id)
    kw.setdefault("name", f"insumo-{kw['id']}")
    kw.setdefault("code", f"codigo-{kw['id']}")
    kw.setdefault("type", "raw_material")
    kw.setdefault("current_stock", Decimal("0"))
    kw.setdefault("min_stock", Decimal("0"))
    kw.setdefault("unit_cost", Decimal("0"))
    kw.setdefault("active", True)
    obj = InventoryItem(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_supplier(db: Session, **kw) -> Supplier:
    kw.setdefault("id", _uid())
    kw.setdefault("name", f"proveedor-{kw['id']}")
    kw.setdefault("active", True)
    obj = Supplier(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_supplier_item(db: Session, supplier: Supplier | None = None, inventory_item: InventoryItem | None = None, **kw) -> SupplierItem:
    if supplier is None:
        supplier = make_supplier(db)
    if inventory_item is None:
        inventory_item = make_inventory_item(db)
    kw.setdefault("id", _uid())
    kw.setdefault("supplier_id", supplier.id)
    kw.setdefault("inventory_item_id", inventory_item.id)
    kw.setdefault("presentation", f"presentacion-{kw['id']}")
    kw.setdefault("conversion_factor", Decimal("1"))
    kw.setdefault("active", True)
    kw.setdefault("preferred", False)
    obj = SupplierItem(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_option_group(db: Session, **kw) -> OptionGroup:
    kw.setdefault("id", _uid())
    kw.setdefault("name", f"grupo-{kw['id']}")
    kw.setdefault("min_select", 0)
    kw.setdefault("max_select", 1)
    kw.setdefault("active", True)
    # spec 064: "con_recargo" es el default de fábrica (server_default de la columna) --
    # se fija aquí explícitamente por el mismo motivo que el resto de este fixture fija
    # sus defaults (legibilidad del test, no depender del server_default de la DDL).
    kw.setdefault("pricing_type", "con_recargo")
    # spec 065: "conteo" es el default de fábrica (server_default) -- se fija aquí
    # explícitamente por el mismo motivo que `pricing_type` arriba.
    kw.setdefault("selection_mode", "conteo")
    obj = OptionGroup(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_shared_tenant(db: Session, **kw) -> Tenant:
    """Fila real de `shared.tenants` (spec 088): su `logo_url` es una de las cuatro
    fuentes de referencias a archivos. Requiere una sesión con `attach_shared_tenants`."""
    kw.setdefault("name", f"negocio-{uuid.uuid4()}")
    kw.setdefault("schema", f"schema_{uuid.uuid4().hex[:8]}")
    kw.setdefault("host", f"host-{uuid.uuid4().hex[:8]}")
    kw.setdefault("plan_id", _uid())  # SQLite no impone la FK a shared.plans
    obj = Tenant(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_tenant_stub(**kw):
    """Objeto `tenant` mínimo, NO persistido -- suficiente para pasar a
    `ProductService.create_product`/`update_product` (spec 064) en tests de este módulo,
    que no incluye las tablas `tenants`/`plans` en su schema de SQLite (fuera de su
    alcance). Solo sirve para tests que nunca activan `tracks_inventory=True`, que es el
    único camino que llega a leer sus atributos (`ensure_module_access`); para tests que sí
    lo activan, usar los fixtures reales de `plan_fixtures.py`."""
    from types import SimpleNamespace

    kw.setdefault("id", _uid())
    kw.setdefault("plan_id", _uid())
    kw.setdefault("plan_vence_en", None)
    # spec 088: las escrituras de imagen validan la key contra `tenant.schema`.
    kw.setdefault("schema", "heladeria3")
    return SimpleNamespace(**kw)


def make_option(db: Session, group: OptionGroup | None = None, **kw) -> Option:
    if group is None:
        group = make_option_group(db)
    kw.setdefault("id", _uid())
    kw.setdefault("option_group_id", group.id)
    kw.setdefault("name", f"opcion-{kw['id']}")
    kw.setdefault("extra_price", Decimal("0"))
    kw.setdefault("item_quantity", Decimal("0"))
    kw.setdefault("active", True)
    obj = Option(**kw)
    db.add(obj)
    db.flush()
    return obj


def link_variant_group(
    db: Session, variant: ProductVariant, group: OptionGroup, **kw
) -> VariantOptionGroup:
    kw.setdefault("id", _uid())
    kw.setdefault("product_variant_id", variant.id)
    kw.setdefault("option_group_id", group.id)
    kw.setdefault("min_select", 0)
    kw.setdefault("max_select", 1)
    kw.setdefault("quantity_per_option", Decimal("0"))
    obj = VariantOptionGroup(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_presentation(db: Session, **kw) -> Presentation:
    kw.setdefault("id", _uid())
    kw.setdefault("name", f"presentacion-{kw['id']}")
    kw.setdefault("active", True)
    obj = Presentation(**kw)
    db.add(obj)
    db.flush()
    return obj


def link_category_presentation(
    db: Session, category: Category, presentation: Presentation, **kw
) -> CategoryPresentation:
    kw.setdefault("id", _uid())
    kw.setdefault("category_id", category.id)
    kw.setdefault("presentation_id", presentation.id)
    obj = CategoryPresentation(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_recipe_item(
    db: Session, variant: ProductVariant, item: InventoryItem, **kw
) -> RecipeItem:
    kw.setdefault("id", _uid())
    kw.setdefault("product_variant_id", variant.id)
    kw.setdefault("inventory_item_id", item.id)
    kw.setdefault("quantity", Decimal("1"))
    obj = RecipeItem(**kw)
    db.add(obj)
    db.flush()
    return obj
