from typing import Literal, Optional

from pydantic import BaseModel

from app.core.subscription import SubscriptionState
from app.core.timezone import UtcDatetime


class ResourceUsage(BaseModel):
    used: int
    limit: Optional[int] = None  # None = ilimitado (FR-007)


class ModuleAccess(BaseModel):
    inventario: bool
    promociones: bool
    cocina: bool


class PlanSummaryResponse(BaseModel):
    """`GET /plan` — consumo del tenant (Historia de Usuario 6, FR-013) y
    datos de gating para el frontend (Historia de Usuario 4, research.md
    Decisión 7)."""

    plan_name: str
    ciclo_facturacion: Optional[Literal["mensual", "anual"]] = None
    plan_vence_en: Optional[UtcDatetime] = None
    # spec 161 (A-163): `vencido` conserva nombre y tipo pero ahora significa "bloqueada" (gracia de 5 días
    # agotada). Durante la gracia es `False`: el frontend lo usa para ocultar módulos y en gracia todo opera.
    vencido: bool
    # spec 161 (FR-004): estado fino de la suscripción. Aditivos: ningún cliente existente los necesita.
    subscription_state: SubscriptionState
    dias_restantes: Optional[int] = None  # `None` sin vencimiento; ver `compute_subscription_status`
    fecha_limite_pago: Optional[UtcDatetime] = None  # `plan_vence_en + 120 h`; `None` sin vencimiento

    resources: dict[str, ResourceUsage]
    modules: ModuleAccess
