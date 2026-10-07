import logging

from app.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import String, Boolean, Integer, Numeric, ForeignKey, CheckConstraint, UniqueConstraint
from sqlalchemy.orm import mapped_column, Mapped, relationship
from typing import List, Optional, TYPE_CHECKING
from decimal import Decimal

if TYPE_CHECKING:
    from .product import Product
    from .recipe_item import RecipeItem
    from .variant_option_group import VariantOptionGroup
    from .presentation import Presentation

logger = logging.getLogger(__name__)


class ProductVariant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Línea vendible: aquí viven el precio y la receta. Su nombre **estructural** es el
    de su `Presentation` (spec 084, A-79): `presentation_id` sigue siendo obligatorio y
    es la única fuente de `presentation_name`. Productos sin tamaños obtienen una
    variante asociada a la presentación 'Presentación única' (spec 083, A-74).

    `name` (spec 099) es distinto: un alias comercial **opcional y puramente
    cosmético** ("Para compartir") que convive con `presentation_id` sin reemplazarlo.
    No reabre la columna `name` que la spec 084 eliminó como clave estructural --
    ver `research.md` de la spec 099, "Contexto histórico"."""

    __tablename__ = "product_variants"

    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product: Mapped["Product"] = relationship(back_populates="variants")

    sku: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, unique=True)

    # spec 099: alias comercial opcional y puramente cosmético. Nunca `null` (FR-003):
    # mismo patrón que `price` abajo -- `default`/`server_default` juntos, uno para
    # instancias en memoria sin flush todavía, otro para la columna en la base.
    name: Mapped[str] = mapped_column(
        String(60), nullable=False, default="", server_default=""
    )

    price: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=0, server_default="0"
    )

    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Posición de despliegue dentro del producto (spec 042): determina el orden en el
    # formulario y en el detalle del Menú QR. Sin default de ORM -- toda ruta que crea
    # una variante (fixtures de test incluidas) debe asignarlo explícitamente.
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)

    # spec 084 (A-79): la presentación del catálogo (spec 083) es obligatoria y es la única
    # fuente del nombre de la variante (`presentation_name`). RESTRICT y no SET NULL/CASCADE:
    # no hay endpoint de borrado de `Presentation`, pero si alguien la borra a mano no debe
    # arrastrar variantes (ni recetas ni historial) ni dejarlas sin nombre. `lazy="joined"`
    # porque toda lectura de una variante necesita su nombre (evita N+1 en el menú público).
    presentation_id: Mapped[UUID] = mapped_column(
        ForeignKey("presentations.id", ondelete="RESTRICT"), nullable=False
    )
    presentation: Mapped["Presentation"] = relationship(lazy="joined")

    @property
    def presentation_name(self) -> str:
        return self.presentation.name

    @property
    def display_name(self) -> str:
        """Etiqueta que ve el comensal (spec 099, FR-004/FR-005): `name` si no está
        vacío; si no, el de la presentación; si ninguno está disponible, `"Tamaño"` con
        una advertencia registrada (Escenario 16) -- nunca una etiqueta vacía."""
        if self.name:
            return self.name
        presentation = self.presentation
        if presentation is not None and presentation.name:
            return presentation.name
        logger.warning(
            "ProductVariant %s sin display_name resoluble (name vacío, presentación "
            "no disponible); se usa el valor por defecto 'Tamaño'.",
            self.id,
        )
        return "Tamaño"

    recipe_items: Mapped[List["RecipeItem"]] = relationship(
        back_populates="product_variant", cascade="all, delete-orphan"
    )

    option_groups: Mapped[List["VariantOptionGroup"]] = relationship(
        back_populates="product_variant", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("price >= 0", name="ck_product_variant_price_positive"),
        UniqueConstraint(
            "product_id", "display_order", name="uq__product_variants__product_id__display_order"
        ),
        UniqueConstraint(
            "product_id", "presentation_id",
            name="uq__product_variants__product_id__presentation_id",
        ),
        {"schema": "tenant"},
    )
