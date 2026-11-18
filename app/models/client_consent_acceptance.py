from datetime import datetime

from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, UUIDPrimaryKeyMixin


class ClientConsentAcceptance(Base, UUIDPrimaryKeyMixin):
    """Evidencia de que un cliente final aceptó el documento legal vigente del
    tenant (FR-018, spec 162). Nace siempre junto con el `SessionParticipant`
    que identifica al cliente, en la misma transacción de
    `cart/service.py::open_session` (research.md Decisión 6): si cualquiera
    de las dos inserciones falla, ninguna persiste.

    `ip_hash` nunca es la IP en claro (research.md Decisión 5): es
    `sha256(ip + CONSENT_IP_HASH_SALT)`, determinista (comparable) pero no
    reversible sin el salt."""

    __tablename__ = "client_consent_acceptances"

    session_participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("session_participants.id"), nullable=False
    )
    tenant_legal_document_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenant_legal_documents.id"), nullable=False
    )
    ip_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_client_consent_acceptance_participant", "session_participant_id"),
        Index("idx_client_consent_acceptance_document", "tenant_legal_document_id"),
        {"schema": "tenant"},
    )
