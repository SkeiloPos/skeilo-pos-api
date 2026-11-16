from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import Integer, ForeignKey, DateTime, func, UniqueConstraint
from sqlalchemy.orm import mapped_column, Mapped, relationship
from typing import TYPE_CHECKING
from datetime import datetime

if TYPE_CHECKING:
    from app.core.models import User, Tenant, Role


class TenantMembership(UUIDPrimaryKeyMixin, Base):
    """Membresía de un usuario en un negocio, con el rol que tiene en ESE
    negocio (spec 159, research.md Decisión 1). `users.tenant_id` sigue
    siendo el "negocio principal" que ya usan login (spec 091) e invitación
    (spec 037) -- esta tabla es aditiva y representa el conjunto completo de
    negocios/roles de un usuario, incluyendo los multi-negocio."""

    __tablename__ = "tenant_memberships"

    user_id: Mapped[UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shared.users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user: Mapped["User"] = relationship(back_populates="memberships")

    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("shared.tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant: Mapped["Tenant"] = relationship()

    role_id: Mapped[UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shared.roles.id"), nullable=False
    )
    role: Mapped["Role"] = relationship()

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "tenant_id", name="uq__tenant_memberships__user_id_tenant_id"),
        {"schema": "shared"},
    )
