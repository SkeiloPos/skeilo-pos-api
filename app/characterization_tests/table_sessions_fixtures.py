"""Infraestructura compartida para los characterization tests de `table_sessions`
(`test_table_sessions_split_blindaje.py`, `test_table_sessions_service.py`,
`test_table_sessions_router.py`) — no es código de producción.

Amplía el motor SQLite-en-memoria de `fixtures.py` (que no se toca, ver
`contracts/test-harness-api.md` de `specs/016-caracterizacion-table-sessions`) con
las 18 tablas de mesas/sesión/comensales/pedidos/promociones/caja/ventas/factura que
`table_sessions/service.py` y sus dependencias reales (`orders.checkout`,
`promotions.service`, `sales.builder`, `invoices.service`) necesitan, sus factories,
los dobles de `Tenant`/`User` que sustituyen `Depends` para invocar `router.py`
directo, el espía de `service._load` (A-17/R12) y el doble de `events.bill_changed`
(FR-010a).

A diferencia de `cart_fixtures.py`, este módulo es **autónomo**: no importa nada de
`cart_fixtures.py` ni depende de él (research.md §1-4, plan.md §Structure Decision) —
registra su propio compilador `@compiles(JSONB, "sqlite")` de forma independiente,
aunque sea idempotente si ambos módulos se cargan en el mismo proceso de test.
"""
from __future__ import annotations

from collections import namedtuple
from datetime import datetime
from decimal import Decimal
from itertools import count
from types import SimpleNamespace
from unittest import mock
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.schema import DefaultClause

from app.characterization_tests.fixtures import (
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

# Reexport tal cual (contracts/test-harness-api.md §Sesión y base de datos): el
# propio import de arriba ya los trae al namespace de este módulo.
__all__ = [
    "make_category", "make_inventory_item", "make_option", "make_option_group",
    "make_product", "make_recipe_item", "make_unit", "make_variant",
    "link_variant_group",
    "new_session",
    "make_dining_table", "make_table_session", "make_participant", "make_cart",
    "make_customer_order", "make_order_item",
    "make_promotion", "add_rule_to_promotion",
    "make_cash_register", "make_cash_shift", "make_payment_method",
    "make_tenant_double", "make_user_double",
    "spy_load", "spy_bill_changed",
]

from app.core.models import Base
import app.models  # noqa: F401 - ya registrado vía fixtures.py; se repite por claridad

from app.models.dining_table import DiningTable
from app.models.table_session import TableSession
from app.models.session_participant import SessionParticipant
from app.models.cart import Cart
from app.models.customer_order import CustomerOrder
from app.models.order_item import OrderItem, OrderItemOption
from app.models.product_variant import ProductVariant
from app.models.promotion import Promotion, PromotionRule, PromotionVariant
from app.models.cash_register import CashRegister
from app.models.cash_shift import CashShift
from app.models.payment import Payment, PaymentMethod
from app.models.sale import Sale, SaleItem
from app.models.invoice import Invoice, InvoiceCounter


# --------------------------------------------------------------- Esquema SQLite

_CATALOG_TABLE_NAMES = [
    "categories",
    "products",
    "product_toppings",
    "product_variants",
    "presentations",  # spec 084 (A-79): la variante depende de su presentación
    "option_groups",
    "options",
    "variant_option_groups",
    "recipe_items",
    "inventory_items",
    "inventory_movements",
    "unit_measures",
    "unit_measure_groups",  # FK obligatoria de `unit_measures.group_id` (grupos de unidades)
]

# research.md §6: cierre transitivo real de lo que las 9 funciones públicas y sus
# dependencias (orders.checkout, promotions.service, sales.builder,
# invoices.service) tocan. Sin remover ningún índice único parcial (research.md
# §6: esta spec nunca siembra dos TableSession 'active' de la misma mesa ni dos
# CashShift 'open' de la misma caja, así que no hace falta el truco que sí
# necesitó cart_fixtures.py para A-17/R16).
_TABLE_SESSIONS_TABLE_NAMES = [
    "dining_tables",
    "table_sessions",
    "session_participants",
    "customer_orders",
    "order_items",
    "order_item_options",
    "order_item_toppings",
    "carts",
    "promotions",
    # spec 063 (revisión 2026-09-01): una promoción agrupa una o más reglas.
    "promotion_rules",
    # spec 063: conjunto explícito de variantes elegibles (de una regla).
    "promotion_variants",
    "cash_registers",
    "cash_shifts",
    "payment_methods",
    "payments",
    "sales",
    "sale_items",
    "invoices",
    "invoice_counters",
]

_TABLE_NAMES = _CATALOG_TABLE_NAMES + _TABLE_SESSIONS_TABLE_NAMES


# `sale_items.options` es `postgresql.JSONB` (`app/models/sale.py`). La versión de
# SQLAlchemy de este entorno NO compila `JSONB` a un tipo genérico sobre SQLite por
# sí sola (research.md §4, misma situación que ya resolvió `cart_fixtures.py` para
# `audit_logs.payload`) — sin este shim `create_all()` falla con
# `UnsupportedCompilationError` antes de crear una sola tabla. Es un registro de
# compilador de SQLAlchemy (`sqlalchemy.ext.compiler.compiles`), no una migración de
# modelo: no toca `app/models/sale.py` ni cambia el tipo en Postgres. Registrarlo
# de forma independiente aquí (en vez de importarlo de `cart_fixtures.py`) es
# idempotente si ambos módulos se cargan en el mismo proceso (plan.md §Structure
# Decision).
@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json_on_sqlite(element, compiler, **kw):  # pragma: no cover
    return "JSON"


def _patch_sqlite_incompatible_server_defaults() -> None:
    """`sale_items.options` tiene `server_default=text("'[]'::jsonb")`
    (`app/models/sale.py`) — válido en Postgres, pero SQLite no entiende el cast
    `::jsonb` dentro de un `DEFAULT` de columna y `create_all()` falla con
    `OperationalError: unrecognized token`. Se elimina aquí, solo en el metadata
    en memoria que usa este fixture (no se reemplaza por un literal: `Column.
    server_default` se compara con `bool()` en otros puntos de SQLAlchemy, y un
    `TextClause` sin envolver en `DefaultClause` no lo soporta). No es una
    migración de modelo: no toca los modelos de producción ni cambia el DDL real
    de Postgres — todos los tests siembran esos campos explícito
    (`build_sale`/`order_sale_lines` de producción también lo hacen siempre), así
    que el `DEFAULT` nunca hacía falta de verdad. Idempotente: `None` sobre
    `None` no tiene efecto adicional en una segunda llamada.

    spec 063: mismo tratamiento para `sales`/`invoices`/`customer_orders.
    applied_promotions` (`server_default=text("'[]'::jsonb")`)."""
    strip_default = {"sale_items": ["options"]}
    literal_default = {
        "sales": ["applied_promotions"],
        "invoices": ["applied_promotions"],
        "customer_orders": ["applied_promotions"],
    }
    for table in Base.metadata.tables.values():
        for col_name in strip_default.get(table.name, ()):
            col = table.c.get(col_name)
            if col is not None:
                col.server_default = None
        for col_name in literal_default.get(table.name, ()):
            col = table.c.get(col_name)
            if col is not None:
                col.server_default = DefaultClause(text("'[]'"))


def new_session() -> Session:
    """Sesión SQLAlchemy real sobre SQLite en memoria, con el esquema ampliado
    (catálogo, reexportado de `fixtures.py`, más
    mesas/sesión/comensales/pedidos/promociones/caja/ventas/factura). Registra el
    compilador JSONB->JSON para SQLite antes de `create_all` (research.md §4). No
    remueve ningún índice único parcial (research.md §6: esta spec no lo
    necesita)."""
    _patch_sqlite_incompatible_server_defaults()
    tables = [t for t in Base.metadata.tables.values() if t.name in _TABLE_NAMES]
    engine = create_engine("sqlite:///:memory:")
    conn = engine.connect().execution_options(schema_translate_map={"tenant": None})
    Base.metadata.create_all(bind=conn, tables=tables)
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
    kw.setdefault("opened_at", datetime.now())
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
    kw.setdefault("joined_at", datetime.now())
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


def make_customer_order(
    db: Session, table_session: TableSession, participant: SessionParticipant | None = None,
    **kw
) -> CustomerOrder:
    kw.setdefault("id", _uid())
    kw.setdefault("table_session_id", table_session.id)
    kw.setdefault("participant_id", participant.id if participant is not None else None)
    kw.setdefault("dining_table_id", table_session.dining_table_id)
    kw.setdefault("channel", "POS")
    kw.setdefault("order_type", "DINE_IN")
    # Estado por defecto 'abierta': ni recién recibido ni terminal, cobrable de
    # inmediato — los tests que ejercitan A-01 (recibida/en_preparacion/
    # cancelada/pagada) lo sobreescriben explícitamente.
    kw.setdefault("status", "abierta")
    kw.setdefault("created_at", datetime.now())
    obj = CustomerOrder(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_order_item(
    db: Session, order: CustomerOrder, variant: ProductVariant, **kw
) -> OrderItem:
    kw.setdefault("id", _uid())
    kw.setdefault("order_id", order.id)
    kw.setdefault("product_variant_id", variant.id)
    kw.setdefault("quantity", 1)
    kw.setdefault("unit_price", variant.price)
    # 'listo': cobrable de inmediato por defecto (no bloquea `_assert_closable`,
    # que rechaza EN_CURSO = ('pendiente', 'en_preparacion')). Los tests que
    # ejercitan cocina en curso o ítems anulados lo sobreescriben.
    kw.setdefault("estado_cocina", "listo")
    obj = OrderItem(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_promotion(db: Session, **kw) -> Promotion:
    """spec 063 (revisión 2026-09-01): la promoción solo lleva vigencia y
    estado — `type`/`value`/`min_qty` viven en cada `PromotionRule`
    (`add_rule_to_promotion`). `kw.setdefault` fuerza `start_time=None,
    end_time=None` (research.md §5): sin ventana horaria, siempre válida sin
    importar el reloj real."""
    kw.setdefault("id", _uid())
    kw.setdefault("name", f"promo-{kw['id']}")
    kw.setdefault("status", "active")
    kw.setdefault("start_time", None)
    kw.setdefault("end_time", None)
    # `promotions.service._best_line_match` ordena por `created_at.timestamp()`:
    # se fija explícito (no server_default) para no depender de que SQLAlchemy
    # refresque el valor generado por SQLite tras el flush.
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
    """spec 063 (revisión 2026-09-01, FR-001/FR-001a): agrega una regla
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


def make_cash_register(db: Session, **kw) -> CashRegister:
    kw.setdefault("id", _uid())
    kw.setdefault("name", f"caja-{kw['id']}")
    kw.setdefault("active", True)
    obj = CashRegister(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_cash_shift(
    db: Session, register: CashRegister | None = None, **kw
) -> CashShift:
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


def make_payment_method(db: Session, **kw) -> PaymentMethod:
    """`kw.setdefault(is_cash=True)`: la mayoría de los tests de cierre no
    necesita distinguir método; `is_cash=True` evita el chequeo de
    'no_efectivo > total' de `build_sale` por defecto."""
    kw.setdefault("id", _uid())
    kw.setdefault("name", f"metodo-{kw['id']}")
    kw.setdefault("is_cash", True)
    kw.setdefault("type", "cash" if kw["is_cash"] else "other")
    kw.setdefault("active", True)
    obj = PaymentMethod(**kw)
    db.add(obj)
    db.flush()
    return obj


# --------------------------------------------------------- Dobles de prueba (router)

def make_tenant_double(
    *, id: int = 1, invoice_prefix: str = "", timezone: str = "America/Bogota",
) -> SimpleNamespace:
    """No es el modelo `Tenant` real (schema `shared`, fuera de las tablas creadas
    por este fixture) — basta con los atributos que `table_sessions/router.py`
    y `service.py` leen de él (research.md §1). `timezone` (spec 138, FR-006): el cobro ahora
    reenvía el tenant al motor de promociones (`resolve_timezone`), que lee este atributo; un
    `Tenant` real siempre lo tiene (mismo precedente que `build_session_context`, spec 127)."""
    return SimpleNamespace(id=id, invoice_prefix=invoice_prefix, timezone=timezone)


def make_user_double(*, id=None, name: str = "Cajero de prueba") -> SimpleNamespace:
    """No es el modelo `User` real, por el mismo motivo. `id` por defecto es un
    `uuid4()` nuevo si no se pasa."""
    if id is None:
        id = uuid.uuid4()
    return SimpleNamespace(id=id, name=name)


# --------------------------------------------------------- Espía de _load (A-17/R12)

_LoadCall = namedtuple("_LoadCall", ["table_session_id", "lock"])


class spy_load:
    """Context manager: parchea `app.api.v1.table_sessions.service._load` con
    `wraps=service._load` (research.md §2) — la función real se sigue
    ejecutando, pero `.calls` queda disponible como lista de
    `(table_session_id, lock)` por cada invocación durante el bloque `with`.

    Uso:
        with table_sessions_fixtures.spy_load() as spy:
            service.add_participant(db, ts.id, "Ana", tenant_id=1)
        assert spy.calls[-1].lock is False
    """

    def __init__(self) -> None:
        self._patcher = None
        self._mock = None

    def __enter__(self) -> "spy_load":
        from app.api.v1.table_sessions import service

        self._patcher = mock.patch(
            "app.api.v1.table_sessions.service._load", wraps=service._load
        )
        self._mock = self._patcher.start()
        return self

    @property
    def calls(self) -> list[_LoadCall]:
        result: list[_LoadCall] = []
        for args, kwargs in self._mock.call_args_list:
            table_session_id = args[1] if len(args) > 1 else kwargs.get("table_session_id")
            lock = kwargs.get("lock", False)
            result.append(_LoadCall(table_session_id, lock))
        return result

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._patcher.stop()
        return False


# --------------------------------------------------- Doble de events.bill_changed

class spy_bill_changed:
    """Context manager: parchea `app.core.events.bill_changed`
    (`unittest.mock.patch` con `side_effect`) para no abrir un socket real a Redis
    y registrar cada invocación. Devuelve el `Mock` parcheado, con `.calls`
    disponible como lista de `(tenant_id, table_session_id)` en el orden en que
    ocurrieron — y con toda la interfaz habitual de `Mock`
    (`assert_called_once_with`, etc.).

    Uso:
        with table_sessions_fixtures.spy_bill_changed() as spy:
            service.add_participant(db, ts.id, "Ana", tenant_id=1)
        spy.assert_called_once_with(1, table_session_id=ts.id)
    """

    def __init__(self) -> None:
        self._patcher = None

    def __enter__(self):
        calls: list[tuple] = []

        def _spy(tenant_id, *, table_session_id):
            calls.append((tenant_id, table_session_id))
            return None

        self._patcher = mock.patch(
            "app.core.events.bill_changed", side_effect=_spy
        )
        mock_obj = self._patcher.start()
        mock_obj.calls = calls
        return mock_obj

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._patcher.stop()
        return False


# --------------------------------------------------------------- Test de humo (T010)

if __name__ == "__main__":
    from app.api.v1.table_sessions import service

    db = new_session()

    table = make_dining_table(db)
    ts = make_table_session(db, table=table)
    participant = make_participant(db, table_session=ts)
    order = make_customer_order(db, ts, participant=participant)

    category = make_category(db)
    product = make_product(db, category=category)
    variant = make_variant(db, product=product, price=Decimal("10000"))
    item = make_order_item(db, order, variant)
    assert item.order_id == order.id

    register = make_cash_register(db)
    shift = make_cash_shift(db, register=register)
    method = make_payment_method(db)
    assert shift.cash_register_id == register.id
    assert method.is_cash is True

    # `sale_items.options` es JSONB en Postgres, JSON genérico en SQLite: debe
    # aceptar un `dict`/`list` tal cual (research.md §4).
    sale = Sale(
        cash_shift_id=shift.id, user_id=uuid.uuid4(), user_name="Cajero de prueba",
        customer_name="Mesa de prueba", subtotal=Decimal("10000"), total=Decimal("10000"),
        status="paid",
    )
    db.add(sale)
    db.flush()
    db.add(SaleItem(
        sale_id=sale.id, product_variant_id=variant.id, description="prueba",
        options=[{"ok": True}], quantity=1, unit_price=Decimal("10000"),
        line_total=Decimal("10000"),
    ))
    db.commit()

    with spy_load() as spy:
        got = service.get_session(db, ts.id)
    assert got.id == ts.id
    assert spy.calls[-1] == (ts.id, False)

    with spy_bill_changed() as spy_bc:
        pass  # solo confirmar que el parche entra/sale sin error
    assert spy_bc.calls == []

    print("table_sessions_fixtures: test de humo OK")
