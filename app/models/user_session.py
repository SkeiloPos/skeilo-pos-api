from app.core.models import Base
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import ForeignKey, DateTime, func, String
from sqlalchemy.orm import mapped_column, Mapped, relationship
from typing import Optional, TYPE_CHECKING
from datetime import datetime
import uuid

if TYPE_CHECKING:
    from app.core.models import User


class UserSession(Base):
    """Sesión activa de un usuario (spec 159, research.md Decisión 5). `id`
    es el mismo `jti` del refresh token emitido en el login -- el JWT gana un
    claim `sid` que referencia esta fila. `revoked_at IS NULL` = sesión
    activa; se revoca en logout, revocación individual o masiva desde el
    Super Admin."""

    __tablename__ = "user_sessions"

    id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shared.users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user: Mapped["User"] = relationship(back_populates="sessions")

    device_label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    browser: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = ({"schema": "shared"},)
