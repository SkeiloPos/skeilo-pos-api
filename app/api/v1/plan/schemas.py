from typing import Literal, Optional

from pydantic import BaseModel

from app.core.timezone import UtcDatetime


class ResourceUsage(BaseModel):
    used: int
    limit: Optional[int] = None  # None = ilimitado (FR-007)


class ModuleAccess(BaseModel):
    inventario: bool
    promociones: bool


class PlanSummaryResponse(BaseModel):
    """`GET /plan` — consumo del tenant (Historia de Usuario 6, FR-013) y
    datos de gating para el frontend (Historia de Usuario 4, research.md
    Decisión 7)."""

    plan_name: str
    ciclo_facturacion: Optional[Literal["mensual", "anual"]] = None
    plan_vence_en: Optional[UtcDatetime] = None
    vencido: bool

    resources: dict[str, ResourceUsage]
    modules: ModuleAccess
