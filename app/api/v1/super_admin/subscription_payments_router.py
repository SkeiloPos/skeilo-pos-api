"""Historial de pagos de suscripción de un tenant, para soporte y auditoría global del Super Admin (spec 161,
Historia de Usuario 6, FR-034; contrato `subscription-api.md § 4`).

Se incluye en el router de Super Admin (`router.py`), que ya aplica `get_current_super_admin` a todas sus
rutas y está bajo el manejo de errores estandarizado del prefijo `/api/v1/super-admin` (spec 068): un tenant
inexistente responde `404` con ese mismo sobre. No usa `x-tenant-host`: el tenant va en la ruta y se resuelve
contra `shared.tenants`.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.v1.plan import subscription_service
from app.api.v1.plan.subscription_schemas import PaymentHistoryItem, PaymentHistoryResponse
from app.core.crud import get_or_404
from app.core.dependencies import get_shared_db
from app.core.models import Tenant

router = APIRouter()


@router.get(
    "/tenants/{tenant_id}/subscription-payments",
    response_model=PaymentHistoryResponse,
    summary="Historial de pagos de suscripción de un tenant",
    description=(
        "Pagos de suscripción de cualquier tenant, del más reciente al más antiguo y paginados, con los mismos "
        "datos que ve su ADMIN (spec 161, FR-034). No expone el link de pago, la transacción ni la firma."
    ),
    responses={404: {"description": "No existe ese tenant."}},
)
def list_tenant_subscription_payments(
    tenant_id: int,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_shared_db),
):
    tenant = get_or_404(db, Tenant, tenant_id, "No existe ese tenant")
    items, total = subscription_service.list_payments(db, tenant.id, page, size)
    return PaymentHistoryResponse(
        items=[PaymentHistoryItem.model_validate(p) for p in items], total=total, page=page, size=size
    )
