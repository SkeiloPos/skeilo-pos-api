from typing import Optional
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, UUIDPrimaryKeyMixin


class TenantLegalProfile(Base, UUIDPrimaryKeyMixin):
    """Datos de negocio del tenant usados para rellenar la plantilla legal
    maestra (spec 162, FR-012) — una fila por schema de tenant (a lo sumo una
    en toda la tabla). Los 6 campos son nullable hasta que el tenant los
    completa: "completo" se valida en el servicio (preview/publish), no con
    `NOT NULL` de columna (data-model.md tabla 4)."""

    __tablename__ = "tenant_legal_profiles"

    razon_social: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    identificacion_legal: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    direccion_legal: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ciudad: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    correo_contacto: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    telefono_contacto: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)

    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, onupdate=func.now(), nullable=True)

    __table_args__ = ({"schema": "tenant"},)
