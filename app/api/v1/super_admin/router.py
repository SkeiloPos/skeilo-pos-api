from uuid import UUID
import logging
import sentry_sdk
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import select

from app.core.crud import get_or_404
from app.core.dependencies import get_shared_db, get_current_super_admin
from app.core.pagination import Page, paginate
from app.core.models import User, Tenant
from app.core.plan_limits import calculate_plan_vencimiento, validate_billing_cycle_price
from app.core.timezone import utc_now_naive
from app.models.plan import Plan
from app.api.v1.users.schemas import UserResponse
from app.api.v1.super_admin.schemas import TenantPlanUpdate, TenantResponse,TenantCreateWithUser
from app.api.v1.super_admin.payment_methods_router import router as payment_methods_catalog_router
from app.api.v1.super_admin.plans_router import router as plans_router

from app.celery_task import send_email_task
from app.core.config import settings
from app.core.db import tenant_create
from app.core.dependencies import get_current_super_admin
from app.core.mail_templates import construir_correo_invitacion
from app.core.utils import generate_random_password
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/super-admin",
    tags=["super-admin"],
    dependencies=[Depends(get_current_super_admin)],
)

router.include_router(payment_methods_catalog_router)
router.include_router(plans_router)


@router.get(
    "/users",
    response_model=Page[UserResponse],
    summary="Listar todos los usuarios",
    description="Devuelve, de forma paginada, todos los usuarios del sistema. Permite filtrar por tenant. Solo el super admin.",
    response_description="Página de usuarios.",
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "Se requiere acceso de super admin."},
    },
)
def list_all_users(
    page: int = Query(1, ge=1, description="Número de página (empieza en 1)."),
    size: int = Query(20, ge=1, le=100, description="Cantidad de elementos por página (máximo 100)."),
    tenant_id: int | None = Query(None, description="Filtra los usuarios por tenant."),
    db: Session = Depends(get_shared_db),
):
    stmt = (
        select(User)
        .options(selectinload(User.role), selectinload(User.tenant))
        .order_by(User.created_at.desc())
    )
    if tenant_id is not None:
        stmt = stmt.where(User.tenant_id == tenant_id)
    return paginate(db, stmt, page, size)


@router.get(
    "/tenants",
    response_model=Page[TenantResponse],
    summary="Listar todos los tenants",
    description="Devuelve, de forma paginada, todos los tenants registrados. Solo el super admin.",
    response_description="Página de tenants.",
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "Se requiere acceso de super admin."},
    },
)
def list_all_tenants(
    page: int = Query(1, ge=1, description="Número de página (empieza en 1)."),
    size: int = Query(20, ge=1, le=100, description="Cantidad de elementos por página (máximo 100)."),
    db: Session = Depends(get_shared_db),
):
    stmt = (
        select(Tenant)
        .options(selectinload(Tenant.plan))
        .order_by(Tenant.created_at.desc())
    )
    return paginate(db, stmt, page, size)


@router.patch(
    "/tenants/{tenant_id}",
    response_model=TenantResponse,
    summary="Asignar, cambiar o renovar el plan de un tenant",
    description=(
        "Reasigna el plan vigente de un tenant, lo cambia por otro, o lo renueva (mismo plan) — "
        "las tres son la misma operación: siempre recalcula el período desde este momento "
        "(FR-010/FR-017/FR-018/FR-020, research.md Decisión 16)."
    ),
    responses={
        404: {"description": "No existe ese tenant, o el plan indicado no existe."},
        409: {"description": "El ciclo de facturación elegido no tiene precio definido en ese plan."},
    },
)
def update_tenant_plan(
    tenant_id: int, body: TenantPlanUpdate, db: Session = Depends(get_shared_db),
):
    tenant = get_or_404(db, Tenant, tenant_id, "No existe ese tenant")
    plan = get_or_404(db, Plan, body.plan_id, "No existe ese plan")
    validate_billing_cycle_price(plan, body.ciclo_facturacion)

    tenant.plan_id = plan.id
    tenant.ciclo_facturacion = body.ciclo_facturacion
    if body.ciclo_facturacion is not None:
        tenant.plan_iniciado_en = utc_now_naive()
        tenant.plan_vence_en = calculate_plan_vencimiento(tenant.plan_iniciado_en, body.ciclo_facturacion)
    else:
        tenant.plan_iniciado_en = None
        tenant.plan_vence_en = None

    db.commit()
    db.refresh(tenant)
    return tenant



@router.post("/tenants", dependencies=[Depends(get_current_super_admin)])
def create_tenant(body: TenantCreateWithUser):
    password = generate_random_password()

    tenant_create(
        name=body.tenant_name,
        schema=body.schema_name,
        host=body.host,
        admin_name=body.name,
        admin_email=body.email,
        admin_password=password,
        plan_id=body.plan_id,
        ciclo_facturacion=body.ciclo_facturacion,
    )

    if settings.ENVIRONMENT == "prod":
        login_url = f"https://{body.host}.skeilopos.com/login"
    else:
        login_url = f"http://{body.host}.localhost:4200/login"
    try:
        # FR-026/A-115/D9: el correo del alta de un negocio reusa tal cual
        # la plantilla de invitación, con los mismos campos y el mismo
        # asunto que una invitación de tenant.
        correo = construir_correo_invitacion(
            nombre_negocio=body.tenant_name,
            url_ingreso=login_url,
            correo=body.email,
            contrasena_temporal=password,
        )
        send_email_task.delay(
            recipients=[body.email],
            subject=correo.subject,
            body=correo.html_body,
            text_body=correo.text_body,
        )
    except Exception as exc:
        # El tenant ya fue creado y commiteado; no romper la respuesta si la
        # mensajería (Redis/worker) no está disponible.
        logger.warning(
            "No se pudo encolar el correo de bienvenida para el tenant '%s'",
            body.tenant_name,
            exc_info=True,
        )
        # Visibilidad en producción sin afectar la respuesta (spec 068,
        # research.md § 9): no-op fuera de "prod" porque ahí sentry_sdk nunca
        # se inicializó (app/main.py).
        sentry_sdk.capture_exception(exc)

    return {"status": "ok", "tenant": body.tenant_name}
