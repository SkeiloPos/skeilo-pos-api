from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from uuid import UUID
from zoneinfo import ZoneInfo
import logging
import sentry_sdk
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import select, func, literal, or_, union_all

from app.core.crud import get_or_404
from app.core.dependencies import get_shared_db, get_current_super_admin
from app.core.pagination import Page, paginate
from app.core.models import User, Tenant, Role, UserInvitation
from app.core.plan_limits import calculate_plan_vencimiento, validate_billing_cycle_price
from app.core.timezone import utc_now, utc_now_naive
from app.models.plan import Plan
from app.models.tenant_membership import TenantMembership
from app.models.user_session import UserSession
from app.api.v1.super_admin.schemas import (
    TenantPlanUpdate, TenantResponse, TenantCreateWithUser, UserListItem, UserListPage, RoleOption,
    UserStats,
)
from app.api.v1.super_admin.audit_router import router as audit_router
from app.api.v1.super_admin.payment_methods_router import router as payment_methods_catalog_router
from app.api.v1.super_admin.plans_router import router as plans_router
from app.api.v1.super_admin.users_router import router as users_detail_router

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
router.include_router(audit_router)


# spec 159, Edge case "Sin actividad reciente": el spec no fija un número
# exacto de días -- se elige 30 (ventana mensual, consistente con el
# indicador "nuevos del mes" de FR-008) como umbral razonable. Es puramente
# visual (no cambia `status`, FR-031), así que ajustarlo a futuro no exige
# migración.
INACTIVITY_THRESHOLD_DAYS = 30

_VALID_SORT_BY = {"name", "last_login_at", "created_at"}


def _build_users_listing_query(
    *,
    search: str | None,
    status: list[str] | None,
    role_id: UUID | None,
    plan_id: UUID | None,
    registered_from: date | None,
    registered_to: date | None,
):
    """Construye el `SELECT` unificado de `User` + `UserInvitation` pendiente
    (research.md Decisión 3), proyectando ambas fuentes a las mismas columnas
    de `UserListItem` vía `UNION ALL`, para poder filtrar/ordenar/paginar el
    conjunto como una sola lista.
    """
    memberships_count_sq = (
        select(func.count(TenantMembership.id))
        .where(TenantMembership.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    memberships_preview_sq = (
        select(Tenant.name)
        .join(TenantMembership, TenantMembership.tenant_id == Tenant.id)
        .where(TenantMembership.user_id == User.id)
        .order_by(TenantMembership.created_at.asc())
        .limit(1)
        .correlate(User)
        .scalar_subquery()
    )
    # research.md Decisión 5: no existe `users.last_login_at` -- cada login
    # crea una fila en `user_sessions`, así que "último acceso" es el
    # `created_at` más reciente de las sesiones del usuario (incluye
    # sesiones ya revocadas: siguen representando un login real).
    last_login_at_sq = (
        select(func.max(UserSession.created_at))
        .where(UserSession.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )

    users_stmt = (
        select(
            User.id.label("id"),
            User.name.label("name"),
            User.email.label("email"),
            Role.name.label("role_name"),
            User.status.label("status"),
            memberships_count_sq.label("memberships_count"),
            memberships_preview_sq.label("memberships_preview"),
            Plan.name.label("plan_name"),
            last_login_at_sq.label("last_login_at"),
            User.created_at.label("created_at"),
        )
        .join(Role, Role.id == User.role_id, isouter=True)
        .join(Tenant, Tenant.id == User.tenant_id, isouter=True)
        .join(Plan, Plan.id == Tenant.plan_id, isouter=True)
    )

    invitations_stmt = (
        select(
            UserInvitation.id.label("id"),
            UserInvitation.name.label("name"),
            UserInvitation.email.label("email"),
            Role.name.label("role_name"),
            literal("invitation_pending").label("status"),
            literal(0).label("memberships_count"),
            literal(None).label("memberships_preview"),
            Plan.name.label("plan_name"),
            literal(None).label("last_login_at"),
            UserInvitation.created_at.label("created_at"),
        )
        .join(Role, Role.id == UserInvitation.role_id, isouter=True)
        .join(Tenant, Tenant.id == UserInvitation.tenant_id, isouter=True)
        .join(Plan, Plan.id == Tenant.plan_id, isouter=True)
        .where(UserInvitation.status == "pending")
    )

    status_values = set(status) if status else None

    if status_values is not None:
        users_statuses = status_values - {"invitation_pending"}
        if users_statuses:
            users_stmt = users_stmt.where(User.status.in_(users_statuses))
        else:
            users_stmt = users_stmt.where(literal(False))
        include_invitations = "invitation_pending" in status_values
    else:
        include_invitations = True

    if role_id is not None:
        users_stmt = users_stmt.where(User.role_id == role_id)
        invitations_stmt = invitations_stmt.where(UserInvitation.role_id == role_id)

    if plan_id is not None:
        users_stmt = users_stmt.where(Tenant.plan_id == plan_id)
        invitations_stmt = invitations_stmt.where(Tenant.plan_id == plan_id)

    if registered_from is not None:
        users_stmt = users_stmt.where(func.date(User.created_at) >= registered_from)
        invitations_stmt = invitations_stmt.where(func.date(UserInvitation.created_at) >= registered_from)

    if registered_to is not None:
        users_stmt = users_stmt.where(func.date(User.created_at) <= registered_to)
        invitations_stmt = invitations_stmt.where(func.date(UserInvitation.created_at) <= registered_to)

    if search:
        term = f"%{search}%"
        users_stmt = users_stmt.where(
            or_(User.name.ilike(term), User.email.ilike(term), Tenant.name.ilike(term))
        )
        invitations_stmt = invitations_stmt.where(
            or_(UserInvitation.name.ilike(term), UserInvitation.email.ilike(term), Tenant.name.ilike(term))
        )

    if include_invitations:
        return union_all(users_stmt, invitations_stmt)
    return union_all(users_stmt)


def _paginate_listing(db: Session, unioned, page: int, size: int, sort_by: str, sort_dir: str) -> dict:
    subquery = unioned.subquery()

    total = db.execute(select(func.count()).select_from(subquery)).scalar_one()

    sort_column = {
        "name": subquery.c.name,
        "last_login_at": subquery.c.last_login_at,
        "created_at": subquery.c.created_at,
    }[sort_by]
    order_expr = sort_column.desc() if sort_dir == "desc" else sort_column.asc()
    # NULLS LAST en ambas direcciones: un usuario sin "último acceso" (nunca
    # ha iniciado sesión, o es una invitación pendiente) no debe mezclarse
    # arbitrariamente al ordenar ascendente/descendente.
    order_expr = order_expr.nulls_last()

    rows = db.execute(
        select(subquery)
        .order_by(order_expr, subquery.c.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    ).all()

    now = utc_now_naive()
    threshold = now - timedelta(days=INACTIVITY_THRESHOLD_DAYS)
    items = []
    for row in rows:
        data = dict(row._mapping)
        last_login_at = data.get("last_login_at")
        data["is_inactive_recently"] = bool(last_login_at and last_login_at < threshold)
        items.append(UserListItem.model_validate(data))

    from math import ceil

    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "pages": ceil(total / size) if total else 0,
    }


@router.get(
    "/roles",
    response_model=list[RoleOption],
    summary="Listar el catálogo de roles",
    description=(
        "Catálogo global y fijo de roles (sembrado por migración), para poblar el "
        "selector de filtro `role_id` del listado de usuarios (spec 159, FR-005)."
    ),
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "Se requiere acceso de super admin."},
    },
)
def list_roles(db: Session = Depends(get_shared_db)):
    return db.execute(select(Role).order_by(Role.name)).scalars().all()


@router.get(
    "/users",
    response_model=UserListPage,
    summary="Listar todos los usuarios de la plataforma",
    description=(
        "Listado unificado y paginado de usuarios e invitaciones pendientes "
        "(spec 159, research.md Decisión 3), con búsqueda y filtros combinables "
        "(FR-001 a FR-007). Solo el super admin."
    ),
    response_description="Página de usuarios (y, si aplica, invitaciones pendientes).",
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "Se requiere acceso de super admin."},
    },
)
def list_all_users(
    page: int = Query(1, ge=1, description="Número de página (empieza en 1)."),
    size: int = Query(20, ge=1, le=100, description="Cantidad de elementos por página (máximo 100)."),
    search: str | None = Query(None, description="Busca por nombre, correo o nombre de negocio (FR-004)."),
    status: list[str] | None = Query(None, description="Filtra por uno o más estados (FR-005)."),
    role_id: UUID | None = Query(None, description="Filtra por rol."),
    plan_id: UUID | None = Query(None, description="Filtra por el plan del negocio principal."),
    registered_from: date | None = Query(None, description="Fecha de registro mínima (inclusive)."),
    registered_to: date | None = Query(None, description="Fecha de registro máxima (inclusive)."),
    sort_by: str = Query("created_at", description="name|last_login_at|created_at (FR-006)."),
    sort_dir: str = Query("desc", description="asc|desc."),
    db: Session = Depends(get_shared_db),
):
    if sort_by not in _VALID_SORT_BY:
        sort_by = "created_at"
    if sort_dir not in {"asc", "desc"}:
        sort_dir = "desc"

    unioned = _build_users_listing_query(
        search=search,
        status=status,
        role_id=role_id,
        plan_id=plan_id,
        registered_from=registered_from,
        registered_to=registered_to,
    )
    page_result = _paginate_listing(db, unioned, page, size, sort_by, sort_dir)

    # FR-041: distingue "sin datos de plataforma" (total=0 sin ningún filtro)
    # de "sin resultados" (total=0 con algún filtro/búsqueda activo). Se
    # expone como un campo plano en la respuesta -- más simple que forzar al
    # frontend a repetir la misma lógica de "¿qué filtros mandé?" para decidir
    # el mensaje.
    has_active_filters = any(
        [search, status, role_id, plan_id, registered_from, registered_to]
    )
    page_result["empty_reason"] = (
        None if page_result["total"] else ("no_results" if has_active_filters else "no_data")
    )
    return page_result


def _current_platform_month_bounds_utc() -> tuple[datetime, datetime]:
    """Límites [inicio, fin) del mes calendario en curso, en la "zona horaria
    de la plataforma" (FR-008) -- este endpoint es cross-tenant (incluye
    Super Admins sin `tenant_id`), así que no hay un tenant del que derivar
    `resolve_timezone`; se usa `settings.TENANT_TIMEZONE` (mismo respaldo que
    `resolve_timezone` aplica cuando el tenant no trae zona propia), igual que
    `local_day_bounds_utc` pero para el mes en vez del día. Devuelve datetimes
    naive en UTC, el mismo tipo que las columnas `DateTime` que compara."""
    tz = ZoneInfo(settings.TENANT_TIMEZONE)
    today_local = utc_now().astimezone(tz).date()
    start_local = date(today_local.year, today_local.month, 1)
    if start_local.month == 12:
        end_local = date(start_local.year + 1, 1, 1)
    else:
        end_local = date(start_local.year, start_local.month + 1, 1)
    start_utc = datetime.combine(start_local, time.min, tzinfo=tz).astimezone(dt_timezone.utc).replace(tzinfo=None)
    end_utc = datetime.combine(end_local, time.min, tzinfo=tz).astimezone(dt_timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


@router.get(
    "/users/stats",
    response_model=UserStats,
    summary="Indicadores generales de usuarios",
    description=(
        "Cuatro indicadores resumidos sobre `User` (FR-008): total registrado, "
        "activos, suspendidos y nuevos del mes calendario en curso (zona "
        "horaria de la plataforma). No incluye invitaciones pendientes (no son "
        "todavía un usuario registrado, research.md Decisión 3). Se consulta "
        "de nuevo tras cualquier acción administrativa que cambie el estado de "
        "un usuario (FR-009), para mantenerse consistente sin recargar la página."
    ),
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "Se requiere acceso de super admin."},
    },
)
def get_users_stats(db: Session = Depends(get_shared_db)):
    month_start, month_end = _current_platform_month_bounds_utc()

    total = db.execute(select(func.count()).select_from(User)).scalar_one()
    active = db.execute(
        select(func.count()).select_from(User).where(User.status == "active")
    ).scalar_one()
    suspended = db.execute(
        select(func.count()).select_from(User).where(User.status == "suspended")
    ).scalar_one()
    new_this_month = db.execute(
        select(func.count())
        .select_from(User)
        .where(User.created_at >= month_start, User.created_at < month_end)
    ).scalar_one()

    return UserStats(total=total, active=active, suspended=suspended, new_this_month=new_this_month)


# Registrado aquí (tras `/users` y `/users/stats`), no junto a los otros
# `include_router` arriba: FastAPI resuelve rutas en orden de registro, y
# `/users/{user_id}` (definido en `users_detail_router`) atraparía
# `/users/stats` si se incluyera antes de que ese literal exista.
router.include_router(users_detail_router)


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
        timezone=body.timezone,
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
