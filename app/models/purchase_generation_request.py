from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import String, DateTime, Index, func
from sqlalchemy.orm import mapped_column, Mapped
from typing import Any
from datetime import datetime


class PurchaseGenerationRequest(UUIDPrimaryKeyMixin, Base):
    """Registro de deduplicación por `idempotency_key` para la generación de compra
    sugerida (spec 158). `purchase_ids` guarda las `Purchase.id` creadas por esa
    solicitud; sin FK entrante, nunca invalida una `Purchase` ya creada si se purga."""

    __tablename__ = "purchase_generation_requests"

    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)

    purchase_ids: Mapped[Any] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "ux__purchase_generation_requests__idempotency_key",
            "idempotency_key",
            unique=True,
        ),
        {"schema": "tenant"},
    )
