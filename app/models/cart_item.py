from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import Boolean, String, Integer, Numeric, ForeignKey, CheckConstraint, UniqueConstraint, false
from sqlalchemy.orm import mapped_column, Mapped, relationship, column_property
from sqlalchemy import select
from typing import Optional, List, TYPE_CHECKING
from decimal import Decimal

if TYPE_CHECKING:
    from .cart import Cart


class CartItem(UUIDPrimaryKeyMixin, Base):
    """Línea del carrito. Espeja a `order_items` (variante + snapshot de precio +
    opciones); al consolidar se copia tal cual a una `order_item`."""

    __tablename__ = "cart_items"

    cart_id: Mapped[UUID] = mapped_column(
        ForeignKey("carts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cart: Mapped["Cart"] = relationship(back_populates="items")

    product_variant_id: Mapped[UUID] = mapped_column(
        ForeignKey("product_variants.id"), nullable=False, index=True
    )

    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    unit_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=0, server_default="0"
    )

    # spec 089 (A-94): Σ(precio del adicional × cantidad elegida) de las opciones
    # `per_line`, cobrado UNA vez por línea (no por unidad de producto). Total de la
    # línea = `unit_price × quantity + addons_total`. Las líneas anteriores quedan en 0
    # (su `unit_price` sigue incluyendo los extras por unidad): mismo total de siempre.
    addons_total: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=0, server_default="0"
    )

    # Promoción de combo que generó esta línea (selección explícita). Varias
    # líneas comparten el mismo combo_id: son los componentes de un mismo combo.
    combo_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("promotions.id", ondelete="SET NULL"), nullable=True, index=True
    )

    notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    options: Mapped[List["CartItemOption"]] = relationship(
        back_populates="cart_item", cascade="all, delete-orphan"
    )

    # spec 129: adiciones (productos-topping) adjuntas a esta línea -- distinto de
    # `options` (sabores de un `OptionGroup`). Ver `CartItemTopping` abajo.
    toppings: Mapped[List["CartItemTopping"]] = relationship(
        back_populates="cart_item", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_cart_item_quantity_positive"),
        CheckConstraint("addons_total >= 0", name="ck_cart_item_addons_total_nonneg"),
        {"schema": "tenant"},
    )


class CartItemOption(UUIDPrimaryKeyMixin, Base):
    """Opción elegida en una línea de carrito (p.ej. un sabor). Espejo de
    `order_item_options`."""

    __tablename__ = "cart_item_options"

    cart_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("cart_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cart_item: Mapped["CartItem"] = relationship(back_populates="options")

    option_id: Mapped[UUID] = mapped_column(
        ForeignKey("options.id"), nullable=False, index=True
    )

    # spec 065: unidades elegidas de esta opción (grupo "cantidad"); siempre 1 para un
    # grupo "conteo" (validado en validate_option_selection, no asumido aquí).
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    # spec 089 (A-94): la opción es un adicional de la regla nueva (cobrado y consumido
    # una vez por línea). Es un snapshot inmutable de qué regla se aplicó: el consumo de
    # inventario (descuento y reversa) lo lee de aquí, nunca de `option_groups.pricing_type`
    # vigente, para que deduct == reverse aunque el grupo cambie de tipo después.
    per_line: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    __table_args__ = (
        UniqueConstraint(
            "cart_item_id", "option_id",
            name="uq__cart_item_options__cart_item_id__option_id",
        ),
        CheckConstraint("quantity > 0", name="ck_cart_item_options_quantity_positive"),
        {"schema": "tenant"},
    )


class CartItemTopping(UUIDPrimaryKeyMixin, Base):
    """Adición (producto-topping) adjunta a una línea de carrito (spec 129). Espejo de
    `CartItemOption`, pero referenciando la variante de un producto marcado
    `is_topping=true` en vez de una `Option` -- las adiciones ya no viven en grupos de
    opciones (research.md D4 de spec 129). Espejo de `OrderItemTopping`."""

    __tablename__ = "cart_item_toppings"

    cart_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("cart_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cart_item: Mapped["CartItem"] = relationship(back_populates="toppings")

    # Variante "principal" (menor display_order) del producto-adición (research.md D5
    # de spec 129) -- no de un producto cualquiera.
    topping_product_variant_id: Mapped[UUID] = mapped_column(
        ForeignKey("product_variants.id"), nullable=False, index=True
    )

    # spec 129 (FR-018): unidades elegidas de esta adición, cantidad libre -- mismo
    # criterio que `CartItemOption.quantity` en modo "cantidad" (spec 065).
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    # Snapshot del precio de catálogo de la adición al agregarla (FR-017: siempre su
    # precio de catálogo, nunca un recargo propio del producto que la ofrece); igual
    # criterio que `CartItem.unit_price` -- se recalcula si el precio de catálogo
    # cambia mientras el carrito sigue abierto, no se congela hasta consolidar.
    unit_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=0, server_default="0"
    )

    __table_args__ = (
        UniqueConstraint(
            "cart_item_id", "topping_product_variant_id",
            name="uq__cart_item_toppings__cart_item_id__topping_variant_id",
        ),
        CheckConstraint("quantity > 0", name="ck_cart_item_toppings_quantity_positive"),
        {"schema": "tenant"},
    )


# spec 129: id del producto-adición, derivado de su variante principal -- el
# cliente (menú QR) trabaja con `topping_product_id`, no con el id de la
# variante (que nunca elige); mismo patrón que `OrderItemOption.name` (spec 087).
from app.models.product_variant import ProductVariant  # noqa: E402

CartItemTopping.topping_product_id = column_property(
    select(ProductVariant.product_id)
    .where(ProductVariant.id == CartItemTopping.topping_product_variant_id)
    .correlate_except(ProductVariant)
    .scalar_subquery()
)
