from app.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import mapped_column, Mapped, relationship
from sqlalchemy import String, Boolean, DateTime, ForeignKey, CheckConstraint, UniqueConstraint
from typing import TYPE_CHECKING, Optional, List
from datetime import datetime

if TYPE_CHECKING:
    from .category import Category
    from .product_variant import ProductVariant


class Product(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Producto del menú. `preparation_type` = 'prepared' (se arma con receta)
    o 'packaged' (se vende empacado). El precio vive en la variante."""

    __tablename__ = "products"

    category_id: Mapped[UUID] = mapped_column(
        ForeignKey("categories.id"), nullable=False, index=True
    )
    category: Mapped[Optional["Category"]] = relationship(back_populates="products")

    @property
    def category_name(self) -> Optional[str]:
        """Resuelve el nombre de la categoría desde la relación ya cargada -- spec 097
        (research.md D2): evita que el cliente dependa de una lista de categorías
        paginada aparte (`GET /categories`, tope 100) para mostrar este dato."""
        return self.category.name if self.category else None

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    preparation_type: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="prepared"
    )

    image_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Disponibilidad operativa (RF-006): 'agotado temporal' sin dar de baja el
    # producto. Distinto de `active` (alta/baja del catálogo).
    available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # Si el producto exige/aplica la validación y el descuento de inventario de sus
    # presentaciones (spec 003). Default True a nivel de ORM a propósito: protege
    # cualquier construcción de Product(...) que no mencione este campo (tests,
    # scripts) preservando el comportamiento anterior a esta spec. El default False
    # que pide el formulario nuevo vive solo en ProductCreate (spec 027).
    tracks_inventory: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # Trazabilidad del interruptor "Agotado" (spec 093, FR-013): quién marcó/desmarcó
    # `available` por última vez y cuándo. Denormalizado sobre la propia fila para
    # pintar "desde hace X" en el listado sin una consulta adicional por producto; el
    # historial completo (incluidos cambios previos) vive aparte en `AuditLog`
    # (`action="availability_changed"`, ver app/core/audit.py). `NULL` en ambos
    # significa que nadie ha tocado el interruptor dedicado todavía (incluye todo
    # producto creado antes de esta spec) -- no se infiere retroactivamente.
    available_changed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    available_changed_by_name: Mapped[Optional[str]] = mapped_column(
        String(150), nullable=True
    )

    # Los grupos de opciones cuelgan de la VARIANTE (`variant_option_groups`), no de
    # aquí: cuántos sabores se eligen y cuánto descuenta cada uno cambian con el tamaño.
    # order_by (spec 042): quien recorra `product.variants` (p. ej. el Menú QR,
    # menu/router.py) recibe las presentaciones ya en el orden guardado, sin tener que
    # acordarse de ordenar en cada punto de lectura por separado.
    variants: Mapped[List["ProductVariant"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="ProductVariant.display_order",
    )

    __table_args__ = (
        CheckConstraint(
            "preparation_type IN ('prepared', 'packaged')",
            name="ck_product_preparation_type",
        ),
        UniqueConstraint("category_id", "name", name="uq__products__category_id__name"),
        {"schema": "tenant"},
    )
