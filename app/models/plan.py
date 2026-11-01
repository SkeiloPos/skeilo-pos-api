from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Plan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Nivel de suscripción administrado por el Super Admin (spec 033). Siete
    características fijas: cinco límites numéricos (NULL = ilimitado, default
    0 = bloqueado) y dos accesos de módulo (default false = bloqueado) —
    research.md Decisión 1. No configurar una característica explícitamente
    produce el mismo bloqueo que configurarla en su default (FR-002).

    Spec 141 (A-150): los módulos son `inventario` (que incluye Proveedores y
    Compras) y `promociones`; el antiguo `compras_access` se absorbió en
    `inventario_access` y se eliminó."""

    __tablename__ = "plans"

    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Sin `default=`/`server_default=` a propósito: SQLAlchemy omite de el
    # INSERT cualquier columna con un default (de cliente o de servidor)
    # cuyo valor asignado sea `None`, dejando que ese default la reemplace —
    # eso convertiría silenciosamente el sentinel "ilimitado" (FR-007) en
    # "bloqueado" (0) cada vez que alguien lo asignara explícitamente. El
    # "0 si no se configura" de FR-002 se resuelve en la capa Pydantic
    # (`PlanCreate.mesas_limit: int | None = 0`), no en la base de datos —
    # ahí sí se puede distinguir "omitido" de "enviado como null".
    mesas_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cajas_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    usuarios_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    productos_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    metodos_pago_activos_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    inventario_access: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    promociones_access: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    # Precios de referencia (spec 033, ampliación precio/duración/renovación).
    # Sin cobro real asociado — solo el dato que el Super Admin captura
    # manualmente (research.md Decisión 11, Assumptions de spec.md).
    precio_mensual: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    precio_anual: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)

    __table_args__ = ({"schema": "shared"},)
