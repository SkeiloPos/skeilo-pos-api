"""Infraestructura compartida para los characterization tests de `orders`
(`test_orders_consolidation.py`, `test_orders_checkout.py`, `test_orders_kitchen.py`,
`test_orders_tables_advanced.py`, `test_orders_service.py`) — no es código de producción.

Amplía el motor SQLite-en-memoria de `fixtures.py` (que no se toca) con el
superconjunto de tablas que ya existen, repartidas, en `cart_fixtures.py`
(carrito, `order_cancel_logs`, `audit_logs`) y `table_sessions_fixtures.py`
(mesas/sesión/comensales/pedidos, promociones, caja/ventas/factura), más una
tabla que ninguno de los dos necesitó: `order_item_void_logs`
(`kitchen.void_item`).

Autónomo, como sus dos hermanos (research.md §1, plan.md §Structure Decision):
no importa nada de `cart_fixtures.py` ni de `table_sessions_fixtures.py` —
registra su propio compilador `@compiles(JSONB, "sqlite")` de forma
independiente, idempotente si convive en el mismo proceso de test con los otros
dos.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from itertools import count
from types import SimpleNamespace
from unittest import mock
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.schema import DefaultClause
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

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
    "make_dining_table", "make_table_session", "make_participant",
    "make_customer_order", "make_order_item", "make_order_item_void_log",
    "make_cart", "make_cart_item",
    "make_promotion", "add_rule_to_promotion",
    "make_cash_register", "make_cash_shift", "make_payment_method",
    "make_sale", "make_payment_attempt",
    "make_tenant_double", "make_user_double",
    "force_flush_integrity_error",
]

from app.core.models import Base
import app.models  # noqa: F401 - ya registrado vía fixtures.py; se repite por claridad

from app.models.dining_table import DiningTable
from app.models.table_session import TableSession
from app.models.session_participant import SessionParticipant
from app.models.cart import Cart
from app.models.cart_item import CartItem
from app.models.customer_order import CustomerOrder
from app.models.order_item import OrderItem
from app.models.order_item_void_log import OrderItemVoidLog
from app.models.order_payment_attempt import OrderPaymentAttempt
from app.models.product_variant import ProductVariant
from app.models.promotion import Promotion, PromotionRule, PromotionVariant
from app.models.cash_register import CashRegister
from app.models.cash_shift import CashShift
from app.models.shift_sales_report import (
    ShiftSalesReport, ShiftSalesReportProduct, ShiftSalesReportCategory,
)
from app.models.payment import Payment, PaymentMethod
from app.models.sale import Sale, SaleItem
from app.models.invoice import Invoice, InvoiceCounter


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

# data-model.md §Esquema SQLite: cierre transitivo real de lo que las 23
# funciones públicas y sus dependencias (orders.consumption, promotions.service,
# sales.builder, invoices.service) tocan.
_ORDERS_TABLE_NAMES = [
    "dining_tables",
    "table_sessions",
    "session_participants",
    "customer_orders",
    "order_items",
    "order_item_options",
    "order_item_void_logs",
    "order_cancel_logs",
    "carts",
    "cart_items",
    "cart_item_options",
    "promotions",
    # spec 063 (revisión 2026-09-01): una promoción agrupa una o más reglas.
    "promotion_rules",
    # spec 063: conjunto explícito de variantes elegibles (de una regla).
    "promotion_variants",
    "cash_registers",
    "cash_shifts",
    "shift_sales_reports",
    "shift_sales_report_products",
    "shift_sales_report_categories",
    "payment_methods",
    "payments",
    "sales",
    "sale_items",
    "invoices",
    "invoice_counters",
    "audit_logs",
    "order_payment_attempts",
]

_TABLE_NAMES = _CATALOG_TABLE_NAMES + _ORDERS_TABLE_NAMES

# spec 024: sin remover esto no se puede sembrar más de un
# `OrderPaymentAttempt` por orden (p.ej. uno rechazado + uno nuevo pendiente,
# o dos rechazados + uno confirmado) — sobre SQLite el índice se crearía como
# UNIQUE incondicional (mismo mecanismo que `cart_fixtures._PARTIAL_UNIQUE_INDEXES`,
# research.md spec 015 §3).
_PARTIAL_UNIQUE_INDEXES = {
    "order_payment_attempts": "idx_pending_payment_attempt_per_order",
}


def _remove_partial_unique_indexes() -> None:
    for table_name, index_name in _PARTIAL_UNIQUE_INDEXES.items():
        for table in Base.metadata.tables.values():
            if table.name != table_name:
                continue
            for idx in list(table.indexes):
                if idx.name == index_name:
                    table.indexes.discard(idx)


# `sale_items.options` es `postgresql.JSONB` (`app/models/sale.py`). La versión
# de SQLAlchemy de este entorno no la compila a un tipo genérico sobre SQLite
# por sí sola — sin este shim `create_all()` falla con
# `UnsupportedCompilationError` antes de crear una sola tabla. Es un registro de
# compilador de SQLAlchemy, no una migración de modelo: no toca
# `app/models/sale.py` ni cambia el tipo en Postgres.
@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json_on_sqlite(element, compiler, **kw):  # pragma: no cover
    return "JSON"


def _patch_sqlite_incompatible_server_defaults() -> None:
    """`sale_items.options` y (spec 063) `sales`/`invoices`/`customer_orders.
    applied_promotions` tienen `server_default=text("'[]'::jsonb")` — válido en
    Postgres, pero SQLite no entiende el cast `::jsonb` dentro de un `DEFAULT` de
    columna y `create_all()` falla con `OperationalError: unrecognized token`. Se
    elimina aquí, solo en el metadata en memoria que usa este fixture (no se
    tocan los modelos de producción): todos los caminos de cobro siembran esos
    campos explícito, así que el `DEFAULT` nunca hacía falta de verdad.
    Idempotente."""
    # `None` donde todos los caminos de siembra fijan el valor explícito;
    # `DEFAULT '[]'` (sin cast) donde producción NO lo fija (spec 063:
    # `create_order` no toca `applied_promotions`, se llena en el cobro).
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


def new_session(*, autoflush: bool = True) -> Session:
    """Sesión SQLAlchemy real sobre SQLite en memoria, con el esquema ampliado
    (catálogo, reexportado de `fixtures.py`, más el superconjunto de
    mesas/sesión/comensales/pedidos/carrito/promociones/caja/ventas/factura/
    auditoría que `orders` necesita). No remueve ningún índice único parcial
    (`idx_active_session_per_table`, `idx_open_cart_per_participant`,
    `idx_open_shift_per_register`): ningún escenario de esta spec siembra dos
    filas activas que colisionen con ellos. Sí remueve
    `idx_pending_payment_attempt_per_order` (spec 024): esos tests siembran
    varios intentos de pago no-pendientes por orden a propósito.

    `autoflush` por defecto es `True` (el default de SQLAlchemy) para no
    cambiar el comportamiento de ningún test existente. La sesión real de
    producción (`app/core/db.py::with_db`) usa `autoflush=False` — pásalo
    explícitamente cuando el test necesite reproducir ese comportamiento
    (p. ej. detectar un `db.flush()` faltante entre una mutación y una
    `SELECT` que dependa de verla en la misma transacción, spec 028)."""
    _patch_sqlite_incompatible_server_defaults()
    _remove_partial_unique_indexes()
    tables = [t for t in Base.metadata.tables.values() if t.name in _TABLE_NAMES]
    engine = create_engine("sqlite:///:memory:")
    conn = engine.connect().execution_options(schema_translate_map={"tenant": None})
    Base.metadata.create_all(bind=conn, tables=tables)
    conn.commit()
    return Session(bind=conn, autoflush=autoflush)


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


def make_customer_order(
    db: Session, table_session: TableSession, participant: SessionParticipant | None = None,
    **kw
) -> CustomerOrder:
    """`kw.setdefault(status="abierta")` — ni recién recibido ni terminal,
    cobrable de inmediato; los tests que ejercitan A-01/A-16/Historia 2 lo
    sobreescriben explícitamente."""
    kw.setdefault("id", _uid())
    kw.setdefault("table_session_id", table_session.id)
    kw.setdefault("participant_id", participant.id if participant is not None else None)
    kw.setdefault("dining_table_id", table_session.dining_table_id)
    kw.setdefault("channel", "POS")
    kw.setdefault("order_type", "DINE_IN")
    kw.setdefault("status", "abierta")
    kw.setdefault("created_at", datetime.now())
    obj = CustomerOrder(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_order_item(db: Session, order: CustomerOrder, variant: ProductVariant, **kw) -> OrderItem:
    """`kw.setdefault(estado_cocina="listo")` — cobrable de inmediato; los tests
    que ejercitan cocina en curso o ítems anulados lo sobreescriben."""
    kw.setdefault("id", _uid())
    kw.setdefault("order_id", order.id)
    kw.setdefault("product_variant_id", variant.id)
    kw.setdefault("quantity", 1)
    kw.setdefault("unit_price", variant.price)
    kw.setdefault("estado_cocina", "listo")
    obj = OrderItem(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_order_item_void_log(db: Session, order_item: OrderItem, **kw) -> OrderItemVoidLog:
    """No es una factory que ningún test de esta spec necesite llamar
    directamente para sembrar estado previo (`kitchen.void_item` la crea
    internamente); existe para que la tabla quede completa y disponible."""
    kw.setdefault("id", _uid())
    kw.setdefault("order_item_id", order_item.id)
    kw.setdefault("motivo", "prueba")
    kw.setdefault("user_id", _uid())
    kw.setdefault("user_name", "Cajero de prueba")
    obj = OrderItemVoidLog(**kw)
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


def make_cart_item(db: Session, cart: Cart, variant: ProductVariant, **kw) -> CartItem:
    kw.setdefault("id", _uid())
    kw.setdefault("cart_id", cart.id)
    kw.setdefault("product_variant_id", variant.id)
    kw.setdefault("quantity", 1)
    kw.setdefault("unit_price", variant.price)
    obj = CartItem(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_promotion(db: Session, **kw) -> Promotion:
    """spec 063 (revisión 2026-09-01): la promoción solo lleva vigencia y
    estado — `type`/`value`/`min_qty` viven en cada `PromotionRule`
    (`add_rule_to_promotion`). `kw.setdefault` fuerza `start_time=None,
    end_time=None`: sin ventana horaria, siempre válida sin importar el
    reloj real (mismo criterio que `table_sessions_fixtures.make_promotion`,
    research.md §5 de la spec 016)."""
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


def make_cash_shift(db: Session, register: CashRegister | None = None, **kw) -> CashShift:
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
    """`kw.setdefault(is_cash=True)`: evita el chequeo de 'no_efectivo > total'
    de `build_sale` por defecto; los tests de pago mixto lo sobreescriben."""
    kw.setdefault("id", _uid())
    kw.setdefault("name", f"metodo-{kw['id']}")
    kw.setdefault("is_cash", True)
    kw.setdefault("type", "cash" if kw["is_cash"] else "other")
    kw.setdefault("active", True)
    obj = PaymentMethod(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_sale(
    db: Session,
    *,
    order: CustomerOrder | None = None,
    table_session: TableSession | None = None,
    **kw,
) -> Sale:
    """Venta emitida, sembrada directamente (spec 094, T009).

    Hasta esta spec los tests de `orders` construían `Sale(...)` a mano en cada
    caso (`test_orders_pagination._seed_order`). El desglose de facturación
    necesita sembrar los **tres** escenarios de D2 —venta propia del pedido,
    venta de la sesión sin pedido, y ninguna venta— así que el constructor se
    centraliza aquí en vez de repetirse.

    `order` y `table_session` son los dos ganchos que distinguen esos
    escenarios, y solo se asignan **cuando se pasan**: una venta sin
    `customer_order_id` es exactamente lo que produce el cierre unificado de
    varios pedidos (`table_sessions/service.py:734`) y el cierre dividido
    (`:832-847`), y es el caso `factura_agrupada`.

    `cash_shift_id` se crea con `make_cash_shift` si no se pasa: la columna es
    `NOT NULL` y ningún test de facturación se interesa por el turno."""
    kw.setdefault("id", _uid())
    if order is not None:
        kw.setdefault("customer_order_id", order.id)
    if table_session is not None:
        kw.setdefault("table_session_id", table_session.id)
    if "cash_shift_id" not in kw:
        kw["cash_shift_id"] = make_cash_shift(db).id
    kw.setdefault("user_id", _uid())
    kw.setdefault("status", "paid")
    kw.setdefault("subtotal", Decimal("0"))
    kw.setdefault("discount", Decimal("0"))
    kw.setdefault("delivery_fee", Decimal("0"))
    kw.setdefault("total", Decimal("0"))
    kw.setdefault("applied_promotions", [])
    obj = Sale(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_payment_attempt(
    db: Session, order: CustomerOrder, method: PaymentMethod, **kw
) -> OrderPaymentAttempt:
    """`kw.setdefault(status="pendiente")` (spec 024) — los tests de
    aprobar/rechazar/confirmar-efectivo/gate de `confirm_order` sobreescriben
    según el escenario."""
    kw.setdefault("id", _uid())
    kw.setdefault("order_id", order.id)
    kw.setdefault("payment_method_id", method.id)
    kw.setdefault("status", "pendiente")
    kw.setdefault("created_at", datetime.now())
    obj = OrderPaymentAttempt(**kw)
    db.add(obj)
    db.flush()
    return obj


# --------------------------------------------------------- Dobles de Tenant/User

def make_tenant_double(
    *, id: int = 1, invoice_prefix: str = "", timezone: str = "America/Bogota"
) -> SimpleNamespace:
    """No es el modelo `Tenant` real (schema `shared`, fuera de las tablas
    creadas por este fixture) — bastan los atributos que las funciones bajo
    prueba podrían leer de él (`timezone`: spec 030, `resolve_timezone`)."""
    return SimpleNamespace(id=id, invoice_prefix=invoice_prefix, timezone=timezone)


def make_user_double(*, id=None, name: str = "Cajero de prueba") -> SimpleNamespace:
    """No es el modelo `User` real, por el mismo motivo. `id` por defecto es un
    `uuid4()` nuevo si no se pasa."""
    if id is None:
        id = uuid.uuid4()
    return SimpleNamespace(id=id, name=name)


# ------------------------------------------ Forzado de IntegrityError (A-26/RN-ORD-60)

class force_flush_integrity_error:
    """Context manager: parchea `db.flush` (de instancia, no de clase) con
    `side_effect=IntegrityError(...)` durante el bloque `with`, para ejercitar el
    `except IntegrityError` huérfano de `tables_advanced.move_order`
    (research.md §2). Fuera del bloque, `db.flush` vuelve a su comportamiento
    real.

    También desactiva `db.autoflush` mientras el parche está activo: sin esto,
    cualquier `SELECT` intermedio de `move_order` (p.ej. `_active_orders_on_table`)
    dispara un autoflush que, contra un `db.flush` mockeado, revienta antes de
    llegar al `db.flush()` explícito que es el que en realidad se quiere
    interceptar — un autoflush sin nada pendiente es un no-op contra el
    `flush()` real, pero deja de serlo cuando `flush()` está reemplazado por un
    mock que siempre lanza. Se restaura al salir del bloque, igual que el
    parche de `flush`.

    Uso:
        with orders_fixtures.force_flush_integrity_error(db):
            with self.assertRaises(HTTPException) as ctx:
                tables_advanced.move_order(db, order.id, target_table.id)
        assert ctx.exception.status_code == 409

    Único mock que introduce esta spec: todo lo demás se ejercita contra código
    real, sin dobles (FR-010, FR-011, FR-012)."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._patcher = None
        self._prev_autoflush = None

    def __enter__(self) -> "force_flush_integrity_error":
        self._prev_autoflush = self._db.autoflush
        self._db.autoflush = False
        self._patcher = mock.patch.object(
            self._db, "flush",
            side_effect=IntegrityError("stmt", {}, Exception("forzado por test (research.md §2)")),
        )
        self._patcher.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._patcher.stop()
        self._db.autoflush = self._prev_autoflush
        return False


# --------------------------------------------------------------- Test de humo (T011)

if __name__ == "__main__":
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

    void_log = make_order_item_void_log(db, item)
    assert void_log.order_item_id == item.id

    cart = make_cart(db, participant=participant)
    cart_item = make_cart_item(db, cart, variant)
    assert cart_item.cart_id == cart.id

    promo = make_promotion(db)
    rule = add_rule_to_promotion(db, promo, variants=[variant])
    assert rule.promotion_id == promo.id

    register = make_cash_register(db)
    shift = make_cash_shift(db, register=register)
    method = make_payment_method(db)
    assert shift.cash_register_id == register.id
    assert method.is_cash is True

    tenant = make_tenant_double()
    user = make_user_double()
    assert tenant.id == 1
    assert user.name == "Cajero de prueba"

    # `sale_items.options` es JSONB en Postgres, JSON genérico en SQLite: debe
    # aceptar un dict tal cual.
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

    with force_flush_integrity_error(db):
        try:
            db.flush()
        except IntegrityError:
            pass
        else:
            raise AssertionError("force_flush_integrity_error no interceptó db.flush()")
    # Fuera del bloque, db.flush() vuelve a la normalidad.
    db.flush()

    print("orders_fixtures: test de humo OK")
