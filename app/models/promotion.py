from app.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import (
    String, Text, Integer, Numeric, Boolean, ForeignKey, DateTime, Time, CheckConstraint,
    UniqueConstraint, Index,
)
from sqlalchemy.orm import mapped_column, Mapped, relationship
from typing import Optional, List, TYPE_CHECKING
from decimal import Decimal
from datetime import datetime, time

if TYPE_CHECKING:
    from .product_variant import ProductVariant


# Máquina de estados del RF. `draft` es el único estado en el que se pueden
# agregar, quitar o editar reglas: una vez activada, la promoción ya pudo
# explicar el descuento de una venta y reescribir sus reglas reescribiría la
# historia. Para cambiar la forma se duplica (`POST /promotions/{id}/duplicate`).
PROMOTION_STATUSES = ("draft", "active", "paused", "finished")

# Transiciones permitidas. `finished` es terminal: una promoción vencida no
# revive, se duplica.
PROMOTION_TRANSITIONS = {
    "draft": {"active", "finished"},
    "active": {"paused", "finished"},
    "paused": {"active", "finished"},
    "finished": set(),
}

# spec 063: los tipos **vivos** de una regla son solo `percent` y
# `package_price`. Los valores viejos (`fixed`/`combo`/`qty_price`/
# `qty_price_presentation`) SE CONSERVAN aquí como referencia: la migración
# `063c` copia el `type` histórico de una promoción `finished` (cerrada por
# `063a`) directo a la regla que le crea, sin filtrar por `status` —
# `PromotionRule.type` no lleva `CHECK` de valores (ver su docstring). La
# restricción "solo dos tipos vivos" para escritura nueva vive en el enum de
# ENTRADA de Pydantic (`PromotionType` en `schemas.py`).
PROMOTION_TYPES = (
    "percent", "fixed", "combo", "qty_price", "qty_price_presentation", "package_price",
)


class Promotion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Promoción del catálogo (spec 063, partición `Promoción`/`Regla`,
    revisión 2026-09-01, FR-001). Agrupa **una o más** `PromotionRule`, que
    comparten su vigencia y su estado — no tienen vigencia ni estado propios.

    Vigencia opcional: `starts_at`/`ends_at`, `days_of_week` (CSV 0=lunes..
    6=domingo) y ventana horaria `start_time`/`end_time`, que admite cruce de
    medianoche. **Toda la vigencia se evalúa en hora local del tenant** (A-08).

    Ya no tiene `type`/`value`/`min_qty`/`variants` propios (retirados en la
    migración destructiva `063d`, Incremento J) — esos viven en cada
    `PromotionRule`.
    """

    __tablename__ = "promotions"

    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="draft", index=True
    )

    starts_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    days_of_week: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    start_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    end_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)

    # spec 063 (FR-025): `NULL` salvo en las promociones que la migración `063a`
    # pasó a `finished`. Fuente del aviso "recrea a mano"
    # (`GET /promotions?closed_by_refactor=true`).
    closed_by_refactor_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    # spec 144 (FR-001/FR-010): visibilidad específica del menú QR, independiente
    # de `status` -- mismo patrón que `Category.visible_en_qr` (spec 128) /
    # `Product.visible_en_qr` (spec 129). Editable en cualquier estado, incluido
    # `active` (ver guard de `update()` en `service.py`).
    visible_en_qr: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # spec 063 (revisión 2026-09-01, FR-001/FR-001a): una promoción agrupa una
    # o más reglas, que comparten su vigencia y su estado.
    rules: Mapped[List["PromotionRule"]] = relationship(
        back_populates="promotion", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'active', 'paused', 'finished')",
            name="ck_promotion_status",
        ),
        # `active_variant_set_rules` filtra estado y fecha de corte en SQL.
        Index("ix_promotions_status_ends_at", "status", "ends_at"),
        {"schema": "tenant"},
    )


class PromotionRule(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """spec 063 (revisión 2026-09-01, FR-001/FR-001a) — la combinación (tipo,
    valor, cantidad mínima) que antes vivía directo en `Promotion`, ahora en
    una entidad hija: una `Promotion` agrupa **una o más** `PromotionRule`,
    que comparten su vigencia y su estado (no tienen vigencia ni estado
    propios). Dentro de una misma promoción, los conjuntos de variantes de
    sus reglas DEBEN ser disjuntos entre sí (FR-001a, validado en
    `_guard_variant_overlap`/`_guard_no_shared_variants_within_payload`, no
    por `CHECK`: ver nota de `type` abajo).

    - `percent`: `value` = % de descuento (0 < value <= 100).
    - `package_price`: `value` = precio total de `min_qty` unidades cualesquiera
      del conjunto de variantes de **esta** regla (`promotion_variants`).

    `type` **no lleva `CHECK` de valores**: el paso de datos de la migración
    `063c` copia el `type` histórico de toda `Promotion` existente —incluidas
    las `Finalizada` con un tipo fuera de `{percent, package_price}`— sin
    filtrar por `status`. `ck_promotion_type` (que existía en `Promotion`
    antes de `063d`) sí podía escapar `OR status='finished'` porque `status`
    era una columna de la misma fila; `PromotionRule` no tiene columna de
    estado propia por diseño, y Postgres no admite subconsultas en un
    `CHECK`. La restricción a los dos tipos vivos para escritura nueva vive
    en el schema Pydantic de entrada (`PromotionRuleIn.type`).
    """

    __tablename__ = "promotion_rules"

    promotion_id: Mapped[UUID] = mapped_column(
        ForeignKey("promotions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    promotion: Mapped["Promotion"] = relationship(back_populates="rules")

    type: Mapped[str] = mapped_column(String(50), nullable=False)

    value: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=0, server_default="0"
    )

    min_qty: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    variants: Mapped[List["PromotionVariant"]] = relationship(
        back_populates="rule", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("value >= 0", name="ck_promotion_rule_value_positive"),
        CheckConstraint("min_qty >= 1", name="ck_promotion_rule_min_qty"),
        CheckConstraint(
            "type <> 'percent' OR value <= 100",
            name="ck_promotion_rule_percent_range",
        ),
        Index("ix_promotion_rules_promotion_id", "promotion_id"),
        {"schema": "tenant"},
    )


class PromotionVariant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """spec 063 (FR-001) — una fila = "esta variante pertenece al conjunto
    elegible de esta regla". Tabla hija de `promotion_rules`.

    **No lleva `type`, `value` ni `min_qty`**: la única combinación (tipo, valor,
    cantidad mínima) vive en la `PromotionRule`. Es lo que permite que un paquete
    combine variantes distintas del conjunto (clarification 2026-08-31).

    `ondelete="CASCADE"` en `product_variant_id`: FR-011 — una variante
    **eliminada** sale del conjunto de toda regla sin dejar fila huérfana.
    Una variante **desactivada** no se borra: el motor la filtra por
    `product_variants.active`.

    Ya no tiene `promotion_id` propio (retirado en la migración destructiva
    `063d`, Incremento J) — la promoción dueña se resuelve vía
    `rule.promotion`.
    """

    __tablename__ = "promotion_variants"

    promotion_rule_id: Mapped[UUID] = mapped_column(
        ForeignKey("promotion_rules.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rule: Mapped["PromotionRule"] = relationship(back_populates="variants")

    product_variant_id: Mapped[UUID] = mapped_column(
        ForeignKey("product_variants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_variant: Mapped["ProductVariant"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "promotion_rule_id", "product_variant_id",
            name="uq__promotion_variants__promotion_rule_id__product_variant_id",
        ),
        {"schema": "tenant"},
    )
