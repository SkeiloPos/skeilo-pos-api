from app.core.models import Base, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import Boolean, String, Integer, Numeric, ForeignKey, CheckConstraint, UniqueConstraint, false
from sqlalchemy.orm import mapped_column, Mapped, relationship, column_property
from sqlalchemy import select
from typing import Optional, List, TYPE_CHECKING
from decimal import Decimal

if TYPE_CHECKING:
    from .customer_order import CustomerOrder

#: Ítems que aún no están terminados. Es la misma regla en tres sitios —bloquear
#: el cobro, cerrar la sesión de mesa y marcar listo un pedido entero—, así que
#: vive junto al modelo dueño del campo en vez de copiarse en cada uno.
EN_CURSO = ("pendiente", "en_preparacion")


class OrderItem(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "order_items"

    order_id: Mapped[UUID] = mapped_column(
        ForeignKey("customer_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order: Mapped["CustomerOrder"] = relationship(back_populates="items")

    # Comensal al que se le carga esta línea. La asignación es **por ítem**, no
    # por pedido: así el split de cuenta es exacto aunque un pedido mezcle
    # comensales. Nullable: mostrador, o línea agregada por el mesero.
    participant_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("session_participants.id"), nullable=True, index=True
    )

    product_variant_id: Mapped[UUID] = mapped_column(
        ForeignKey("product_variants.id"), nullable=False, index=True
    )

    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    unit_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=0, server_default="0"
    )

    # spec 089 (A-94): Σ(precio del adicional × cantidad elegida) de las opciones
    # `per_line`, cobrado UNA vez por línea (copiado del carrito al confirmar, nunca
    # recalculado). Las filas anteriores quedan en 0 y su `unit_price` sigue incluyendo
    # los extras por unidad: el total no cambia (Principio VII).
    addons_total: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=0, server_default="0"
    )

    # Snapshot del descuento vigente al momento de confirmar el pedido (spec
    # 038, FR-013): `None` si ninguna promoción aplicó a esta línea (o es una
    # línea de combo, cuyo ahorro se calcula aparte). Nullable sin default:
    # las filas anteriores a esta spec quedan en NULL, sin backfill (FR-015).
    discounted_unit_price: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    discounted_line_total: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )

    # Promoción de combo que generó esta línea (selección explícita, copiada
    # tal cual desde el cart_item). Varias líneas comparten el mismo combo_id:
    # son los componentes de un mismo combo.
    combo_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("promotions.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Ciclo de preparación del ítem, independiente del status de pago de la orden.
    # Las transiciones pendiente→en_preparacion→listo las mueve la terminal de
    # mesas. 'anulado' se excluye de la validación de bloqueo de cobro.
    estado_cocina: Mapped[str] = mapped_column(
        String(15), nullable=False, server_default="pendiente"
    )

    # Si este ítem reemplaza a uno anulado (void + recreación, Fase 6).
    void_de: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("order_items.id"), nullable=True
    )

    notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    options: Mapped[List["OrderItemOption"]] = relationship(
        back_populates="order_item", cascade="all, delete-orphan"
    )

    @property
    def line_total(self) -> Decimal:
        """spec 089: la única fórmula del total de una línea, sin descuento:
        `unit_price × quantity + addons_total` (con `addons_total = 0` es la de siempre)."""
        return Decimal(self.unit_price) * self.quantity + Decimal(self.addons_total or 0)

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_item_quantity_positive"),
        CheckConstraint("addons_total >= 0", name="ck_order_item_addons_total_nonneg"),
        CheckConstraint(
            "estado_cocina IN ('pendiente', 'en_preparacion', 'listo', 'anulado')",
            name="ck_order_item_estado_cocina",
        ),
        {"schema": "tenant"},
    )


class OrderItemOption(UUIDPrimaryKeyMixin, Base):
    """Opción elegida en una línea de comanda (p.ej. un sabor)."""

    __tablename__ = "order_item_options"

    order_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("order_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_item: Mapped["OrderItem"] = relationship(back_populates="options")

    option_id: Mapped[UUID] = mapped_column(
        ForeignKey("options.id"), nullable=False, index=True
    )

    # spec 065: unidades elegidas de esta opción (grupo "cantidad"); siempre 1 para un
    # grupo "conteo" (validado en validate_option_selection, no asumido aquí).
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    # spec 089 (A-94): ver `CartItemOption.per_line`. Copiado del carrito al confirmar.
    per_line: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    # spec 123: `true` si, al insertarse esta fila (adición agregada después de
    # creada la línea), el pedido ya tenía una `Sale` emitida (`order_has_sale`).
    # Se fija una sola vez al crear la fila; nunca se recalcula después. Las filas
    # anteriores a esta spec (creadas con la línea) quedan en `false` por default,
    # sin excepción (Principio VII).
    added_after_sale: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    # spec 123: `Sale` complementaria que liquidó esta adición. `NULL` mientras
    # el saldo pendiente de la adición sigue sin cobrarse; se fija exactamente
    # una vez, al cobrarse con éxito el pago complementario que la cubre.
    complementary_sale_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("sales.id"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "order_item_id", "option_id",
            name="uq__order_item_options__order_item_id__option_id",
        ),
        CheckConstraint("quantity > 0", name="ck_order_item_options_quantity_positive"),
        {"schema": "tenant"},
    )


# spec 087 (FR-015, A-89, research.md D12): nombre de la opción y de su grupo,
# derivados en lectura con subconsultas escalares correlacionadas. Viajan en el
# mismo SELECT que la fila (sin N+1 y sin tocar los `selectinload(OrderItem.options)`
# existentes). `order_item_options.option_id` es FK a `options.id` sin `ON DELETE`,
# así que una opción referenciada por un pedido no puede borrarse: no hace falta snapshot.
# Se definen después de la clase para poder referenciar `Option`/`OptionGroup`.
from app.models.option import Option  # noqa: E402
from app.models.option_group import OptionGroup  # noqa: E402

OrderItemOption.name = column_property(
    select(Option.name)
    .where(Option.id == OrderItemOption.option_id)
    .correlate_except(Option)
    .scalar_subquery()
)
OrderItemOption.group_name = column_property(
    select(OptionGroup.name)
    .join(Option, Option.option_group_id == OptionGroup.id)
    .where(Option.id == OrderItemOption.option_id)
    .correlate_except(Option, OptionGroup)
    .scalar_subquery()
)
