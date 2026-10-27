from decimal import Decimal
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.sales.schemas import PaymentIn
from app.core.timezone import UtcDatetime


class BillingMode(str, Enum):
    """Cómo se divide la cuenta al cerrar. Lo elige el cajero al cobrar, no el
    cliente."""
    #: Una sola venta con todo lo consumido en la mesa.
    UNIFIED = "unified"
    #: Una venta por comensal, agrupando por `order_items.participant_id`.
    SPLIT = "split"


class ParticipantResponse(BaseModel):
    id: UUID
    display_name: str
    display_label: str | None = None
    status: str
    joined_at: UtcDatetime
    expires_at: UtcDatetime | None = None
    closed_at: UtcDatetime | None = None

    model_config = ConfigDict(from_attributes=True)


class TableSessionResponse(BaseModel):
    id: UUID
    dining_table_id: UUID
    status: str
    opened_at: UtcDatetime
    closed_at: UtcDatetime | None = None
    closed_by_user_name: str | None = None
    billing_mode: str | None = None
    participants: list[ParticipantResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class ParticipantCreateIn(BaseModel):
    """Comensal que crea el staff para repartir la cuenta.

    No tiene token ni carrito: es una **etiqueta de cobro**, no alguien conectado por
    el QR. Sirve para el caso en que una sola persona pidió por todos y luego cada
    quien quiere pagar lo suyo.
    """
    display_name: str = Field(..., min_length=1, max_length=255)


class ItemAssignmentIn(BaseModel):
    """A quién se le cobra una línea, o parte de ella.

    `participant_id` nulo la deja sin asignar. `quantity` permite repartir las
    unidades de una misma línea entre varias personas: se mandan varias entradas del
    mismo `order_item_id`, y **la suma debe ser exactamente la cantidad de la línea**
    (el servicio lo verifica). Omitirlo significa "la línea entera".
    """
    order_item_id: UUID
    participant_id: UUID | None = None
    quantity: int | None = Field(None, ge=1)


class AssignmentsIn(BaseModel):
    """Reparto en lote.

    Va en lote y no ítem a ítem porque el cajero reparte varias líneas de una vez: si
    cada una fuera su propia petición, un fallo a mitad dejaría la cuenta repartida a
    medias y el cobro cuadraría mal.
    """
    assignments: list[ItemAssignmentIn] = Field(..., min_length=1)


class SessionBillItem(BaseModel):
    """Ítem consumido por un comensal, para el detalle de la cuenta (spec 026,
    FR-006) — mismos datos que ya calcula `checkout.order_sale_lines`, solo
    expuestos; no cambia cómo se valoran."""
    description: str
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal


class SessionBillLine(BaseModel):
    """Lo que debe un comensal. `participant_id` nulo agrupa lo que añadió el
    staff sin asignar a nadie."""
    participant_id: UUID | None = None
    display_label: str | None = None
    subtotal: Decimal
    #: Detalle de ítems y descuento ya aplicado (spec 026, FR-006) — mismos
    #: valores que `compute_bill` ya calculaba internamente para llegar a
    #: `subtotal`, ahora expuestos en vez de descartarse.
    items: list[SessionBillItem] = Field(default_factory=list)
    discount: Decimal = Decimal("0")


class SessionBillResponse(BaseModel):
    table_session_id: UUID
    dining_table_id: UUID
    total: Decimal
    order_ids: list[UUID] = Field(default_factory=list)
    split: list[SessionBillLine] = Field(default_factory=list)
    # spec 123: saldo pendiente acumulado de todas las órdenes no canceladas
    # de la sesión (adiciones agregadas después de facturar, aún sin cobrar).
    # Campo aditivo: 0 cuando no hay saldo pendiente.
    pending_balance: Decimal = Decimal("0")


class SplitPaymentIn(BaseModel):
    """Pago de un comensal concreto en el modo `split`."""
    participant_id: UUID | None = Field(
        None,
        description="Null para cobrar los ítems sin comensal asignado "
                    "(los que añadió el mesero).",
    )
    payments: list[PaymentIn] = Field(..., min_length=1)
    discount: Decimal = Field(Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    tax: Decimal = Field(Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    tip: Decimal = Field(Decimal("0"), ge=0, max_digits=12, decimal_places=2)


class CloseSessionIn(BaseModel):
    cash_shift_id: UUID
    billing_mode: BillingMode

    # --- unified ---
    payments: list[PaymentIn] = Field(
        default_factory=list,
        description="Solo en billing_mode='unified': pagos de la cuenta completa.",
    )
    discount: Decimal = Field(Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    tax: Decimal = Field(Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    tip: Decimal = Field(Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    customer_name: str | None = Field(
        None,
        max_length=255,
        description="Solo en billing_mode='unified': a nombre de quién va la factura. "
                    "Si se omite se usan los comensales de la sesión, y si no hay, la mesa. "
                    "En 'split' cada venta va a nombre de su comensal.",
    )

    # --- split ---
    splits: list[SplitPaymentIn] = Field(
        default_factory=list,
        description="Solo en billing_mode='split': un bloque de pago por comensal. "
                    "Debe cubrir a todos los que tengan consumo.",
    )

    # spec 125 (FR-007): id de la pestaña/terminal que origina el cierre, para que
    # esa misma terminal pueda silenciar su propio aviso de pago en tiempo real sin
    # dejar de notificar a las demás. Opaco, opcional y retrocompatible — sin él,
    # ninguna terminal se silencia (comportamiento actual). Aplica a todas las
    # ventas emitidas por este cierre (unificado o una por comensal en dividido).
    origin_client_id: str | None = Field(None, max_length=64)


class CloseSessionResponse(BaseModel):
    table_session: TableSessionResponse
    sale_ids: list[UUID] = Field(default_factory=list)


class ReleaseSessionResponse(BaseModel):
    """Respuesta de `POST /table-sessions/{id}/release` (spec 028, T027/T028):
    la sesión ya estaba completamente pagada (nada billable pendiente), así
    que aquí solo se confirma que la mesa quedó libre — no hay venta que
    reportar, a diferencia de `CloseSessionResponse`."""
    dining_table_id: UUID
    status: str
