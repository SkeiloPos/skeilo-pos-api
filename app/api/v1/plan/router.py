from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.core.db import get_db, get_tenant
from app.core.dependencies import get_current_user, require_tenant_admin
from app.core.models import Tenant, User
from app.core import wompi
from app.api.v1.plan import service, subscription_service
from app.api.v1.plan.schemas import PlanSummaryResponse
from app.api.v1.plan.subscription_schemas import PaymentLinkResponse

router = APIRouter(prefix="/plan", tags=["plan"])


@router.get(
    "",
    response_model=PlanSummaryResponse,
    summary="Consultar el plan vigente y el consumo del tenant",
    description=(
        "Nombre del plan, consumo de cada límite, estado de cada módulo, y vencimiento "
        "(Historia de Usuario 6, FR-013). Accesible a cualquier usuario autenticado del "
        "tenant, no solo ADMIN — research.md Decisión 7: el guard de navegación del "
        "frontend lo necesita para cualquier rol."
    ),
)
def get_plan_summary(
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_tenant),
    _: User = Depends(get_current_user),
):
    return service.build_plan_summary(db, tenant)


@router.post(
    "/subscription/payment-link",
    response_model=PaymentLinkResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Pedir el link de pago de la suscripción (Wompi)",
    description=(
        "Entrega el link de pago pendiente vigente del tenant, o crea uno por el precio mensual de su plan "
        "(spec 161, FR-011 a FR-018). Solo el ADMIN del tenant. Responde `201` si creó un pago y `200` si "
        "reutilizó el pendiente vigente. Disponible en `por_vencer`, `en_gracia` y `bloqueada`; con el tenant "
        "bloqueado sigue funcionando (ruta exenta del bloqueo). No almacena tarjetas ni llama a Wompi: la URL "
        "del Web Checkout se firma localmente."
    ),
    responses={
        200: {"description": "Ya había un link pendiente vigente y se devolvió ese mismo (`reused: true`)."},
        403: {"description": "El usuario no es ADMIN del tenant."},
        409: {
            "description": (
                "El tenant no puede pagar por este flujo. Cuerpo `{detail, code: subscription_not_payable, "
                "reason}` con `reason` en `already_active`, `no_expiry`, `unsupported_cycle` o `no_price`."
            )
        },
        503: {"description": "Faltan las credenciales de Wompi. Cuerpo `{detail, code: payment_gateway_not_configured}`."},
    },
)
def create_payment_link(
    response: Response,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_tenant),
    user: User = Depends(require_tenant_admin),
):
    payment, reused = subscription_service.get_or_create_payment_link(db, tenant, user)
    if reused:
        response.status_code = status.HTTP_200_OK
    return PaymentLinkResponse(
        payment_id=payment.id,
        status=payment.status,
        amount=payment.amount,
        currency=wompi.CURRENCY,
        gateway_reference=payment.gateway_reference,
        payment_link_url=payment.payment_link_url,
        payment_link_expires_at=payment.payment_link_expires_at,
        reused=reused,
    )
