"""Infraestructura compartida para los tests de planes de suscripción (spec
033) — no es código de producción. Mismo patrón que `auth_fixtures.py`:
colapsa tanto `tenant` como `shared` a `None` sobre SQLite en memoria, pero
agrega además las tablas de negocio que gobiernan los cinco recursos
limitados (mesas/cajas/usuarios/productos/métodos de pago) para poder
ejercitar `enforce_plan_limit`/`require_module_access`/`ensure_plan_not_expired`
de punta a punta sin PostgreSQL.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

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

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.core.models import Base, Role, Tenant, User
from app.core.plan_limits import calculate_plan_vencimiento
from app.core.timezone import utc_now
from app.core.utils import generate_passwd_hash
from app.models.cash_register import CashRegister
from app.models.dining_table import DiningTable
from app.models.payment import PaymentMethod
from app.models.plan import Plan
from app.models.product import Product

_TABLE_NAMES = [
    "tenants",
    "plans",
    "roles",
    "users",
    "user_invitations",
    "dining_tables",
    "cash_registers",
    "products",
    "product_toppings",
    "payment_methods",
]


# PaymentMethod.payment_info es postgresql.JSONB — sin este shim create_all()
# falla con UnsupportedCompilationError sobre SQLite (mismo mecanismo que
# payment_catalog_fixtures.py). El registro de @compiles es global por
# proceso, pero redeclararlo aquí hace este módulo autosuficiente sin
# depender del orden de import de otros ficheros de test.
@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json_on_sqlite(element, compiler, **kw):  # pragma: no cover
    return "JSON"


def new_session() -> Session:
    """Sesión SQLAlchemy real, limpia, sobre SQLite en memoria — colapsa
    tanto `tenant` como `shared` al schema por defecto de SQLite."""
    tables = [t for t in Base.metadata.tables.values() if t.name in _TABLE_NAMES]
    engine = create_engine("sqlite:///:memory:")
    conn = engine.connect().execution_options(
        schema_translate_map={"tenant": None, "shared": None}
    )
    Base.metadata.create_all(bind=conn, tables=tables)
    conn.commit()
    return Session(bind=conn)


_LIMIT_KEYS = (
    "mesas_limit", "cajas_limit", "usuarios_limit", "productos_limit",
    "metodos_pago_activos_limit",
)


def make_plan(db: Session, **kw) -> Plan:
    """Por defecto, un plan sin ninguna característica configurada — todo
    bloqueado (FR-002): límites en 0, accesos en `false`, sin precios. Pasar
    `mesas_limit=None` explícitamente para "ilimitado", o los accesos que se
    necesiten en `true`.

    Las columnas de límite no tienen default a nivel de ORM/DB a propósito
    (ver app/models/plan.py: un default ahí haría que SQLAlchemy omita del
    INSERT cualquier `None` explícito, el sentinel de "ilimitado") — el "0
    si no se configura" de FR-002 lo aplica `PlanCreate` en la API real, y
    esta fixture lo replica aquí con `setdefault` para que un fixture sin
    parámetros produzca el mismo plan "todo bloqueado" que crearía la API
    real omitiendo esas mismas claves."""
    kw.setdefault("name", f"plan-{uuid.uuid4()}")
    for key in _LIMIT_KEYS:
        kw.setdefault(key, 0)
    plan = Plan(**kw)
    db.add(plan)
    db.flush()
    return plan


def make_tenant(
    db: Session,
    plan: Optional[Plan] = None,
    ciclo_facturacion: Optional[str] = None,
    plan_iniciado_en: Optional[datetime] = None,
    **kw,
) -> Tenant:
    """`plan` por defecto: un plan recién creado sin restricciones
    explícitas (todo bloqueado) salvo que el llamador pase uno propio. Sin
    `ciclo_facturacion`, el tenant nunca vence (FR-021) — pasar
    `ciclo_facturacion="mensual"/"anual"` (y opcionalmente
    `plan_iniciado_en` para simular un vencimiento ya pasado, ej. para los
    tests de la Historia de Usuario 5)."""
    if plan is None:
        plan = make_plan(db)
    kw.setdefault("name", f"tenant-{uuid.uuid4()}")
    kw.setdefault("schema", f"schema_{uuid.uuid4().hex[:8]}")
    kw.setdefault("host", f"host-{uuid.uuid4().hex[:8]}")

    resolved_inicio = None
    resolved_vence = None
    if ciclo_facturacion is not None:
        resolved_inicio = plan_iniciado_en or utc_now().replace(tzinfo=None)
        resolved_vence = calculate_plan_vencimiento(resolved_inicio, ciclo_facturacion)

    tenant = Tenant(
        plan_id=plan.id,
        ciclo_facturacion=ciclo_facturacion,
        plan_iniciado_en=resolved_inicio,
        plan_vence_en=resolved_vence,
        **kw,
    )
    db.add(tenant)
    db.flush()
    return tenant


def make_role(db: Session, name: str = "ADMIN") -> Role:
    role = Role(name=name, active=True)
    db.add(role)
    db.flush()
    return role


def make_user(db: Session, tenant: Tenant, role: Optional[Role] = None, **kw) -> User:
    if role is None:
        role = make_role(db, kw.pop("role_name", "ADMIN"))
    kw.setdefault("name", f"user-{uuid.uuid4()}")
    kw.setdefault("email", f"user-{uuid.uuid4()}@example.invalid")
    kw.setdefault("password_hash", generate_passwd_hash("x" * 10))
    kw.setdefault("active", True)
    user = User(tenant_id=tenant.id, role_id=role.id, **kw)
    db.add(user)
    db.flush()
    return user


def make_dining_table(db: Session, **kw) -> DiningTable:
    kw.setdefault("number", uuid.uuid4().int % 100000)
    table = DiningTable(**kw)
    db.add(table)
    db.flush()
    return table


def make_cash_register(db: Session, **kw) -> CashRegister:
    kw.setdefault("name", f"caja-{uuid.uuid4()}")
    register = CashRegister(**kw)
    db.add(register)
    db.flush()
    return register


def make_product(db: Session, **kw) -> Product:
    kw.setdefault("category_id", uuid.uuid4())
    kw.setdefault("name", f"producto-{uuid.uuid4()}")
    product = Product(**kw)
    db.add(product)
    db.flush()
    return product


def make_payment_method(db: Session, **kw) -> PaymentMethod:
    kw.setdefault("name", f"metodo-{uuid.uuid4()}")
    kw.setdefault("active", True)
    method = PaymentMethod(**kw)
    db.add(method)
    db.flush()
    return method
