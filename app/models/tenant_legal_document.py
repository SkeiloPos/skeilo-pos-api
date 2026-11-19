from datetime import datetime

from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, UUIDPrimaryKeyMixin


class TenantLegalDocument(Base, UUIDPrimaryKeyMixin):
    """Snapshot inmutable del documento legal propio del tenant (FR-014,
    spec 162) — la única fuente que ve el cliente final. `master_template_id`
    es una FK cross-schema a `shared.master_legal_templates.id`, igual que
    `payment_methods.catalog_id -> shared.payment_method_catalog` (spec 032,
    data-model.md tabla 5 "Nota cross-schema"): `get_tenant_specific_metadata`
    ya copia la tabla referenciada al crear un schema de tenant.

    Ninguna columna de una fila ya insertada se actualiza después (FR-014):
    el único `UPDATE` de esta tabla es el que pasa la fila `publicado`
    anterior a `superado` al publicar la siguiente, dentro de la misma
    transacción del `INSERT`."""

    __tablename__ = "tenant_legal_documents"

    version: Mapped[int] = mapped_column(Integer, nullable=False)
    master_template_id: Mapped[UUID] = mapped_column(
        ForeignKey("shared.master_legal_templates.id"), nullable=False
    )
    # Denormalizado para mostrar el banner de FR-015 sin round-trip a `shared`.
    master_template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Markdown con las variables ya sustituidas y escapadas (FR-011).
    contenido_renderizado: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(32), nullable=False)
    estado: Mapped[str] = mapped_column(String(12), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        Index(
            "idx_tenant_legal_document_vigente",
            "estado",
            unique=True,
            postgresql_where=text("estado = 'publicado'"),
            sqlite_where=text("estado = 'publicado'"),
        ),
        CheckConstraint(
            "estado IN ('publicado', 'superado')",
            name="ck_tenant_legal_documents_estado",
        ),
        {"schema": "tenant"},
    )
