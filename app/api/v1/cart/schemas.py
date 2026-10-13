from uuid import UUID
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.v1.catalog.schemas import OptionSelectionIn
from app.core.schema_types import AssetUrl
from app.core.storage import asset_display_url


# ---------- Apertura de sesión (por QR token) ----------
class SessionOpenIn(BaseModel):
    qr_token: str = Field(..., description="Token de QR firmado de la mesa.")
    display_name: str = Field(
        ..., min_length=1, max_length=255, examples=["Ana Pérez"],
        description="Nombre que el comensal escribe. No es único ni identifica: "
                    "el identificador real va firmado en el token de sesión.",
    )


class SessionTableInfo(BaseModel):
    id: UUID
    number: int
    name: str | None = None

    model_config = ConfigDict(from_attributes=True)


class SessionOpenResponse(BaseModel):
    participant_id: UUID
    table_session_id: UUID
    display_name: str
    # Nombre desambiguado que ven cocina y staff ("Ana (2)" si ya había una Ana).
    display_label: str | None = None
    expires_at: datetime | None = None
    table: SessionTableInfo
    cart_id: UUID
    # Token que el frontend debe enviar en `x-session-token` para operar el carrito.
    session_token: str


# ---------- Ítems de carrito ----------
class CartItemIn(BaseModel):
    # spec 063 (FR-024): el mecanismo de combo se retira; `combo_id` ya no se acepta.
    product_variant_id: UUID
    quantity: int = Field(1, ge=1)
    # spec 065: reemplaza `option_ids: list[UUID]` -- cada entrada trae su propia
    # cantidad elegida (default 1, el mismo significado que tenía "incluir este id").
    options: list[OptionSelectionIn] = Field(default_factory=list)
    notes: str | None = Field(None, max_length=500)


class CartItemUpdate(BaseModel):
    quantity: int | None = Field(None, ge=1)
    options: list[OptionSelectionIn] | None = None
    notes: str | None = Field(None, max_length=500)


class CartItemOptionResponse(BaseModel):
    id: UUID
    option_id: UUID
    quantity: int
    # spec 089 (A-94): la opción es un adicional cobrado/consumido una vez por línea.
    per_line: bool = False

    model_config = ConfigDict(from_attributes=True)


class AdjustedOptionOut(BaseModel):
    """Spec 117 (A-126, D4/D5): una adición "Por cantidad" que `update_item` tuvo que
    reducir automáticamente porque el nuevo tope escalado (por la cantidad de producto
    reducida) ya no la admitía."""
    option_id: UUID
    option_name: str
    previous_quantity: int
    new_quantity: int


class CartItemResponse(BaseModel):
    id: UUID
    product_variant_id: UUID
    quantity: int
    # spec 089: `unit_price` es solo el precio de UNA unidad de producto (en las líneas
    # nuevas del Menú QR ya no incluye los adicionales); `addons_total` los suma una vez.
    # `line_total = unit_price × quantity + addons_total`.
    unit_price: Decimal
    addons_total: Decimal = Decimal("0")
    line_total: Decimal
    # Precio/subtotal ya con el mejor descuento percent/fixed vigente aplicado, o
    # `None` si ninguna promoción aplica a esta línea (o es un combo: ese ahorro se
    # calcula aparte, al cobrar).
    discounted_unit_price: Decimal | None = None
    discounted_line_total: Decimal | None = None
    notes: str | None = None
    combo_id: UUID | None = None
    options: list[CartItemOptionResponse] = Field(default_factory=list)
    # Spec 117 (A-126, D5): poblado SOLO en la respuesta del `PATCH` que disparó el
    # ajuste automático (contracts/cart-item-auto-adjust.md) -- `[]` en toda otra
    # lectura/escritura, incluido `GET /cart`, que nunca lo repite. Aditivo: no rompe a
    # ningún cliente que lo ignore.
    adjusted_options: list[AdjustedOptionOut] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class CartResponse(BaseModel):
    id: UUID
    participant_id: UUID
    # Nombre del comensal dueño del carrito. Viaja aquí porque el `session_token`
    # no lleva el nombre: es lo que permite repintar el saludo tras una recarga.
    display_name: str
    display_label: str | None = None
    status: str
    total: Decimal
    # Suma de las líneas ya con su mejor descuento aplicado, o `None` si ninguna
    # línea tiene promoción vigente. Vista previa para el comensal; el cobro real
    # lo sigue fijando el checkout de staff.
    discounted_total: Decimal | None = None
    items: list[CartItemResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


# ---------- Pedidos del comensal ----------
class MyOrderCancelIn(BaseModel):
    motivo: str = Field(..., min_length=1, max_length=500,
                        examples=["Me equivoqué de sabor"])


# ---------- Pagos del comensal (spec 024) ----------
class DinerPaymentMethod(BaseModel):
    """Método de pago tal como lo ve el comensal — solo los que el tenant
    tiene `active` (FR-004); nunca expone el flag `active` en sí."""
    id: UUID
    name: str
    type: str
    is_cash: bool
    payment_info: dict[str, str] | None = None
    # Metadata de formato de cada clave de `payment_info` (spec 034,
    # FR-011/FR-012) — mismo shape que ya usa `CatalogPaymentMethodOption.fields`
    # (sales/schemas.py:57): [{"key", "label", "required", "format", "length"?}].
    fields: list[dict] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def _assemble_asset_urls(self):
        """spec 080 (FR-005/FR-007): las claves de `payment_info` marcadas
        `format:"image"` (según `fields`) se entregan al comensal como URL de
        visualización contra ASSETS_BASE_URL. La columna en base de datos no
        cambia (FR-006) — este validador corre sobre el modelo ya poblado desde
        el ORM, sobre una copia del dict."""
        if self.payment_info:
            image_keys = {
                f.get("key") for f in self.fields if f.get("format") == "image"
            }
            if image_keys:
                self.payment_info = {
                    k: (asset_display_url(v) if k in image_keys else v)
                    for k, v in self.payment_info.items()
                }
        return self


class PaymentAttemptCreateIn(BaseModel):
    payment_method_id: UUID


class DinerPaymentAttempt(BaseModel):
    """Intento de pago tal como lo ve el comensal — **nunca** incluye
    `rejection_reason` (Clarification 3)."""
    id: UUID
    order_id: UUID
    payment_method_id: UUID
    status: str
    # spec 088 (FR-008): la columna guarda la key; se ensambla la URL de visualización al
    # serializar (una fila histórica con URL absoluta se sigue viendo, tolerancia de lectura).
    receipt_file_url: AssetUrl = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReceiptPresignIn(BaseModel):
    content_type: str = Field(..., min_length=1, max_length=100, examples=["image/jpeg"])


class ReceiptPresignOut(BaseModel):
    upload_url: str
    key: str
    public_url: str
    expires_in: int


class ReceiptAttachIn(BaseModel):
    file_url: str = Field(..., min_length=1, max_length=500)


# ---------- Revisión y pago antes de enviar (spec 025) ----------
class SubmitCartIn(BaseModel):
    """Cuerpo de `POST /cart/submit` — el pedido nace junto con su primer
    intento de pago (contracts/submit-cart-with-payment.md)."""
    payment_method_id: UUID
    receipt_file_url: str | None = Field(None, max_length=500)


class PaymentReceiptPresignIn(ReceiptPresignIn):
    """Mismo shape que `ReceiptPresignIn` (`content_type`), sin campos
    nuevos — reexportada con su propio nombre porque alimenta un endpoint
    distinto (`POST /cart/payment-receipt/presign`, no ligado a ningún
    `attempt_id`, contracts/payment-receipt-presign.md)."""
