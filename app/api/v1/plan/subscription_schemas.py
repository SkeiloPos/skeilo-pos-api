"""Esquemas de la suscripción del tenant (spec 161; contrato `subscription-api.md`)."""
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.core.timezone import UtcDatetime


class PaymentLinkResponse(BaseModel):
    """`POST /plan/subscription/payment-link` (FR-014, FR-015, FR-018). `reused` es `true` cuando ya había un
    pago pendiente vigente y se devolvió ese mismo link (FR-016)."""

    payment_id: UUID
    status: str
    amount: Decimal
    currency: str
    gateway_reference: str
    payment_link_url: str
    payment_link_expires_at: UtcDatetime
    reused: bool

    model_config = ConfigDict(from_attributes=True)


class PaymentHistoryItem(BaseModel):
    """Un pago en el historial (FR-035; contrato `subscription-api.md § 3`).

    A propósito **no** incluye `payment_link_url`, `gateway_transaction_id` ni nada de la firma: el historial
    es información de cobro, no de la pasarela. `currency` es constante (solo se cobra en pesos colombianos).
    """

    id: UUID
    status: str
    amount: Decimal
    currency: str = "COP"
    ciclo_facturacion: str
    gateway: str
    gateway_reference: str
    requested_by_user_id: Optional[UUID] = None
    period_start: Optional[UtcDatetime] = None
    period_end: Optional[UtcDatetime] = None
    paid_at: Optional[UtcDatetime] = None
    payment_link_expires_at: UtcDatetime
    created_at: UtcDatetime
    updated_at: Optional[UtcDatetime] = None

    model_config = ConfigDict(from_attributes=True)


class PaymentHistoryResponse(BaseModel):
    """`GET /plan/subscription/payments` y `GET /super-admin/tenants/{id}/subscription-payments`: misma forma
    paginada que `/notifications` (`items`, `total`, `page`, `size`)."""

    items: list[PaymentHistoryItem]
    total: int
    page: int
    size: int
