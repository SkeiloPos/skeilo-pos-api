from app.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin
from sqlalchemy import String, Boolean
from sqlalchemy.orm import mapped_column, Mapped
from typing import Optional


class Supplier(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "suppliers"

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    tax_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Proveedor de sistema (p. ej. "Compra ocasional", spec 106): nunca se vincula a un
    # insumo ni puede ser preferido (RN-17).
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    __table_args__ = ({"schema": "tenant"},)
