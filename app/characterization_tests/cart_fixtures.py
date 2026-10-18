"""Infraestructura compartida para los characterization tests de `cart`
(`test_cart_service.py`, `test_cart_router.py`) — no es código de producción.

Amplía el motor SQLite-en-memoria de `fixtures.py` (que no se toca, ver
`contracts/test-harness-api.md` de `specs/015-caracterizacion-cart`) con las
14 tablas de mesas/carrito/pedidos/promociones que `cart/service.py` y
`cart/router.py` necesitan, sus factories, un reloj fijado para el caso A-08
y el harness que sustituye `Depends`/los context managers de
`app.core.qr_context` para poder invocar los endpoints de `router.py` como
funciones Python directas, sin Postgres ni Redis reales (research.md §1-2).
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from itertools import count
from types import SimpleNamespace
from typing import Iterator
from unittest import mock
import uuid

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.schema import DefaultClause

from app.characterization_tests.fixtures import (
    attach_shared_tenants,
    make_category,
    make_inventory_item,
    make_option,
    make_option_group,
    make_product,
    make_recipe_item,
    make_unit,
    make_variant,
    link_variant_group,
)

# Reexport tal cual (contracts/test-harness-api.md §Sesión y base de datos):
# el propio import de arriba ya los trae al namespace de este módulo.
__all__ = [
    "make_category", "make_inventory_item", "make_option", "make_option_group",
    "make_product", "make_recipe_item", "make_unit", "make_variant",
    "link_variant_group",
    "new_session",
    "make_dining_table", "make_table_session", "make_participant",
    "make_cart", "make_cart_item", "make_customer_order",
    "make_promotion", "add_rule_to_promotion",
    "make_payment_method", "make_payment_attempt",
    "frozen_now",
    "build_session_context", "patched_qr_context", "patched_session_context",
    "FakeRedisBucket",
]

from app.core.models import Base
import app.models  # noqa: F401 - ya registrado vía fixtures.py; se repite por claridad
from app.core.qr_context import QrContext, SessionContext

from app.models.dining_table import DiningTable
from app.models.table_session import TableSession
from app.models.session_participant import SessionParticipant
from app.models.cart import Cart
from app.models.cart_item import CartItem
from app.models.customer_order import CustomerOrder
from app.models.product_variant import ProductVariant
from app.models.promotion import Promotion, PromotionRule, PromotionVariant
from app.models.payment import PaymentMethod
from app.models.order_payment_attempt import OrderPaymentAttempt
from app.models.cash_register import CashRegister
from app.models.cash_shift import CashShift


# --------------------------------------------------------------- Esquema SQLite

_CATALOG_TABLE_NAMES = [
    "categories",
    "products",
    "product_variants",
    "presentations",  # spec 084 (A-79): la variante depende de su presentación
    "option_groups",
    "options",
    "variant_option_groups",
    "recipe_items",
    "inventory_items",
    "inventory_movements",
    "unit_measures",
]

_CART_TABLE_NAMES = [
    "dining_tables",
    "table_sessions",
    "session_participants",
    "carts",
    "cart_items",
    "cart_item_options",
    "customer_orders",
    "order_items",
    "order_item_options",
    "promotions",
    # spec 063 (revisión 2026-09-01): una promoción agrupa una o más reglas.
    "promotion_rules",
    # spec 063: conjunto explícito de variantes elegibles (de una regla).
    "promotion_variants",
    "order_cancel_logs",
    "audit_logs",
    "payment_methods",
    "order_payment_attempts",
    # spec 028: `has_billable_orders` (vía `try_release_if_empty`, que este
    # módulo ejercita a través de `leave_session`) ahora también consulta
    # `sales.customer_order_id` — sin esta tabla, esa consulta revienta con
    # "no such table: sales" aunque el escenario probado no cree ninguna
    # venta.
    "sales",
    # spec 087 (FR-006, A-86): `submit_cart` consulta `cash_shifts` para
    # numerar el pedido DINE_IN (`resolve_dine_in_table_order`) — sin estas
    # dos tablas, esa consulta revienta con "no such table: cash_shifts"
    # aunque el escenario probado no abra ningún turno.
    "cash_registers",
    "cash_shifts",
]

_TABLE_NAMES = _CATALOG_TABLE_NAMES + _CART_TABLE_NAMES


# `audit_logs.payload` es `postgresql.JSONB` (`app/models/audit_log.py`). La
# versión de SQLAlchemy de este entorno NO compila `JSONB` a un tipo genérico
# sobre SQLite por sí sola (a diferencia de lo asumido en research.md §4: ahí
# se documentó como comportamiento esperado y se verificó aquí, en el test de
# humo, T008) — sin este shim `create_all` falla con `UnsupportedCompilationError`
# antes de crear una sola tabla. Es un registro de compilador de SQLAlchemy
# (`sqlalchemy.ext.compiler.compiles`), no una migración de modelo: no toca
# `app/models/audit_log.py` ni cambia el tipo en Postgres.
@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json_on_sqlite(element, compiler, **kw):  # pragma: no cover
    return "JSON"

# specs/022-correccion-zona-horaria-menu-carrito: `menu/router._option_availability`
# usa `func.bool_or(...)` (agregado de Postgres) para saber si alguna variante de un
# grupo de opciones no exige cantidad. SQLite no trae `bool_or` — sin registrarlo
# como función agregada sobre la conexión, cualquier SELECT que lo mencione falla con
# `OperationalError: no such function: bool_or`, incluso con la tabla vacía (falla en
# la compilación/ejecución del SQL, no en el conteo de filas). No es una migración de
# modelo ni toca producción: es un shim de test equivalente al de JSONB de arriba.
class _BoolOr:
    def __init__(self) -> None:
        self._result = False

    def step(self, value) -> None:
        if value:
            self._result = True

    def finalize(self) -> bool:
        return self._result

# research.md §3: `postgresql_where` es un kwarg de dialecto que SQLAlchemy solo
# interpreta al compilar DDL para Postgres; sobre SQLite el índice se crea sin el
# predicado parcial, es decir, como UNIQUE incondicional. Sin removerlo aquí no se
# puede sembrar el estado de A-17/R16 (dos `table_sessions` 'active' de la misma
# mesa) ni el flujo normal de "un carrito nuevo tras `submit_cart`".
_PARTIAL_UNIQUE_INDEXES = {
    "table_sessions": "idx_active_session_per_table",
    "carts": "idx_open_cart_per_participant",
    # spec 024: sin remover esto no se puede sembrar más de un
    # `OrderPaymentAttempt` por orden (p.ej. uno rechazado + uno nuevo
    # pendiente, US5) — el índice se crearía como UNIQUE incondicional.
    "order_payment_attempts": "idx_pending_payment_attempt_per_order",
    # spec 025: sin remover esto no se puede sembrar, a propósito, más de una
    # orden no-terminal del mismo comensal en un test que necesite dos
    # participantes/dos órdenes (T006) — el índice se crearía como UNIQUE
    # incondicional sobre `participant_id`.
    "customer_orders": "idx_active_order_per_participant",
}


def _remove_partial_unique_indexes() -> None:
    """Remueve del `Table.indexes` en memoria los dos índices parciales que
    SQLite compilaría distinto (research.md §3). Idempotente: `discard` no
    falla si una llamada anterior ya los quitó."""
    for table_name, index_name in _PARTIAL_UNIQUE_INDEXES.items():
        for table in Base.metadata.tables.values():
            if table.name != table_name:
                continue
            for idx in list(table.indexes):
                if idx.name == index_name:
                    table.indexes.discard(idx)


# spec 063: `sales`/`invoices`/`customer_orders.applied_promotions` tienen
# `server_default=text("'[]'::jsonb")` (`app/models/*.py`) — válido en Postgres,
# pero SQLite no entiende el cast `::jsonb` dentro de un `DEFAULT` de columna y
# `create_all()` falla con `OperationalError: unrecognized token`. Se reemplaza
# aquí por `DEFAULT '[]'` (sin cast), solo en el metadata en memoria (no toca los
# modelos de producción): `create_order` de producción NO fija `applied_promotions`
# (se llena en el cobro), así que el `DEFAULT` sí hace falta para el `NOT NULL`.
# Idempotente.
_SQLITE_INCOMPATIBLE_DEFAULTS = {
    "sales": ["applied_promotions"],
    "invoices": ["applied_promotions"],
    "customer_orders": ["applied_promotions"],
}


def _patch_sqlite_incompatible_server_defaults() -> None:
    for table in Base.metadata.tables.values():
        for col_name in _SQLITE_INCOMPATIBLE_DEFAULTS.get(table.name, ()):
            col = table.c.get(col_name)
            if col is not None:
                col.server_default = DefaultClause(text("'[]'"))


def new_session() -> Session:
    """Sesión SQLAlchemy real sobre SQLite en memoria, con el esquema ampliado
    (catálogo + inventario, reexportado de `fixtures.py`, más
    mesas/carrito/pedidos/promociones). Remueve
    `idx_active_session_per_table` e `idx_open_cart_per_participant` del
    metadata antes de `create_all` (research.md §3) — cada llamada es
    idempotente respecto a esa remoción."""
    _remove_partial_unique_indexes()
    _patch_sqlite_incompatible_server_defaults()
    tables = [t for t in Base.metadata.tables.values() if t.name in _TABLE_NAMES]
    engine = create_engine("sqlite:///:memory:")
    conn = engine.connect().execution_options(schema_translate_map={"tenant": None})
    conn.connection.dbapi_connection.create_aggregate("bool_or", 1, _BoolOr)
    Base.metadata.create_all(bind=conn, tables=tables)
    attach_shared_tenants(conn)  # spec 088: `shared.tenants.logo_url` (is_key_referenced)
    conn.commit()
    return Session(bind=conn)


def _uid() -> uuid.UUID:
    return uuid.uuid4()


_table_number_seq = count(1)


# --------------------------------------------------------------- Factories nuevas

def make_dining_table(db: Session, **kw) -> DiningTable:
    kw.setdefault("id", _uid())
    kw.setdefault("number", next(_table_number_seq))
    kw.setdefault("name", f"mesa-{kw['id']}")
    kw.setdefault("active", True)
    kw.setdefault("status", "libre")
    obj = DiningTable(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_table_session(
    db: Session, table: DiningTable | None = None, **kw
) -> TableSession:
    if table is None:
        table = make_dining_table(db)
    kw.setdefault("id", _uid())
    kw.setdefault("dining_table_id", table.id)
    kw.setdefault("status", "active")
    obj = TableSession(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_participant(
    db: Session, table_session: TableSession | None = None, **kw
) -> SessionParticipant:
    if table_session is None:
        table_session = make_table_session(db)
    kw.setdefault("id", _uid())
    kw.setdefault("table_session_id", table_session.id)
    kw.setdefault("dining_table_id", table_session.dining_table_id)
    kw.setdefault("display_name", f"comensal-{kw['id']}")
    kw.setdefault("display_label", kw["display_name"])
    kw.setdefault("status", "open")
    obj = SessionParticipant(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_cart(db: Session, participant: SessionParticipant | None = None, **kw) -> Cart:
    if participant is None:
        participant = make_participant(db)
    kw.setdefault("id", _uid())
    kw.setdefault("participant_id", participant.id)
    kw.setdefault("status", "abierto")
    obj = Cart(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_cart_item(
    db: Session, cart: Cart, variant: ProductVariant, **kw
) -> CartItem:
    kw.setdefault("id", _uid())
    kw.setdefault("cart_id", cart.id)
    kw.setdefault("product_variant_id", variant.id)
    kw.setdefault("quantity", 1)
    kw.setdefault("unit_price", variant.price)
    obj = CartItem(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_customer_order(
    db: Session, participant: SessionParticipant, **kw
) -> CustomerOrder:
    kw.setdefault("id", _uid())
    kw.setdefault("table_session_id", participant.table_session_id)
    kw.setdefault("participant_id", participant.id)
    kw.setdefault("dining_table_id", participant.dining_table_id)
    kw.setdefault("customer_name", participant.display_label or participant.display_name)
    kw.setdefault("channel", "QR_MENU")
    kw.setdefault("order_type", "DINE_IN")
    kw.setdefault("status", "recibida")
    kw.setdefault("created_at", datetime.now())
    obj = CustomerOrder(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_promotion(db: Session, **kw) -> Promotion:
    """spec 063 (revisión 2026-09-01): la promoción solo lleva vigencia y
    estado — `type`/`value`/`min_qty` viven en cada `PromotionRule`
    (`add_rule_to_promotion`)."""
    kw.setdefault("id", _uid())
    kw.setdefault("name", f"promo-{kw['id']}")
    kw.setdefault("status", "active")
    kw.setdefault("created_at", datetime.now())
    obj = Promotion(**kw)
    db.add(obj)
    db.flush()
    return obj


def add_rule_to_promotion(
    db: Session,
    promotion: Promotion,
    *,
    type: str = "percent",
    value: Decimal = Decimal("10"),
    min_qty: int = 1,
    variants: list[ProductVariant] = (),
    **kw,
) -> PromotionRule:
    """spec 063 (revisión 2026-09-01, FR-001/FR-001a): agrega una **regla**
    (tipo, valor, cantidad mínima + su propio conjunto de variantes) a una
    promoción (contracts/migracion.md §2.1)."""
    kw.setdefault("id", _uid())
    rule = PromotionRule(
        promotion_id=promotion.id, type=type, value=value, min_qty=min_qty, **kw
    )
    db.add(rule)
    db.flush()
    for variant in variants:
        db.add(PromotionVariant(
            id=_uid(),
            promotion_rule_id=rule.id,
            product_variant_id=variant.id,
        ))
    db.flush()
    return rule


def make_payment_method(db: Session, **kw) -> PaymentMethod:
    """`kw.setdefault(is_cash=True)` por defecto; los tests de transferencia
    (spec 024) pasan `is_cash=False, type="transfer", payment_info={...}`."""
    kw.setdefault("id", _uid())
    kw.setdefault("name", f"metodo-{kw['id']}")
    kw.setdefault("is_cash", True)
    kw.setdefault("type", "cash" if kw["is_cash"] else "other")
    kw.setdefault("active", True)
    obj = PaymentMethod(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_cash_register(db: Session, **kw) -> CashRegister:
    kw.setdefault("id", _uid())
    kw.setdefault("name", f"caja-{kw['id']}")
    kw.setdefault("active", True)
    obj = CashRegister(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_cash_shift(db: Session, register: CashRegister | None = None, **kw) -> CashShift:
    """spec 087 (FR-006, A-86): turno abierto contra el que
    `resolve_dine_in_table_order` numera los pedidos DINE_IN de `submit_cart`."""
    if register is None:
        register = make_cash_register(db)
    kw.setdefault("id", _uid())
    kw.setdefault("cash_register_id", register.id)
    kw.setdefault("user_id", _uid())
    kw.setdefault("user_name", "Cajero de prueba")
    kw.setdefault("opening_amount", Decimal("100000"))
    kw.setdefault("opened_at", datetime.now())
    kw.setdefault("status", "open")
    obj = CashShift(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_payment_attempt(
    db: Session, order: CustomerOrder, method: PaymentMethod, **kw
) -> OrderPaymentAttempt:
    """`kw.setdefault(status="pendiente")` — los tests de US2/US3/US4/US5
    sobreescriben para sembrar intentos ya `confirmado`/`rechazado`."""
    kw.setdefault("id", _uid())
    kw.setdefault("order_id", order.id)
    kw.setdefault("payment_method_id", method.id)
    kw.setdefault("status", "pendiente")
    kw.setdefault("created_at", datetime.now())
    obj = OrderPaymentAttempt(**kw)
    db.add(obj)
    db.flush()
    return obj


# --------------------------------------------------------------- Reloj fijado (A-08)

class frozen_now:
    """Context manager: parchea `<module>.datetime` (por defecto
    `app.api.v1.cart.service`) por una subclase cuyo `.now(tz)` devuelve
    `instant` fijo, sin importar el `tz` que reciba. `timedelta`/`timezone`
    del módulo no se tocan (research.md §5, specs/015-caracterizacion-cart).

    `module` (specs/022-correccion-zona-horaria-menu-carrito, research.md
    Decisión 2) permite reutilizar el mismo reloj fijado en otros módulos que
    también evalúan vigencia de promociones (p. ej. `app.api.v1.menu.router`)
    sin duplicar esta clase.

    Uso:
        with frozen_now(datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)):
            resp = service.open_session(db, tenant_id, table, "Ana")

        with frozen_now(instant, module="app.api.v1.menu.router"):
            menu = _build_menu(db)
    """

    def __init__(self, instant: datetime, module: str = "app.api.v1.cart.service"):
        self._instant = instant
        self._module = module
        self._patcher = None

    def __enter__(self) -> "frozen_now":
        instant = self._instant

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return instant

        self._patcher = mock.patch(f"{self._module}.datetime", FixedDatetime)
        self._patcher.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._patcher.stop()
        return False


# --------------------------------------------------------------- Harness de router

def build_session_context(
    db: Session,
    *,
    tenant_id: int = 1,
    tenant_schema: str = "tenant_test",
    table: DiningTable,
    table_session: TableSession,
    participant: SessionParticipant,
) -> SessionContext:
    """`SessionContext` real (mismo dataclass de producción) poblado a mano.
    Se pasa como `ctx=` directamente a las funciones de endpoint que hoy lo
    reciben vía `Depends(get_session_context)` — `Depends` no se resuelve al
    llamar la función Python directamente, así que no hace falta ningún
    parche para estas (research.md §1). `tenant_schema` (spec 024) lo usa
    `presign_receipt` (`cart/service.py`) para construir la key de R2."""
    # `timezone` (spec 127): `cart/service.py` ahora reenvía `ctx.tenant` al
    # motor de promociones (`resolve_timezone`), que lee este atributo -- sin
    # él, cualquier test que ejercite una promoción vigente via este harness
    # levanta `AttributeError`.
    tenant = SimpleNamespace(id=tenant_id, schema=tenant_schema, timezone="America/Bogota")
    return SessionContext(
        db=db,
        tenant=tenant,
        table_id=table.id,
        participant=participant,
        table_session=table_session,
    )


class patched_qr_context:
    """Context manager: parchea `app.api.v1.cart.router.open_qr_context` para
    que, en vez de resolver el tenant vía Postgres real, entregue un
    `QrContext` de prueba fijo (`ctx=`), o levante la misma `HTTPException`
    401 que el código real levantaría con un token inválido (`raises=`).

    Uso: `with patched_qr_context(ctx=mi_qr_context): ...`"""

    def __init__(self, ctx: QrContext | None = None, *, raises: HTTPException | None = None):
        self._ctx = ctx
        self._raises = raises
        self._patcher = None

    def __enter__(self) -> "patched_qr_context":
        ctx = self._ctx
        raises = self._raises

        @contextmanager
        def _fake_open_qr_context(token: str) -> Iterator[QrContext]:
            if raises is not None:
                raise raises
            yield ctx

        self._patcher = mock.patch(
            "app.api.v1.cart.router.open_qr_context", _fake_open_qr_context
        )
        self._patcher.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._patcher.stop()
        return False


class patched_session_context:
    """Análogo a `patched_qr_context`, pero para
    `app.api.v1.cart.router.open_session_context` — lo usa el test de
    `POST /cart/leave` con token válido (el caso sin token no lo necesita: el
    endpoint retorna antes de abrir el contexto, research.md §1)."""

    def __init__(self, ctx: SessionContext | None = None, *, raises: HTTPException | None = None):
        self._ctx = ctx
        self._raises = raises
        self._patcher = None

    def __enter__(self) -> "patched_session_context":
        ctx = self._ctx
        raises = self._raises

        @contextmanager
        def _fake_open_session_context(token: str, *, touch: bool = True) -> Iterator[SessionContext]:
            if raises is not None:
                raise raises
            yield ctx

        self._patcher = mock.patch(
            "app.api.v1.cart.router.open_session_context", _fake_open_session_context
        )
        self._patcher.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._patcher.stop()
        return False


class FakeRedisBucket:
    """Doble mínimo de `app.core.redis.token_blocklist` para
    `app.core.rate_limit.enforce`: `incr`/`expire` async, contador en memoria
    por instancia. No implementa el resto de la API de `redis.asyncio.Redis`.

    Uso:
        with mock.patch("app.core.rate_limit.redis", FakeRedisBucket()):
            ...
    junto con `mock.patch.object(settings, "RATE_LIMIT_ENABLED", True)`."""

    def __init__(self):
        self._counts: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    async def expire(self, key: str, ttl: int) -> None:
        return None


# --------------------------------------------------------------- Test de humo (T008)

if __name__ == "__main__":
    from app.models.audit_log import AuditLog

    db = new_session()

    table = make_dining_table(db)
    # A-17/R16: dos TableSession 'active' de la misma mesa. Sin la remoción de
    # `idx_active_session_per_table` (T002), este segundo INSERT fallaría.
    ts1 = make_table_session(db, table=table, status="active")
    ts2 = make_table_session(db, table=table, status="active")
    assert ts1.id != ts2.id

    participant = make_participant(db, table_session=ts1)

    # Doble carrito 'abierto' del mismo comensal: sin la remoción de
    # `idx_open_cart_per_participant` (T002), el segundo INSERT fallaría.
    cart1 = make_cart(db, participant=participant)
    cart2 = Cart(participant_id=participant.id, status="abierto")
    db.add(cart2)
    db.flush()
    assert cart1.id != cart2.id

    category = make_category(db)
    product = make_product(db, category=category)
    variant = make_variant(db, product=product)
    item = make_cart_item(db, cart1, variant)
    assert item.cart_id == cart1.id

    # `audit_logs.payload` es JSONB en Postgres, JSON genérico en SQLite: debe
    # aceptar un dict tal cual (research.md §4).
    db.add(AuditLog(action="test.smoke", entity="carts", payload={"ok": True}))
    db.commit()

    print("cart_fixtures: test de humo OK")
