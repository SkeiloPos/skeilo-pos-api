from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy import String, Boolean, Numeric, ForeignKey, DateTime, func, CheckConstraint, UniqueConstraint
from sqlalchemy.orm import mapped_column, Mapped, relationship
from typing import Optional, TYPE_CHECKING
from decimal import Decimal
from datetime import datetime

if TYPE_CHECKING:
    from .sale import Sale
    from .payment_method_catalog import PaymentMethodCatalog


class PaymentMethod(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "payment_methods"

    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    is_cash: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # Clasifica el método para el desglose de ventas del arqueo.
    # Invariante: is_cash ⇔ type == 'cash'.
    type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="other")

    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Datos de pago que el comensal necesita ver para transferir (cuenta,
    # titular, teléfono, código, u otro identificador "según el método" — sin
    # esquema fijo, spec 024). `None` para efectivo y para métodos existentes
    # hasta que se editen (research.md spec 024, Decisión 2). Desde spec 032 se
    # valida contra `catalog.fields` en `sales/service.py` (no aquí).
    payment_info: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Referencia al catálogo de plataforma (spec 032). Nullable: filas creadas
    # antes de esta spec se pueblan vía el backfill de migración
    # (research.md Decisión 3) — la capa de aplicación exige el valor para
    # filas nuevas, no la base de datos. Única (no parcial): a lo sumo una
    # fila por (tenant, catalog_id) para siempre — activar/desactivar
    # alterna `active` sobre esa misma fila, nunca crea una fila nueva
    # (FR-017; también evita que dos filas del mismo `catalog_id` choquen
    # contra `name` único, ya que `name` se copia de `catalog.name`).
    # Postgres no considera iguales dos NULL: no bloquea las filas
    # pre-backfill sin `catalog_id` todavía.
    catalog_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("shared.payment_method_catalog.id"), nullable=True
    )
    catalog: Mapped[Optional["PaymentMethodCatalog"]] = relationship()

    @property
    def fields(self) -> list[dict]:
        """Metadata de formato de `payment_info` (spec 034, `DinerPaymentMethod.fields`),
        tomada del catálogo — sin columna propia, no requiere migración."""
        return self.catalog.fields if self.catalog is not None else []

    # Recalculado en `sales/service.py` cada vez que se guarda `payment_info`,
    # comparando contra `catalog.fields` vigente en ese momento (spec 032,
    # research.md Decisión 4). No se revalida en cada lectura.
    #
    # Default `true`, no `false`: filas creadas antes de esta spec (sin
    # `catalog_id` todavía, ventana de backfill) deben seguir disponibles en
    # caja exactamente como estaban (FR-016) — `false` por defecto las habría
    # vaciado del checkout de todos los tenants desde el instante en que se
    # aplica la migración de esquema, antes incluso de correr el backfill.
    # El backfill (FR-015/FR-015a) recalcula el valor real de cada fila
    # contra `catalog.fields`; filas nuevas (spec 032 en adelante) siempre
    # pasan por `service.py`, que también lo recalcula — nunca se apoyan en
    # este default.
    is_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    __table_args__ = (
        CheckConstraint(
            "type IN ('cash', 'card', 'transfer', 'other', 'mixed')",
            name="ck_payment_method_type",
        ),
        UniqueConstraint("catalog_id", name="uq_payment_method_catalog_id"),
        {"schema": "tenant"},
    )


class Payment(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "payments"

    sale_id: Mapped[UUID] = mapped_column(
        ForeignKey("sales.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sale: Mapped["Sale"] = relationship(back_populates="payments")

    payment_method_id: Mapped[UUID] = mapped_column(
        ForeignKey("payment_methods.id"), nullable=False, index=True
    )

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    reference: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    paid_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payment_amount_positive"),
        {"schema": "tenant"},
    )
