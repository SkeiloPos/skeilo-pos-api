"""Lógica de caja: resolución del turno abierto y arqueo (reconciliación),
equivalente a la vista `v_shift_reconciliation` de schema.sql en la capa de app.

Las ventas NO se guardan en `cash_movements`: se derivan de
Sale → Payment → PaymentMethod filtrando por `Sale.cash_shift_id` y
`Sale.status = 'paid'`, agrupadas por `PaymentMethod.type`. Solo el efectivo
(`type = 'cash'`) suma al efectivo esperado."""
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.core.pagination import paginate
from app.models.cash_register import CashRegister
from app.models.cash_shift import CashShift
from app.models.cash_movement import CashMovement
from app.models.customer_order import CustomerOrder
from app.models.sale import Sale
from app.models.payment import Payment, PaymentMethod


def get_open_shift(db: Session, cash_register_id: UUID) -> CashShift | None:
    return db.execute(
        select(CashShift).where(
            CashShift.cash_register_id == cash_register_id,
            CashShift.status == "open",
        )
    ).scalar_one_or_none()


def resolve_dine_in_table_order(db: Session) -> tuple[UUID | None, int | None]:
    """spec 087 (FR-006, A-86): turno/número a asignar a un pedido `DINE_IN`
    nuevo. Un solo turno abierto a la vez por decisión de negocio (research.md);
    si excepcionalmente hubiera más de uno, se toma el de `opened_at` más
    reciente del tenant. Sin turno abierto, el pedido nace sin numerar
    (`None, None`) -- no bloquea la creación del pedido.

    `with_for_update()` bloquea la fila del turno hasta el commit de quien
    llama, serializando el conteo+asignación de dos pedidos creados casi al
    mismo tiempo dentro del mismo turno (evita que ambos lean el mismo
    `COUNT(*)` y choquen contra el índice único de
    `uq_customer_orders_shift_table_order_number`)."""
    shift = db.execute(
        select(CashShift)
        .where(CashShift.status == "open")
        .order_by(CashShift.opened_at.desc())
        .limit(1)
        .with_for_update()
    ).scalar_one_or_none()
    if shift is None:
        return None, None
    count = db.execute(
        select(func.count()).select_from(CustomerOrder).where(
            CustomerOrder.cash_shift_id == shift.id
        )
    ).scalar_one()
    return shift.id, count + 1


def list_shifts(
    db: Session,
    status: str | None,
    cash_register_id: UUID | None,
    page: int,
    size: int,
) -> dict:
    """Histórico de turnos paginado (por defecto los cerrados), más recientes
    primero. Cada fila incluye el nombre de la caja y el arqueo (esperado y
    diferencia) calculados con `reconcile`."""
    stmt = select(CashShift)
    if status:
        stmt = stmt.where(CashShift.status == status)
    if cash_register_id:
        stmt = stmt.where(CashShift.cash_register_id == cash_register_id)
    stmt = stmt.order_by(
        CashShift.closed_at.desc().nullslast(), CashShift.opened_at.desc()
    )

    result = paginate(db, stmt, page, size)

    names = dict(
        db.execute(select(CashRegister.id, CashRegister.name)).all()
    )
    items = []
    for shift in result["items"]:
        recon = reconcile(db, shift)
        items.append({
            "id": shift.id,
            "cash_register_id": shift.cash_register_id,
            "register_name": names.get(shift.cash_register_id, "—"),
            "user_name": shift.user_name,
            "opening_amount": Decimal(shift.opening_amount),
            "counted_amount": shift.counted_amount,
            "opened_at": shift.opened_at,
            "closed_at": shift.closed_at,
            "status": shift.status,
            "close_note": shift.close_note,
            "expected": recon["expected"],
            "difference": recon["difference"],
        })
    result["items"] = items
    return result


def reconcile(db: Session, shift: CashShift) -> dict:
    # --- Ventas del turno agrupadas por clasificación del método de pago ---
    rows = db.execute(
        select(
            PaymentMethod.id,
            PaymentMethod.name,
            PaymentMethod.type,
            func.coalesce(func.sum(Payment.amount), 0),
            func.count(Payment.id),
        )
        .select_from(Sale)
        .join(Payment, Payment.sale_id == Sale.id)
        .join(PaymentMethod, PaymentMethod.id == Payment.payment_method_id)
        .where(
            Sale.cash_shift_id == shift.id,
            Sale.status == "paid",
        )
        .group_by(PaymentMethod.id, PaymentMethod.name, PaymentMethod.type)
    ).all()

    vendido: dict[UUID, dict] = {}
    by_type: dict[str, Decimal] = {"cash": Decimal(0), "card": Decimal(0), "transfer": Decimal(0)}
    for method_id, method_name, method_type, total, count in rows:
        total = Decimal(total)
        vendido[method_id] = {
            "method_id": method_id,
            "method_name": method_name,
            "method_type": method_type,
            "total": total,
            "count": count,
        }
        if method_type in by_type:
            by_type[method_type] += total

    # Los métodos sin ventas también salen, en cero: el arqueo tiene que enseñar
    # todos los medios de cobro del negocio, no solo los que se usaron hoy. Con el
    # INNER JOIN de arriba, "Tarjeta" sin ventas simplemente no existía.
    activos = db.execute(
        select(PaymentMethod).where(PaymentMethod.active.is_(True))
    ).scalars().all()
    for method in activos:
        vendido.setdefault(method.id, {
            "method_id": method.id,
            "method_name": method.name,
            "method_type": method.type,
            "total": Decimal(0),
            "count": 0,
        })

    # El efectivo primero (es el único que afecta al cajón), el resto por nombre.
    sales_by_method = sorted(
        vendido.values(),
        key=lambda m: (m["method_type"] != "cash", m["method_name"].lower()),
    )

    # El cambio (RF-029) sale del cajón, así que el efectivo neto de ventas es
    # Σ pagos en efectivo − Σ cambio entregado.
    change_total = Decimal(db.execute(
        select(func.coalesce(func.sum(Sale.change_given), 0)).where(
            Sale.cash_shift_id == shift.id, Sale.status == "paid"
        )
    ).scalar_one())

    ventas_efectivo = by_type["cash"] - change_total
    ventas_tarjeta = by_type["card"]
    ventas_transferencia = by_type["transfer"]

    # Spec 108: `sales_by_method` debe mostrar venta neta (lo facturado), no el
    # efectivo bruto recibido en mano — se descuenta el cambio a prorrata del
    # bruto de cada método "cash" (normalmente hay uno solo, y entonces queda
    # igual a `ventas_efectivo`).
    cash_rows = [m for m in sales_by_method if m["method_type"] == "cash"]
    if cash_rows and change_total:
        for row in cash_rows:
            share = (
                change_total / len(cash_rows)
                if by_type["cash"] == 0
                else change_total * row["total"] / by_type["cash"]
            )
            row["total"] -= share

    # --- Movimientos manuales agrupados por kind ---
    mov_rows = db.execute(
        select(CashMovement.kind, func.coalesce(func.sum(CashMovement.amount), 0))
        .where(CashMovement.cash_shift_id == shift.id)
        .group_by(CashMovement.kind)
    ).all()
    by_kind = {kind: Decimal(total) for kind, total in mov_rows}
    ingresos = by_kind.get("ingreso", Decimal(0))
    egresos = by_kind.get("egreso", Decimal(0))
    retiros = by_kind.get("retiro", Decimal(0))

    # Solo el efectivo entra en el esperado del cajón.
    expected = (
        Decimal(shift.opening_amount) + ventas_efectivo + ingresos - egresos - retiros
    )
    difference = None
    if shift.counted_amount is not None:
        difference = Decimal(shift.counted_amount) - expected

    return {
        "cash_shift_id": shift.id,
        "status": shift.status,
        "opening_amount": Decimal(shift.opening_amount),
        "ventas_efectivo": ventas_efectivo,
        "ventas_tarjeta": ventas_tarjeta,
        "ventas_transferencia": ventas_transferencia,
        # Dato informativo: cuánto cambio salió del cajón. Desde spec 108, ya no
        # hace falta para que el desglose cuadre a la vista — las filas de
        # `sales_by_method` también son netas (ver ajuste arriba).
        "cambio_entregado": change_total,
        "sales_by_method": sales_by_method,
        "ingresos": ingresos,
        "egresos": egresos,
        "retiros": retiros,
        "expected": expected,
        "counted_amount": shift.counted_amount,
        "difference": difference,
        # DEPRECADO: alias de ventas_efectivo por compatibilidad.
        "cash_sales": ventas_efectivo,
    }
