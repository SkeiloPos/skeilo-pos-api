
from app.core.models import Base,TimestampMixin,UUIDPrimaryKeyMixin
from typing import Optional,List,TYPE_CHECKING
from sqlalchemy.orm  import mapped_column,Mapped,relationship
from sqlalchemy import String,Boolean,Integer,CheckConstraint


if TYPE_CHECKING:
    from .product import Product
    from .presentation import Presentation

class Category(UUIDPrimaryKeyMixin,TimestampMixin,Base):
   __tablename__ = "categories"

   name:Mapped[str]  = mapped_column(String(255),nullable=False,unique=True,index=True)

   description:Mapped[Optional[str]] =  mapped_column(String(255),nullable=True)

   active:Mapped[bool] = mapped_column(Boolean, default=True)

   # spec 128 (FR-001/FR-002): visibilidad específica del menú QR, independiente
   # de `active` -- una categoría solo aparece en el menú QR si ambas son
   # verdaderas a la vez. No tiene relación con `display_order`.
   visible_en_qr:Mapped[bool] = mapped_column(Boolean, default=True)

   display_order:Mapped[int] = mapped_column(Integer, nullable=False)

   products:Mapped[List["Product"]] = relationship(back_populates="category")

   # spec 083 (FR-004): presentaciones del catálogo global habilitadas para
   # crear productos en esta categoría. Reemplazo total en cada guardado
   # (research.md D5) -- no hay columnas propias en la tabla puente.
   presentations:Mapped[List["Presentation"]] = relationship(
       secondary="tenant.category_presentations", order_by="Presentation.name"
   )

   __table_args__ = (
       CheckConstraint("display_order >= 0", name="ck_category_display_order_non_negative"),
       {"schema": "tenant"},
   )