"""Infraestructura compartida para los tests de la spec 093 (Carta del menú para
Cajero con estado Agotado) — no es código de producción.

Combina, sobre SQLite en memoria, las tablas de catálogo que ya usa
`fixtures.py` (productos, variantes, receta, grupos de opciones) con las
tablas de `shared` que usa `auth_fixtures.py` (tenants, roles, usuarios) más
`audit_logs` (schema `tenant`, ver `app/models/audit_log.py`) — ningún test
de esta spec puede combinar `fixtures.new_session()` con `auth_fixtures.
new_session()` directamente porque cada una crea un subconjunto de tablas
distinto y, sobre todo, SQLite en memoria no comparte estado entre dos
`new_session()` aunque vengan de módulos distintos.

Reexporta `make_category`/`make_product`/`make_variant` de `fixtures.py` tal
cual (son funciones puras sobre una `Session`, no dependen de qué módulo creó
esa sesión) para no duplicar esa lógica.
"""
from __future__ import annotations

import os
import uuid

# Mismo relleno de variables inertes que `fixtures.py`/`auth_fixtures.py`.
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

from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.core.models import Base, Tenant, Role, User
import app.models  # noqa: F401  - registra todas las tablas de negocio en Base.metadata
from app.models.plan import Plan
from app.models.audit_log import AuditLog

from app.characterization_tests import fixtures as _catalog_fx

# Reexport directo: son funciones puras sobre `Session` (ver docstring).
make_category = _catalog_fx.make_category
make_product = _catalog_fx.make_product
make_variant = _catalog_fx.make_variant
make_presentation = _catalog_fx.make_presentation
link_category_presentation = _catalog_fx.link_category_presentation
make_recipe_item = _catalog_fx.make_recipe_item
make_option_group = _catalog_fx.make_option_group
make_option = _catalog_fx.make_option
link_variant_group = _catalog_fx.link_variant_group
make_inventory_item = _catalog_fx.make_inventory_item
make_unit = _catalog_fx.make_unit

_TENANT_TABLE_NAMES = [
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
    "unit_measure_groups",  # FK obligatoria de `unit_measures.group_id` (grupos de unidades)
    "presentations",
    "category_presentations",
    "audit_logs",
]


@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json_on_sqlite(element, compiler, **kw):  # pragma: no cover
    return "JSON"


def _attach_shared(conn) -> None:
    """Mismo truco que `fixtures.attach_shared_tenants`, extendido a `roles` y
    `users` (que esa función no necesita): adjunta una segunda base SQLite en
    memoria como `shared`, igual que Postgres resuelve `shared.*` en producción."""
    conn.execute(text("ATTACH DATABASE ':memory:' AS shared"))
    Plan.__table__.create(bind=conn)
    Tenant.__table__.create(bind=conn)
    Role.__table__.create(bind=conn)
    User.__table__.create(bind=conn)


def new_session() -> Session:
    """Sesión SQLAlchemy real, limpia, sobre SQLite en memoria, con las tablas de
    catálogo (`tenant`) y las de `shared` (tenants/roles/users/plans) a la vez."""
    tables = [t for t in Base.metadata.tables.values() if t.name in _TENANT_TABLE_NAMES]
    engine = create_engine("sqlite:///:memory:")
    conn = engine.connect().execution_options(schema_translate_map={"tenant": None})
    Base.metadata.create_all(bind=conn, tables=tables)
    _attach_shared(conn)
    conn.commit()
    return Session(bind=conn)


def _uid() -> uuid.UUID:
    return uuid.uuid4()


def make_tenant(db: Session, **kw) -> Tenant:
    kw.setdefault("name", f"tenant-{uuid.uuid4()}")
    kw.setdefault("schema", f"schema_{uuid.uuid4().hex[:8]}")
    kw.setdefault("host", f"host-{uuid.uuid4().hex[:8]}")
    if "plan_id" not in kw:
        plan = Plan(name=f"plan-{uuid.uuid4()}")
        db.add(plan)
        db.flush()
        kw["plan_id"] = plan.id
    obj = Tenant(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_role(db: Session, **kw) -> Role:
    kw.setdefault("id", _uid())
    kw.setdefault("name", "CASHIER")
    kw.setdefault("active", True)
    obj = Role(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_user(db: Session, tenant: Tenant | None = None, role: Role | None = None, **kw) -> User:
    if role is None:
        role = make_role(db)
    kw.setdefault("id", _uid())
    kw.setdefault("name", f"usuario-{uuid.uuid4()}")
    kw.setdefault("email", f"user-{uuid.uuid4()}@example.com")
    kw.setdefault("password_hash", "x")
    kw.setdefault("active", True)
    kw.setdefault("must_change_password", False)
    kw.setdefault("role_id", role.id)
    kw.setdefault("tenant_id", tenant.id if tenant else None)
    obj = User(**kw)
    db.add(obj)
    db.flush()
    return obj


def latest_audit_log(db: Session, *, entity: str, entity_id) -> AuditLog | None:
    """Última fila de `audit_logs` para `(entity, entity_id)` -- para verificar que
    `record_audit` quedó bien llamado, sin acoplarse al orden interno de columnas."""
    from sqlalchemy import select

    return db.execute(
        select(AuditLog)
        .where(AuditLog.entity == entity, AuditLog.entity_id == entity_id)
        .order_by(AuditLog.at.desc())
    ).scalars().first()
