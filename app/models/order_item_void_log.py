from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import String, ForeignKey, DateTime, func
from sqlalchemy.orm import mapped_column, Mapped
from typing import Optional
from datetime import datetime


class OrderItemVoidLog(UUIDPrimaryKeyMixin, Base):
    """Auditoría de anulación/reemplazo de un ítem individual (Fase 6). El ítem
    anulado no se elimina (estado_cocina='cancelado', spec 147); si se
    reemplaza, el nuevo ítem apunta al anulado vía order_items.void_de."""

    __tablename__ = "order_item_void_logs"

    order_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("order_items.id", ondelete="CASCADE"), nullable=False, index=True
    )

    motivo: Mapped[str] = mapped_column(String(500), nullable=False)

    # Referencia blanda a shared.users.id (quién anuló) + snapshot del nombre.
    user_id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = ({"schema": "tenant"},)
