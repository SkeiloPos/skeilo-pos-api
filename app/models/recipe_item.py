from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import Numeric, ForeignKey, CheckConstraint, UniqueConstraint
from sqlalchemy.orm import mapped_column, Mapped, relationship
from typing import TYPE_CHECKING
from decimal import Decimal

if TYPE_CHECKING:
    from .product_variant import ProductVariant
    from .inventory_item import InventoryItem


class RecipeItem(UUIDPrimaryKeyMixin, Base):
    """BOM: los insumos **fijos** que una variante consume al venderse. `quantity` está
    en la unidad de medida del propio insumo (sin conversión).

    Lo que el cliente elige (sabores) no se modela aquí: vive en
    `variant_option_groups`, porque además de la cantidad necesita cuántas opciones se
    pueden elegir, y mezclar ambas cosas en la receta la convertía en medio catálogo.
    """

    __tablename__ = "recipe_items"

    product_variant_id: Mapped[UUID] = mapped_column(
        ForeignKey("product_variants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_variant: Mapped["ProductVariant"] = relationship(back_populates="recipe_items")

    inventory_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("inventory_items.id"), nullable=False, index=True
    )
    # spec 105 (research.md): `lazy="joined"` -- mismo patrón que
    # `ProductVariant.presentation` -- para que el nombre/unidad del insumo viajen en la
    # misma consulta que ya trae la receta (`selectinload` en `get_detail_or_404`), sin
    # que el cliente necesite resolverlos aparte por id insumo por insumo.
    inventory_item: Mapped["InventoryItem"] = relationship(lazy="joined")

    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)

    @property
    def inventory_item_name(self) -> str:
        """Denormalizado desde la relación ya cargada (spec 105) -- mismo patrón que
        `Product.category_name`/`ProductVariant.presentation_name`: evita que el cliente
        dependa de `GET /inventory/items/{id}` por cada insumo de la receta."""
        return self.inventory_item.name

    @property
    def unit_measure_id(self) -> UUID:
        return self.inventory_item.unit_measure_id

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_recipe_item_qty_positive"),
        UniqueConstraint(
            "product_variant_id", "inventory_item_id",
            name="uq__recipe_items__product_variant_id__inventory_item_id",
        ),
        {"schema": "tenant"},
    )
