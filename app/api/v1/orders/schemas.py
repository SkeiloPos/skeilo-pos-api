from enum import Enum
from typing import Literal
from uuid import UUID
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.catalog.schemas import OptionSelectionIn
from app.api.v1.sales.schemas import PaymentIn
from app.core.schema_types import AssetUrl
from app.core.timezone import UtcDatetime


class OrderChannel(str, Enum):
    """Canal de origen del pedido, estandarizado (spec 055). `COUNTER`/`WAITER`
    (personal del punto de venta) y `QR` se fusionaron/renombraron: `POS`
    cubre tanto el mostrador/cajero como al mesero (Terminal de Mesas, modo
    híbrido) — ver `app/api/v1/orders/consolidation.py` para cómo se preserva
    esa distinción internamente sin exponerla aquí."""
    POS = "POS"
    QR_MENU = "QR_MENU"
    WHATSAPP = "WHATSAPP"
    API = "API"


class OrderType(str, Enum):
    """Cómo se atiende el pedido (spec 055)."""
    DINE_IN = "DINE_IN"
    TAKEAWAY = "TAKEAWAY"
    DELIVERY = "DELIVERY"


class OrderStatus(str, Enum):
    """Ciclo del pedido. El estado de cocina es por ítem (`estado_cocina`)."""
    #: Enviada por el comensal desde el QR; todavía no descuenta inventario.
    RECIBIDA = "recibida"
    #: Confirmada por staff: aquí se descontó el stock.
    ABIERTA = "abierta"
    BLOQUEADA = "bloqueada"
    PAGADA = "pagada"
    CANCELADA = "cancelada"


class KitchenStatus(str, Enum):
    """Estado de preparación por ítem, independiente del status de pago."""
    PENDIENTE = "pendiente"
    EN_PREPARACION = "en_preparacion"
    LISTO = "listo"
    ANULADO = "anulado"


# ---------- Mesas ----------
class TableCreate(BaseModel):
    number: int = Field(..., ge=1)
    name: str | None = Field(None, max_length=255)


class TableUpdate(BaseModel):
    name: str | None = Field(None, max_length=255)
    active: bool | None = None


class TableResponse(BaseModel):
    id: UUID
    number: int
    name: str | None = None
    qr_token: UUID
    active: bool
    status: str

    model_config = ConfigDict(from_attributes=True)


# ---------- Mesas avanzado (RF-051..053) ----------
class TableStatusUpdate(BaseModel):
    status: Literal["libre", "ocupada", "reservada", "pendiente_pago"]


class MoveOrderIn(BaseModel):
    dining_table_id: UUID


class MergeOrdersIn(BaseModel):
    order_ids: list[UUID] = Field(..., min_length=2)


class MergeResponse(BaseModel):
    merged_group_id: UUID
    order_ids: list[UUID]


class GroupBillOrderLine(BaseModel):
    order_id: UUID
    dining_table_id: UUID | None = None
    status: str
    subtotal: Decimal


class GroupBillResponse(BaseModel):
    merged_group_id: UUID
    total: Decimal
    orders: list[GroupBillOrderLine]


class TableQrTokenResponse(BaseModel):
    """Token firmado (tenant_id + table_id) para imprimir en el QR de la mesa,
    junto al path público del menú que lo consume."""
    table_id: UUID
    number: int
    qr_token: str
    menu_path: str


# ---------- Comandas ----------
class OrderItemIn(BaseModel):
    # spec 063 (FR-024): el mecanismo de combo se retira; `combo_id` ya no se acepta.
    product_variant_id: UUID
    quantity: int = Field(1, ge=1)
    # spec 065: reemplaza `option_ids: list[UUID]` -- cada entrada trae su propia
    # cantidad elegida (default 1, el mismo significado que tenía "incluir este id").
    options: list[OptionSelectionIn] = Field(default_factory=list)
    notes: str | None = Field(None, max_length=500)


class OrderCreate(BaseModel):
    channel: OrderChannel = OrderChannel.POS
    #: Cómo se atiende el pedido (spec 055). Validado contra `channel` en
    #: `orders.service.create_order` (no toda combinación tiene sentido de
    #: negocio — p. ej. WHATSAPP nunca admite DINE_IN).
    order_type: OrderType = OrderType.DINE_IN
    participant_id: UUID | None = None
    dining_table_id: UUID | None = None
    customer_name: str | None = Field(None, max_length=255)
    #: Solo aplican (y son obligatorios, salvo el teléfono) cuando
    #: order_type == DELIVERY — validado en orders.service.create_order, no
    #: aquí, porque la obligatoriedad depende del valor de order_type (spec 056).
    delivery_address: str | None = Field(None, max_length=255)
    delivery_phone: str | None = Field(None, max_length=30)
    delivery_fee: Decimal | None = Field(None, ge=0)
    notes: str | None = Field(None, max_length=500)
    items: list[OrderItemIn] = Field(..., min_length=1)
    #: Terminal de Mesas modo híbrido (spec 028): comanda de mostrador/mesero
    #: que nace en 'recibida' en lugar de 'abierta' — el staff cobra primero
    #: (`POST /orders/{id}/checkout-and-send`) y recién ahí se descuenta
    #: inventario y se envía a cocina. Solo aplica a `channel=POS`; combinado
    #: con `channel=QR_MENU` es 400 (ese canal ya tiene su propio flujo
    #: `recibida` vía `/cart/submit`).
    hold_for_payment: bool = False
    #: spec 111 (FR-011, RN4): generado por el cliente una sola vez por intento de "Crear
    #: pedido"; reenviado sin cambios si ese mismo intento se reintenta por una falla de red.
    #: Opcional y retrocompatible — sin él, cada solicitud sigue creando un pedido nuevo como
    #: hasta ahora (ver contracts/post-orders.md de esa spec).
    client_request_id: str | None = Field(None, max_length=64)


class OrderItemOptionResponse(BaseModel):
    id: UUID
    option_id: UUID
    quantity: int
    # spec 089 (A-94): la opción es un adicional cobrado/consumido una vez por línea.
    per_line: bool = False
    # spec 087 (FR-015, A-89): nombre de la opción y de su grupo, resueltos por
    # JOIN en lectura (`OrderItemOption.name`/`group_name`). Opcionales: un
    # `OrderItemOption` recién creado y devuelto sin recargar los trae en `None`.
    name: str | None = None
    group_name: str | None = None

    model_config = ConfigDict(from_attributes=True)


class OrderItemResponse(BaseModel):
    id: UUID
    product_variant_id: UUID
    participant_id: UUID | None = None
    quantity: int
    unit_price: Decimal
    # spec 089 (A-94): adicionales cobrados UNA vez por línea (0 en toda línea histórica y
    # en las de la terminal POS) y el total de la línea sin descuento, que antes el cliente
    # calculaba como `unit_price × quantity`: `unit_price × quantity + addons_total`.
    addons_total: Decimal = Decimal("0")
    line_total: Decimal
    # Snapshot del descuento vigente al confirmar (spec 038, FR-013), mismos
    # nombres/semántica que `CartItemResponse`: `None` si ninguna promoción
    # aplicó a la línea (o es un combo), o si el pedido es anterior a esta
    # spec (columnas nuevas, sin backfill — FR-015).
    discounted_unit_price: Decimal | None = None
    discounted_line_total: Decimal | None = None
    estado_cocina: str
    void_de: UUID | None = None
    notes: str | None = None
    combo_id: UUID | None = None
    options: list[OrderItemOptionResponse] = Field(default_factory=list)
    #: Versión del evento de tiempo real que emitió esta escritura. Solo lo
    #: rellena `PATCH /orders/items/{id}/kitchen`; el KDS lo usa para descartar
    #: eventos en vuelo que revertirían su parche optimista. `None` si el evento
    #: no llegó a publicarse (Redis caído): el cliente sigue funcionando, solo
    #: pierde el desempate y se apoya en el guard `busy`.
    rt_v: int | None = None

    model_config = ConfigDict(from_attributes=True)


class CurrentPaymentAttemptSummary(BaseModel):
    """Resumen del intento de pago vigente de una orden, para el comensal
    (spec 024). **Nunca** incluye `rejection_reason` (Clarification 3) — el
    detalle del motivo solo lo ve el cajero, vía
    `GET /orders/{order_id}/payment-attempts` (`PaymentAttemptResponse`)."""
    id: UUID
    status: str
    payment_method_name: str
    is_cash: bool
    # spec 088 (FR-008): la columna guarda la key; se ensambla la URL al serializar.
    receipt_file_url: AssetUrl = None


class AppliedPromotionOut(BaseModel):
    """Una entrada del snapshot `applied_promotions` (spec 094, FR-011).

    Es **una entrada por regla**, no por promoción: `applied_to_dicts`
    (`promotions/service.py:126-131,314-323`) produce una por cada regla que
    descontó, y `amount` es el descuento agregado de ESA regla. Por eso la
    etiqueta de la fila de descuento agrupa por `promotion_id` antes de decidir
    (research.md D6).

    `promotion_id` y `name` son nulables por lectura tolerante del JSONB
    histórico. `rule_id` existe en el JSONB y **no se publica**: no aporta nada
    a esta pantalla y expone un detalle interno del motor de promociones
    (data-model.md §2)."""
    promotion_id: UUID | None = None
    name: str | None = None
    amount: Decimal = Decimal("0")


class OrderBillingSummary(BaseModel):
    """Desglose económico del pedido, ya resuelto por el servidor (spec 094,
    FR-024b).

    Es un **modelo de lectura**: no se persiste, no se cachea y no tiene
    identidad. La pantalla pinta lo que recibe — no elige entre factura y
    pedido, no suma y no resta (RN-001).

    `state` dice cuál de los tres casos de facturación es y `source` de dónde
    salieron los importes: `"factura"` **solo** con `state = "factura_propia"`,
    donde FR-024 manda leer de la `Sale` para que la igualdad con el módulo de
    Ventas se cumpla por construcción."""
    state: Literal["sin_factura", "factura_propia", "factura_agrupada"]
    source: Literal["pedido", "factura"]
    subtotal: Decimal
    discount: Decimal
    #: Nombre de la única promoción que explica el descuento, o `None` cuando la
    #: UI debe usar la etiqueta "Descuento" (FR-009, FR-010, research.md D6).
    discount_label: str | None = None
    delivery_fee: Decimal
    total: Decimal
    promotions: list[AppliedPromotionOut] = Field(default_factory=list)
    #: Condición literal de FR-018 (research.md D8): hay ≥ 1 ítem no anulado y
    #: la suma de sus `line_total` es `0`. Se calcula en el servidor para que la
    #: condición del aviso sea verificable en el contrato, no en la plantilla.
    sin_detalle_de_precios: bool = False


class OrderResponse(BaseModel):
    id: UUID
    channel: str
    order_type: str | None = None
    status: str
    version: int
    table_session_id: UUID | None = None
    participant_id: UUID | None = None
    dining_table_id: UUID | None = None
    customer_name: str | None = None
    delivery_address: str | None = None
    delivery_phone: str | None = None
    delivery_fee: Decimal | None = None
    notes: str | None = None
    # spec 087 (FR-006, A-86): número de pedido de mesa, estable dentro del
    # turno de caja en que se creó. `None` para TAKEAWAY/DELIVERY y para
    # pedidos creados antes de esta spec (nunca se recalcula ni se migra).
    table_order_number: int | None = None
    created_at: UtcDatetime
    items: list[OrderItemResponse] = Field(default_factory=list)
    # Intento de pago más reciente (spec 024) — `None` si nunca se inició
    # ninguno. Mientras no haya uno `confirmado`, la orden sigue "pendiente de
    # pago" para el comensal (Key Entity `Orden`, no es una columna de status).
    current_payment_attempt: CurrentPaymentAttemptSummary | None = None
    # Computado (spec 029) — no es una columna: verdadero si ya existe una
    # `Sale` con `customer_order_id` igual al de esta orden. Es la señal real
    # de "ya está pagado": a diferencia de `status`, que nunca llega a
    # "pagada" en los caminos QR/mostrador vigentes. El router lo asigna
    # antes de serializar (`orders.service.order_has_sale`/`paid_order_ids`).
    paid: bool = False
    # Spec 076, Historia 4: nombre para mostrar del usuario de staff (Cajero o
    # Mesero, sin distinción de rol) que creó este pedido — computado, no una
    # columna nueva: resuelve `CustomerOrder.user_id` (ya existente) contra
    # `shared.users`. `None` si el pedido lo envió el cliente por QR
    # (`user_id` nulo) o si `user_id` no resuelve a ningún usuario. El router
    # lo asigna antes de serializar (`orders.service.staff_user_names`), mismo
    # patrón que `paid`.
    staff_user_name: str | None = None
    # spec 094 (FR-011): los dos campos CRUDOS del pedido — el descuento
    # agregado que el cobro congeló y la lista de promociones aplicadas con su
    # nombre y monto. Son el dato *del pedido*, que no siempre coincide con el
    # que se muestra (cuando manda la factura, research.md D4): por eso FR-011
    # se cumple publicándolos aparte y no reutilizando `billing`. Cero consultas
    # extra: son columnas ya cargadas en el mismo SELECT. La pantalla del
    # Detalle de Orden **no los consume** (D16) — su única fuente es `billing`.
    discount: Decimal = Decimal("0")
    applied_promotions: list[AppliedPromotionOut] = Field(default_factory=list)
    # spec 094 (FR-024b): desglose ya resuelto. **Solo lo rellena el detalle**
    # (`GET /orders/{id}`, vía `_load_order`); en el listado viaja `null` a
    # propósito, para no añadir una consulta por fila sobre hasta 100 pedidos
    # por página (research.md D3, guardia de N+1 en `test_orders_pagination`).
    # Un `null` NO significa "cero": la pantalla trata la ausencia como "no
    # pintar el resumen" (D15).
    billing: OrderBillingSummary | None = None

    model_config = ConfigDict(from_attributes=True)


# ---------- Intentos de pago (spec 024) ----------
class PaymentAttemptResponse(BaseModel):
    """Vista de staff/cajero — a diferencia de `CurrentPaymentAttemptSummary`,
    **sí** incluye `rejection_reason` (FR-016)."""
    id: UUID
    order_id: UUID
    payment_method_id: UUID
    payment_method_name: str
    is_cash: bool
    status: str
    amount_received: Decimal | None = None
    change_amount: Decimal | None = None
    # spec 088 (FR-008): alimenta "Pagos por confirmar" del cajero; la columna guarda la key
    # y aquí se ensambla la URL lista para renderizar (tolerancia de lectura a filas antiguas).
    receipt_file_url: AssetUrl = None
    rejection_reason: str | None = None
    resolved_by_user_id: UUID | None = None
    resolved_at: UtcDatetime | None = None
    created_at: UtcDatetime

    model_config = ConfigDict(from_attributes=True)


class PaymentAttemptRejectIn(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


class PaymentAttemptApproveIn(BaseModel):
    """Spec 028: aprobar ya genera la venta/factura en la misma llamada, así
    que necesita el turno de caja donde registrarla — mismo campo que
    `PayIn`/`CheckoutAndSendIn`."""
    cash_shift_id: UUID


class PaymentAttemptConfirmCashIn(BaseModel):
    amount_received: Decimal = Field(..., gt=0, max_digits=12, decimal_places=2)
    # Spec 028: ver `PaymentAttemptApproveIn`.
    cash_shift_id: UUID


# ---------- Preparación ----------
class KitchenTransitionIn(BaseModel):
    estado_cocina: KitchenStatus


# ---------- Anulación / reemplazo de ítem ----------
class VoidItemIn(BaseModel):
    motivo: str = Field(..., min_length=1, max_length=500)
    replacement: OrderItemIn | None = None


# ---------- Cobro / cancelación (Fase 7) ----------
class BlockIn(BaseModel):
    version: int = Field(..., ge=0, description="Versión esperada (lock optimista).")


class CancelIn(BaseModel):
    motivo: str = Field(..., min_length=1, max_length=500)


class PayIn(BaseModel):
    cash_shift_id: UUID
    discount: Decimal = Field(0, ge=0, max_digits=12, decimal_places=2)
    tax: Decimal = Field(0, ge=0, max_digits=12, decimal_places=2)
    tip: Decimal = Field(0, ge=0, max_digits=12, decimal_places=2)
    payments: list[PaymentIn] = Field(..., min_length=1)


class CheckoutAndSendIn(BaseModel):
    """Cobra y envía a cocina, en un solo paso, una comanda creada con
    `hold_for_payment=True` (`POST /orders/{order_id}/checkout-and-send`).
    Mismos campos que `PayIn` más `version` (lock optimista, igual que
    `BlockIn`) y el nombre para la factura."""
    version: int = Field(..., ge=0, description="Versión esperada (lock optimista).")
    cash_shift_id: UUID
    # spec 029 (Historia 2, FR-009/010/011): descuento manual prohibido sin
    # excepción en la Terminal de Mesas — único valor válido es 0. El motor
    # de promociones (`promotions.evaluate`/`combo_discount_for_lines`) sigue
    # sumándose aparte en `checkout_and_send`, sin relación con este campo.
    # No se toca el `discount` compartido de `sales/schemas.py` (mostrador/
    # cierre unificado/dividido) — ese es alcance de spec 011.
    discount: Decimal = Field(0, ge=0, le=0, max_digits=12, decimal_places=2)
    tax: Decimal = Field(0, ge=0, max_digits=12, decimal_places=2)
    tip: Decimal = Field(0, ge=0, max_digits=12, decimal_places=2)
    payments: list[PaymentIn] = Field(..., min_length=1)
    billing_customer_name: str | None = Field(
        None, max_length=255,
        description="A nombre de quién va la factura. Si se omite, 'Consumidor Final'.",
    )


class BillItemLine(BaseModel):
    order_item_id: UUID
    product_variant_id: UUID
    participant_id: UUID | None = None
    quantity: int
    unit_price: Decimal
    line_total: Decimal
    estado_cocina: str


class BillOrderLine(BaseModel):
    order_id: UUID
    status: str
    subtotal: Decimal
    items: list[BillItemLine] = Field(default_factory=list)


class BillSessionLine(BaseModel):
    participant_id: UUID | None = None
    display_label: str | None = None
    subtotal: Decimal


class BillResponse(BaseModel):
    dining_table_id: UUID
    total: Decimal
    orders: list[BillOrderLine] = Field(default_factory=list)
    split: list[BillSessionLine] = Field(default_factory=list)


# ---------- Preview de cobro (spec 073) ----------
class CheckoutPreviewResponse(BaseModel):
    """Desglose autoritativo de un cobro — lo que el panel de la Terminal
    muestra y valida (spec 073, FR-001/FR-004). Forma común de
    `GET /orders/{order_id}/checkout-preview` (pedido ya creado) y de
    `POST /orders/draft-preview` (borrador sin guardar). Calculado con el mismo
    motor que emite la venta (`order_sale_lines`/`auto_discount`/`compute_total`),
    nunca replicado en el navegador."""
    subtotal: Decimal
    #: Descuento automático por promoción (spec 063), siempre ≥ 0.
    discount: Decimal
    #: Valor del domicilio del pedido (0 si no es DELIVERY o no tiene valor
    #: cargado). El frontend pinta la fila solo cuando es > 0 (FR-004).
    delivery_fee: Decimal
    #: `max(0, subtotal - discount + delivery_fee)` — misma fórmula que
    #: `build_sale` (research.md D6).
    total: Decimal
    #: Instante contra el que se evaluó la vigencia temporal de las promociones
    #: (aware UTC). En `checkout-preview` es el instante congelado del pedido (o
    #: la hora de la llamada para pedidos anteriores a esta spec); en
    #: `draft-preview` es siempre la hora de la llamada (FR-008).
    promotion_evaluated_at: UtcDatetime


class DraftPreviewIn(BaseModel):
    """Cuerpo de `POST /orders/draft-preview` (spec 073, FR-013): las mismas
    líneas que `OrderCreate.items`, para que el frontend arme este cuerpo con
    el mismo borrador que luego usará para el `POST /orders` real."""
    items: list[OrderItemIn] = Field(..., min_length=1)
    #: Igual que `OrderCreate.delivery_fee` — el frontend lo manda solo si el
    #: borrador es de tipo domicilio (FR-003).
    delivery_fee: Decimal | None = Field(None, ge=0)
