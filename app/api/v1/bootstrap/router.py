from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.v1.bootstrap import service
from app.api.v1.bootstrap.schemas import BootstrapResponse
from app.core.dependencies import get_authenticated_user, get_optional_db, get_optional_tenant
from app.core.models import Tenant, User

router = APIRouter(prefix="/bootstrap", tags=["bootstrap"])


@router.get(
    "",
    response_model=BootstrapResponse,
    summary="Arranque agregado del panel (negocio + plan + notificaciones recientes)",
    description=(
        "Compone en una sola respuesta lo que hoy entregan `GET /tenant`, `GET /plan` y "
        "`GET /notifications?page=1&size=20` por separado (spec 100, FR-001/FR-002). "
        "Usa `get_authenticated_user` en vez de `get_current_user` porque no exige que el "
        "tenant exista: el Super Admin recibe `200` con los 3 bloques en `null` (FR-007)."
    ),
)
def get_bootstrap(
    db: Session = Depends(get_optional_db),
    tenant: Optional[Tenant] = Depends(get_optional_tenant),
    user: User = Depends(get_authenticated_user),
):
    return service.build_bootstrap(db, tenant, user)
