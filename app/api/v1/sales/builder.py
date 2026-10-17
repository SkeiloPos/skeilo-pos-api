"""Construcción de una venta, compartida por los dos caminos de cobro.

Antes había dos implementaciones paralelas y divergentes de lo mismo — una en
`sales/service.py:checkout` (mostrador) y otra en `orders/checkout.py:pay_order`
(mesa) — con ~80 líneas duplicadas. Divergían en detalles que costaban dinero: el
cobro de mesa no seteaba `paid_amount`/`change_given`, así que el cambio entregado
no se descontaba del efectivo esperado y el arqueo del turno quedaba descuadrado.

Lo que **sigue siendo distinto por diseño** es el descuento de inventario, y por eso
no vive aquí:

- mostrador: descuenta al vender (`deduct_sale`), porque no hubo paso previo;
- mesa: ya descontó al confirmar el pedido, así que cobrar no vuelve a descontar.

Quien llama decide; `build_sale` nunca toca stock.
"""
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.crud import get_or_404
from app.core.models import User
from app.models.cash_shift import CashShift
from app.models.payment import Payment, PaymentMethod
from app.models.sale import Sale, SaleItem


class SaleLine:
    """Línea ya resuelta y valorada, lista para convertirse en `SaleItem`.

    El precio llega calculado por el caller (snapshot de la variante + extras de
    opción, o el precio congelado del `order_item`), porque cada camino lo obtiene
    de un sitio distinto."""

    __slots__ = ("product_variant_id", "description", "options", "quantity",
                 "unit_price", "addons_total", "line_total", "combo_id", "line_id")

    def __init__(
        self,
        *,
        product_variant_id: UUID,
        description: str,
        options: list[dict],
        quantity: int,
        unit_price: Decimal,
        addons_total: Decimal = Decimal(0),
        combo_id: UUID | None = None,
        line_id: UUID | None = None,
    ) -> None:
        self.product_variant_id = product_variant_id
        self.description = description
        self.options = options
        self.quantity = quantity
        self.unit_price = Decimal(unit_price)
        # spec 089 (A-94): adicionales cobrados una vez por línea (0 en toda línea
        # histórica, de la terminal POS y del mostrador).
        self.addons_total = Decimal(addons_total or 0)
        self.line_total = self.unit_price * Decimal(quantity) + self.addons_total
        # Combo (selección explícita) al que pertenece esta línea, si aplica.
        self.combo_id = combo_id
        # `id` de la fila de origen (`order_items` / `cart_items`), cuando existe:
        # desempate determinista del reparto por presentación (spec 040, FR-011).
        # `None` en la venta de mostrador (aún no hay fila persistida).
        self.line_id = line_id

    @property
    def base_unit_price(self) -> Decimal:
        """FR-027 (spec 083): `unit_price` sin el precio de los toppings/
        adicionales elegidos (`options`, spec 064/065) — la base sobre la que
        una promoción de tipo `percent` debe aplicar su descuento.

        spec 089: solo se restan las opciones que van por unidad (`per_line` ausente o
        falso). Los adicionales `per_line` no están dentro de `unit_price` (van en
        `addons_total`), así que no hay nada que restar y la promoción no los descuenta."""
        toppings = sum(
            (
                Decimal(opt["extra_price"]) * opt.get("quantity", 1)
                for opt in self.options if not opt.get("per_line", False)
            ),
            Decimal(0),
        )
        return self.unit_price - toppings


def ensure_open_shift(db: Session, cash_shift_id: UUID) -> CashShift:
    shift = get_or_404(db, CashShift, cash_shift_id, "Shift not found")
    if shift.status != "open":
        raise HTTPException(status.HTTP_409_CONFLICT, "El turno de caja está cerrado")
    return shift


def compute_total(
    subtotal: Decimal,
    discount: Decimal = Decimal("0"),
    tax: Decimal = Decimal("0"),
    tip: Decimal = Decimal("0"),
    delivery_fee: Decimal = Decimal("0"),
) -> Decimal:
    """Fórmula única del total de un cobro (spec 073, research.md D6): la misma
    aritmética que `build_sale` aplica al emitir la venta, extraída aquí para
    que los previews de cobro (`compute_checkout_preview`/`compute_draft_preview`)
    no dupliquen la suma/resta — cualquier cambio futuro (un impuesto que deje
    de ser $0) los mantiene en sincronía por construcción.

    Devuelve la aritmética cruda `subtotal - discount + tax + tip + delivery_fee`
    (puede ser negativa): `build_sale` conserva su propio `422` de total
    negativo, y los previews aplican el `max(0, …)` de FR-004/Edge Case."""
    return (
        Decimal(subtotal)
        - Decimal(discount)
        + Decimal(tax)
        + Decimal(tip)
        + Decimal(delivery_fee)
    )


def build_sale(
    db: Session,
    *,
    lines: list[SaleLine],
    shift: CashShift,
    cashier: User,
    payments: list,
    discount: Decimal = Decimal("0"),
    tax: Decimal = Decimal("0"),
    tip: Decimal = Decimal("0"),
    delivery_fee: Decimal = Decimal("0"),
    customer_name: str | None = None,
    dining_table_id: UUID | None = None,
    table_session_id: UUID | None = None,
    participant_id: UUID | None = None,
    customer_order_id: UUID | None = None,
    promotion_id: UUID | None = None,
    applied_promotions: list[dict] | None = None,
    promotion_evaluated_at: datetime | None = None,
    invoice_prefix: str = "",
    kind: str = "original",
) -> Sale:
    """Arma `Sale` + `SaleItem` + `Payment`, deja la venta en `paid` y **emite su
    factura**.

    `kind` (spec 123): `'original'` (default, sin cambio para ningún llamador
    existente) o `'complementario'` (pago que liquida un saldo pendiente
    generado por adiciones agregadas después de que el pedido ya tenía una
    `Sale` -- `sales/complementary.py::settle_pending_balance`).

    Valida que el total no sea negativo y que el pago lo cubra. **No hace commit
    ni toca inventario**: se une a la transacción del caller.

    La factura se emite aquí y no en cada camino de cobro por la misma razón que
    todo lo demás vive aquí: hay cuatro formas de cobrar (mostrador, cierre
    unificado, cierre dividido y el `pay_order` legacy) y hacerlo en cada una
    garantizaba que alguna se quedara fuera —que es exactamente lo que pasaba
    cuando facturar dependía de que alguien pulsara un botón.
    """
    if not lines:
        raise HTTPException(status.HTTP_409_CONFLICT, "La venta no tiene ítems cobrables")

    sale = Sale(
        cash_shift_id=shift.id,
        dining_table_id=dining_table_id,
        table_session_id=table_session_id,
        participant_id=participant_id,
        customer_order_id=customer_order_id,
        user_id=cashier.id,
        user_name=cashier.name,
        customer_name=customer_name,
        discount=discount,
        tax=tax,
        tip=tip,
        delivery_fee=delivery_fee,
        promotion_id=promotion_id,
        # spec 063 (FR-021): snapshot de las promociones que descontaron.
        applied_promotions=applied_promotions or [],
        # spec 073 (FR-011a, A-70): el instante con el que se evaluó la vigencia
        # temporal de estas promociones — lo pasa el call site (la salida de
        # `promotion_evaluation_instant`). `None` en ventas sin ningún pedido
        # congelado detrás (venta de mostrador directa, histórico) → columna NULL.
        promotion_evaluated_at=promotion_evaluated_at,
        status="issued",
        kind=kind,
    )
    db.add(sale)
    db.flush()

    subtotal = Decimal("0")
    for line in lines:
        subtotal += line.line_total
        db.add(SaleItem(
            sale_id=sale.id,
            product_variant_id=line.product_variant_id,
            description=line.description,
            options=line.options,
            quantity=line.quantity,
            unit_price=line.unit_price,
            line_total=line.line_total,
            combo_id=line.combo_id,
        ))

    # Spec 056, FR-011: el valor del domicilio (si la orden es DELIVERY) se
    # suma al total igual que tax/tip — 0 para cualquier otra orden (FR-012).
    # spec 073, research.md D6: misma fórmula que los previews de cobro, vía
    # `compute_total` — el `422` de total negativo se conserva aquí.
    total = compute_total(subtotal, discount, tax, tip, delivery_fee)
    if total < 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "El total no puede ser negativo"
        )

    paid = Decimal("0")
    no_efectivo = Decimal("0")
    for p in payments:
        method = get_or_404(db, PaymentMethod, p.payment_method_id, "Payment method not found")
        db.add(Payment(
            sale_id=sale.id, payment_method_id=p.payment_method_id,
            amount=p.amount, reference=p.reference,
        ))
        paid += Decimal(p.amount)
        if not method.is_cash:
            no_efectivo += Decimal(p.amount)
    if paid < total:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"El pago ({paid}) no cubre el total ({total})",
        )
    # Con pago mixto el exceso solo puede venir del efectivo: `change_given` se
    # calcula abajo como `paid - total` sin mirar el método, y `reconcile` se lo
    # descuenta al efectivo. Un cobro electrónico por encima del total dejaría un
    # faltante fantasma en el cajón que nadie podría explicar al cerrar el turno.
    if no_efectivo > total:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Los pagos que no son en efectivo ({no_efectivo}) no pueden superar el "
            f"total ({total}): el vuelto solo sale del efectivo.",
        )

    sale.subtotal = subtotal
    sale.total = total
    # RF-029: `reconcile` calcula el efectivo esperado como
    # Σ pagos en efectivo − Σ change_given. Sin estos dos campos el cambio
    # entregado nunca se descuenta y el arqueo del turno queda descuadrado.
    sale.paid_amount = paid
    sale.change_given = paid - total
    sale.status = "paid"

    # Factura: en la misma transacción, con los totales ya cerrados. Import local
    # para no crear un ciclo (invoices importa modelos de ventas).
    from app.api.v1.invoices.service import issue_for_sale

    issue_for_sale(db, sale, user=cashier, prefix=invoice_prefix)
    return sale
