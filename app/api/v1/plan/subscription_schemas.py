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
