from enum import Enum
from uuid import UUID
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.v1.catalog.schemas import OptionSelectionIn
from app.core.storage import normalize_asset_ref
from app.core.timezone import UtcDatetime


def _normalize_payment_info_assets(info: dict[str, str] | None) -> dict[str, str] | None:
    """spec 080 (FR-002/FR-004): normaliza a key todo valor de `payment_info`
    que sea una URL absoluta del bucket gestionado (típ. la clave marcada
    `format:"image"`, p. ej. `qr`). Los valores que no lo son —`celular`,
    `cuenta`, `titular`— quedan **idénticos**: `normalize_asset_ref` solo toca
    los que empiezan por un prefijo del bucket gestionado."""
    if not info:
        return info
    return {k: (normalize_asset_ref(v) or v) for k, v in info.items()}


# ---------- Métodos de pago ----------
class PaymentMethodType(str, Enum):
    CASH = "cash"
    CARD = "card"
    TRANSFER = "transfer"
    OTHER = "other"


class PaymentMethodCreate(BaseModel):
    """Activa un método del catálogo del Super Admin para este tenant (spec
    032, FR-007/FR-011). `name`/`type`/`is_cash` ya no se aceptan aquí — se
    copian de `catalog_id` en `service.py` (research.md Decisión 5); un tenant
    no puede crear métodos fuera del catálogo."""

    catalog_id: UUID
    # Datos de integración del método (cuenta, celular, código...); validados
    # contra `catalog.fields` en `service.py` (FR-009). `None`/`{}` para
    # métodos sin campos (ej. Efectivo).
    payment_info: dict[str, str] | None = None

    @field_validator("payment_info")
    @classmethod
    def _normalize_assets(cls, info: dict[str, str] | None) -> dict[str, str] | None:
        return _normalize_payment_info_assets(info)


class PaymentMethodUpdate(BaseModel):
    payment_info: dict[str, str] | None = None
    # spec 088 (FR-002, research D5/D7): `payment_info` que el formulario mostraba al
    # abrirse (basta con las claves de imagen). Una imagen enviada solo cuenta como
    # cambio si su valor base coincide con el vigente; "no enviado" != `null`.
    payment_info_base: dict[str, str] | None = None
    active: bool | None = None

    @field_validator("payment_info", "payment_info_base")
    @classmethod
    def _normalize_assets(cls, info: dict[str, str] | None) -> dict[str, str] | None:
        return _normalize_payment_info_assets(info)


class PaymentMethodResponse(BaseModel):
    id: UUID
    catalog_id: UUID | None = None
    name: str
    type: str
    is_cash: bool
    active: bool
    is_complete: bool
    payment_info: dict[str, str] | None = None

    model_config = ConfigDict(from_attributes=True)


class CatalogPaymentMethodOption(BaseModel):
    """Una entrada del catálogo de plataforma, vista desde el Tenant Admin
    (`GET /sales/payment-methods/catalog`, FR-005/FR-006). `active` es el
    estado del catálogo (no el de la activación del tenant); `already_activated`
    indica si el tenant ya tiene una configuración para este `catalog_id`."""

    id: UUID
    name: str
    fields: list[dict]
    active: bool
    already_activated: bool

    model_config = ConfigDict(from_attributes=True)


class PaymentMethodCheckoutOption(BaseModel):
    """Lo que el cajero ve en la pantalla de cobro (FR-012a, clarificación
    2026-08-24 #1): nunca `payment_info` (cuenta, celular, QR — los "datos de
    integración" que la clarificación reserva al Tenant Admin). `is_cash`/
    `type` sí viajan: son clasificación operativa, no datos de integración —
    el checkout los necesita para decidir si calcula cambio (mismo criterio
    que ya usaba `payment-input.component.ts` antes de esta spec)."""

    id: UUID
    name: str
    is_cash: bool

    model_config = ConfigDict(from_attributes=True)


# ---------- Checkout ----------
class SaleItemIn(BaseModel):
    # spec 063 (FR-024): el mecanismo de combo se retira; `combo_id` ya no se acepta.
    product_variant_id: UUID
    quantity: int = Field(1, ge=1)
    # spec 065: reemplaza `option_ids: list[UUID]` -- cada entrada trae su propia
    # cantidad elegida (default 1, el mismo significado que tenía "incluir este id").
    options: list[OptionSelectionIn] = Field(default_factory=list)


class PaymentIn(BaseModel):
    payment_method_id: UUID
    amount: Decimal = Field(..., gt=0, max_digits=12, decimal_places=2)
    reference: str | None = Field(None, max_length=255)


class ComplementaryPaymentIn(BaseModel):
    """Cobro del saldo pendiente de un pedido (spec 123,
    `sales/complementary.py::settle_pending_balance`). FR-018: un único pago
    por el monto exacto del saldo vigente. El tope de "un único pago" se
    valida en el servicio (no con `max_length=1` aquí) para devolver un 422
    con el mensaje en español, no el genérico de Pydantic."""
    cash_shift_id: UUID
    payments: list[PaymentIn] = Field(..., min_length=1)


class SaleCreate(BaseModel):
    cash_shift_id: UUID
    participant_id: UUID | None = None
    table_session_id: UUID | None = None
    dining_table_id: UUID | None = None
    customer_name: str | None = Field(None, max_length=255)
    discount: Decimal = Field(0, ge=0, max_digits=12, decimal_places=2)
    tax: Decimal = Field(0, ge=0, max_digits=12, decimal_places=2)
    tip: Decimal = Field(0, ge=0, max_digits=12, decimal_places=2)
    items: list[SaleItemIn] = Field(..., min_length=1)
    # spec 112 (RN3/FR-004): hasta 3 líneas de pago por cobro.
    payments: list[PaymentIn] = Field(..., min_length=1, max_length=3)


class SaleItemResponse(BaseModel):
    id: UUID
    product_variant_id: UUID
    description: str
    options: list = Field(default_factory=list)
    quantity: int
    unit_price: Decimal
    line_total: Decimal
    combo_id: UUID | None = None

    model_config = ConfigDict(from_attributes=True)


class PaymentResponse(BaseModel):
    id: UUID
    payment_method_id: UUID
    amount: Decimal
    reference: str | None = None
    paid_at: UtcDatetime

    model_config = ConfigDict(from_attributes=True)


class SaleInvoiceRef(BaseModel):
    """Consecutivo fiscal de la venta. Es lo que debe imprimirse en el ticket."""
    prefix: str
    number: int

    model_config = ConfigDict(from_attributes=True)


class SaleTableRef(BaseModel):
    """Mesa cobrada. Null en ventas de mostrador."""
    id: UUID
    number: int
    name: str | None = None

    model_config = ConfigDict(from_attributes=True)


class SaleResponse(BaseModel):
    id: UUID
    cash_shift_id: UUID
    user_id: UUID
    user_name: str | None = None
    customer_name: str | None = None
    subtotal: Decimal
    discount: Decimal
    tax: Decimal
    tip: Decimal
    total: Decimal
    paid_amount: Decimal | None = None
    change_given: Decimal | None = None
    status: str
    sold_at: UtcDatetime
    # spec 073 (FR-011a, A-70): el instante contra el que se evaluó la vigencia
    # temporal de las promociones de esta venta — permite explicar, desde el
    # detalle de la venta, un descuento de una promoción hoy vencida (SC-009).
    # `null` en toda venta emitida antes de esta spec (sin backfill). Misma
    # serialización que `sold_at`.
    promotion_evaluated_at: UtcDatetime | None = None
    items: list[SaleItemResponse] = Field(default_factory=list)
    payments: list[PaymentResponse] = Field(default_factory=list)
    # Para reconstruir el ticket completo fuera del momento del cobro.
    invoice: SaleInvoiceRef | None = None
    dining_table: SaleTableRef | None = None

    model_config = ConfigDict(from_attributes=True)
