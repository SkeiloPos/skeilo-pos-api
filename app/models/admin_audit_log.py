from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy import String, Integer, DateTime, func, Index
from sqlalchemy.orm import mapped_column, Mapped
from typing import Optional, Any
from datetime import datetime


class AdminAuditLog(UUIDPrimaryKeyMixin, Base):
    """Bitácora de auditoría administrativa de alcance plataforma (spec 159,
    research.md Decisión 6). Distinta de `tenant.audit_logs` (por negocio,
    operación de venta/mesa). Referencias a `users`/`tenants` son BLANDAS a
    propósito (sin FK): deben sobrevivir a la eliminación permanente de un
    usuario (FR-038) -- por eso cada fila guarda también un snapshot
    (`actor_name_snapshot`, `affected_user_email_snapshot`)."""

    __tablename__ = "admin_audit_logs"

    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_user_id: Mapped[Optional[UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_name_snapshot: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    affected_user_id: Mapped[Optional[UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    affected_user_email_snapshot: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    tenant_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    action: Mapped[str] = mapped_column(String(50), nullable=False)
    detail: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix__admin_audit_logs__created_at", "created_at"),
        Index("ix__admin_audit_logs__affected_user_id", "affected_user_id"),
        Index("ix__admin_audit_logs__actor_user_id", "actor_user_id"),
        Index("ix__admin_audit_logs__tenant_id", "tenant_id"),
        {"schema": "shared"},
    )
