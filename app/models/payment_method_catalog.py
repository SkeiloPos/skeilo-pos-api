from app.core.models import Base, UUIDPrimaryKeyMixin, TimestampMixin
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import String, Boolean, CheckConstraint
from sqlalchemy.orm import mapped_column, Mapped
from typing import Optional


class PaymentMethodCatalog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payment_method_catalog"

    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    # Misma clasificación que PaymentMethod.type (app/models/payment.py) — se
    # copia al PaymentMethod del tenant al activarse (spec 032, research.md
    # Decisión 5), así el arqueo sigue agrupando por esa columna sin cambios.
    type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="other")

    # Activación a nivel plataforma (RF-1). Nunca se borra la fila — desactivar
    # es el único mecanismo (spec 032, Assumptions).
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Lista de campos de integración que un tenant debe/puede diligenciar al
    # activar este método: [{"key", "label", "required", "format", "length"?}].
    # `format` ∈ "text"|"numeric"|"image". Sin esquema fijo en la base de datos
    # (validado en Pydantic/service.py, no por CHECK — mismo criterio que
    # PaymentMethod.payment_info, spec 024). Lista vacía = sin campos (Efectivo).
    fields: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")

    __table_args__ = (
        CheckConstraint(
            "type IN ('cash', 'card', 'transfer', 'other', 'mixed')",
            name="ck_payment_method_catalog_type",
        ),
        {"schema": "shared"},
    )
