from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy import String, Integer, DateTime, CheckConstraint, func
from sqlalchemy.orm import mapped_column, Mapped
from typing import Optional
from datetime import datetime


class InventoryImportJob(UUIDPrimaryKeyMixin, Base):
    """Seguimiento de una importación masiva de inventario (spec 109). Una fila por
    archivo subido; la tarea Celery la actualiza a medida que procesa cada fila
    (research.md D2) -- no depende del result backend de Celery para que el
    resumen sobreviva un reinicio del worker y sea consultable por tenant.
    """

    __tablename__ = "inventory_import_jobs"

    status: Mapped[str] = mapped_column(String(20), nullable=False)

    file_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Referencia blanda a shared.users.id (sin FK cross-schema), igual patrón que
    # CashShift.user_id.
    started_by_user_id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    started_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    total_rows: Mapped[int] = mapped_column(Integer, nullable=False)

    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    updated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # Lista de {fila, codigo, motivo} -- a la escala acordada (≈500 filas) no
    # justifica una tabla hija aparte (data-model.md).
    errors: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")

    __table_args__ = (
        CheckConstraint(
            "status IN ('procesando', 'finalizado', 'interrumpido')",
            name="ck_inventory_import_job_status",
        ),
        {"schema": "tenant"},
    )
