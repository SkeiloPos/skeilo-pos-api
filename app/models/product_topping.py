from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import ForeignKey, CheckConstraint, UniqueConstraint
from sqlalchemy.orm import mapped_column, Mapped, relationship
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .product import Product


class ProductTopping(UUIDPrimaryKeyMixin, Base):
    """Qué adición (topping) ofrece un producto (spec 129). Reemplaza, para adiciones,
    al enlace que antes vivía entre variante y grupo de opciones: se declara una sola
    vez a nivel de producto y aplica automáticamente a todas sus variantes.

    `topping_product_id` MUST referenciar un producto con `is_topping=true` --
    validado en servicio (`ProductService`), no expresable en un `CheckConstraint` de
    una sola tabla."""

    __tablename__ = "product_toppings"

    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product: Mapped["Product"] = relationship(
        foreign_keys=[product_id], back_populates="toppings"
    )

    # Sin ondelete: una adición referenciada no puede borrarse físicamente -- se
    # retira por soft-delete (`active=false`), igual que `option_group_id` en
    # `VariantOptionGroup`.
    topping_product_id: Mapped[UUID] = mapped_column(
        ForeignKey("products.id"), nullable=False, index=True
    )
    # `lazy="joined"` -- mismo patrón que `ProductVariant.presentation` -- para que el
    # nombre de la adición viaje en la misma consulta que ya trae `product.toppings`
    # (`selectinload`), sin que el cliente tenga que resolverlo por id aparte (ver
    # memoria del proyecto: preferir denormalización de backend a resolve-by-id).
    topping_product: Mapped["Product"] = relationship(
        foreign_keys=[topping_product_id], lazy="joined"
    )

    @property
    def topping_name(self) -> str:
        return self.topping_product.name

    __table_args__ = (
        UniqueConstraint(
            "product_id", "topping_product_id",
            name="uq__product_toppings__product_id__topping_product_id",
        ),
        CheckConstraint(
            "product_id <> topping_product_id", name="ck_product_toppings_no_self_association"
        ),
        {"schema": "tenant"},
    )
