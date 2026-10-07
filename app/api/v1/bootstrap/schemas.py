from typing import Optional

from pydantic import BaseModel

from app.api.v1.notifications.schemas import NotificationListResponse
from app.api.v1.plan.schemas import PlanSummaryResponse
from app.api.v1.tenant.schemas import TenantInfoResponse


class BootstrapResponse(BaseModel):
    """Composición de los 3 bloques que hoy pinta el panel al iniciar sesión
    (spec 100, data-model.md). Cada campo anida, sin modificar, el schema que
    ya usa su endpoint individual — `null` cuando no hay tenant resuelto
    (Super Admin, FR-007) o cuando solo esa fuente falló (FR-006)."""

    tenant: Optional[TenantInfoResponse] = None
    plan: Optional[PlanSummaryResponse] = None
    notifications: Optional[NotificationListResponse] = None
