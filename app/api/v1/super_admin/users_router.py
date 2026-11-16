"""Ficha individual de usuario (spec 159, User Story 3/4, FR-010 a FR-022).

Información general/actividad/seguridad/negocios asociados (solo lectura --
la revocación de sesiones es User Story 6) más la administración básica de
User Story 4: editar, activar/reactivar/desbloquear, suspender y enviar
recuperación de contraseña. Registrado bajo el mismo prefijo `/super-admin`
que `router.py`, reutilizando `get_current_super_admin` (sin duplicar la
dependencia aquí)."""

import hashlib
import secrets
from datetime import timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi import Depends
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.celery_task import send_email_task
from app.core.config import settings
from app.core.crud import get_or_404
from app.core.dependencies import get_current_super_admin, get_shared_db
from app.core.mail_templates import construir_correo_restablecimiento
from app.core.models import PasswordResetToken, Role, Tenant, User
from app.core.pagination import Page
from app.core.reserved_hosts import PLATFORM_HOST
from app.core.timezone import utc_now_naive
from app.core.utils import generate_passwd_hash
from app.models.admin_audit_log import AdminAuditLog
from app.models.tenant_membership import TenantMembership
from app.models.user_session import UserSession
from app.services.admin_audit_log_service import AdminAuditLogService
from app.services.super_admin_guard_service import ensure_action_keeps_a_super_admin_active
from app.services.user_retention_service import find_retention_tenants
from app.api.v1.super_admin.schemas import (
    AdminAuditLogItem,
    ConfirmEmailRequest,
    DeleteBlockedByRetention,
    MembershipItem,
    SuspendUserRequest,
    UserDetail,
    UserSecurity,
    UserSessionItem,
    UserUpdate,
)

import logging

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_user_or_404(db: Session, user_id: UUID) -> User:
    return get_or_404(db, User, user_id, "No existe ese usuario")


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _to_user_detail(db: Session, user: User) -> UserDetail:
    last_login_at = db.execute(
        select(UserSession.created_at)
        .where(UserSession.user_id == user.id)
        .order_by(UserSession.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return UserDetail(
        id=user.id,
        name=user.name,
        email=user.email,
        status=user.status,
        created_at=user.created_at,
        last_login_at=last_login_at,
    )


@router.get(
    "/users/{user_id}",
    response_model=UserDetail,
    summary="Información general de un usuario",
    description=(
        "Pestaña «información general» de la ficha de usuario (FR-011): "
        "nombre, correo, fecha de registro, estado y último acceso."
    ),
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "Se requiere acceso de super admin."},
        404: {"description": "No existe ese usuario."},
    },
)
def get_user_detail(user_id: UUID, db: Session = Depends(get_shared_db)):
    user = _get_user_or_404(db, user_id)
    return _to_user_detail(db, user)


@router.get(
    "/users/{user_id}/activity",
    response_model=Page[AdminAuditLogItem],
    summary="Historial de actividad de un usuario",
    description=(
        "Pestaña «actividad» de la ficha de usuario (FR-012): historial "
        "cronológico (más reciente primero) de `AdminAuditLog` filtrado por "
        "`affected_user_id`, indicando quién hizo cada cambio administrativo "
        "(`actor_type`/`actor_name_snapshot`). No existe hoy en `pos-backend` "
        "un registro propio de eventos de login/cambio de contraseña fuera de "
        "esta bitácora, así que por ahora esta pestaña muestra únicamente "
        "`AdminAuditLog` -- si en el futuro se agrega un registro de eventos "
        "propios del usuario, se fusionaría aquí (research.md Decisión 6)."
    ),
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "Se requiere acceso de super admin."},
        404: {"description": "No existe ese usuario."},
    },
)
def get_user_activity(
    user_id: UUID,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_shared_db),
):
    _get_user_or_404(db, user_id)
    from app.core.pagination import paginate

    stmt = (
        select(AdminAuditLog)
        .where(AdminAuditLog.affected_user_id == user_id)
        .order_by(AdminAuditLog.created_at.desc())
    )
    return paginate(db, stmt, page, size)


@router.get(
    "/users/{user_id}/memberships",
    response_model=list[MembershipItem],
    summary="Negocios asociados a un usuario",
    description=(
        "Pestaña «negocios asociados» de la ficha de usuario (FR-014): "
        "negocio, plan, rol y fecha de ingreso por cada membresía "
        "(research.md Decisión 1). Vacío cuando el usuario no pertenece a "
        "ningún negocio (FR-015)."
    ),
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "Se requiere acceso de super admin."},
        404: {"description": "No existe ese usuario."},
    },
)
def get_user_memberships(user_id: UUID, db: Session = Depends(get_shared_db)):
    _get_user_or_404(db, user_id)
    rows = db.execute(
        select(
            TenantMembership.tenant_id,
            Tenant.name.label("tenant_name"),
            Tenant.status.label("tenant_status"),
            Tenant.plan_id,
            Role.name.label("role_name"),
            TenantMembership.created_at.label("member_since"),
        )
        .join(Tenant, Tenant.id == TenantMembership.tenant_id)
        .join(Role, Role.id == TenantMembership.role_id)
        .where(TenantMembership.user_id == user_id)
        .order_by(TenantMembership.created_at.asc())
    ).all()

    if not rows:
        return []

    from app.models.plan import Plan

    plan_ids = {row.plan_id for row in rows}
    plan_names = dict(
        db.execute(select(Plan.id, Plan.name).where(Plan.id.in_(plan_ids))).all()
    )

    return [
        MembershipItem(
            tenant_id=row.tenant_id,
            tenant_name=row.tenant_name,
            tenant_status=row.tenant_status,
            plan_name=plan_names.get(row.plan_id),
            role_name=row.role_name,
            member_since=row.member_since,
        )
        for row in rows
    ]


@router.get(
    "/users/{user_id}/security",
    response_model=UserSecurity,
    summary="Seguridad de un usuario",
    description=(
        "Pestaña «seguridad» de la ficha de usuario (FR-013/FR-028/FR-029): "
        "sesiones activas (dispositivo, navegador, IP) e intentos fallidos "
        "de inicio de sesión. Las sesiones estarán vacías hasta que User "
        "Story 6 conecte `POST /auth/login` con `shared.user_sessions`."
    ),
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "Se requiere acceso de super admin."},
        404: {"description": "No existe ese usuario."},
    },
)
def get_user_security(user_id: UUID, db: Session = Depends(get_shared_db)):
    user = _get_user_or_404(db, user_id)
    active_sessions = db.execute(
        select(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .order_by(UserSession.last_seen_at.desc())
    ).scalars().all()
    return UserSecurity(
        active_sessions=[UserSessionItem.model_validate(s) for s in active_sessions],
        failed_login_attempts=user.failed_login_attempts,
        security_blocked_at=user.security_blocked_at,
    )


@router.patch(
    "/users/{user_id}",
    response_model=UserDetail,
    summary="Editar un usuario",
    description=(
        "Edita nombre, rol y/o el negocio asociado de un usuario (FR-016/"
        "FR-017). `membership_tenant_id`/`membership_role_id` crean o "
        "actualizan (upsert) la membresía de ese negocio (research.md "
        "Decisión 1). Registra `profile_updated` y/o `role_changed` en la "
        "auditoría administrativa."
    ),
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "Se requiere acceso de super admin."},
        404: {"description": "No existe ese usuario (o el rol/negocio indicado)."},
        409: {"description": "El usuario está anonimizado (FR-027)."},
    },
)
def update_user(
    user_id: UUID,
    body: UserUpdate,
    request: Request,
    db: Session = Depends(get_shared_db),
    actor: User = Depends(get_current_super_admin),
):
    user = _get_user_or_404(db, user_id)
    if user.status == "anonymized":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No se puede editar un usuario anonimizado.",
        )

    ip_address = _client_ip(request)

    if body.name is not None and body.name != user.name:
        previous_name = user.name
        user.name = body.name
        AdminAuditLogService.record(
            db,
            actor_type="admin",
            actor_user=actor,
            affected_user=user,
            tenant_id=user.tenant_id,
            action="profile_updated",
            detail={"nombre_anterior": previous_name, "nombre_nuevo": body.name},
            ip_address=ip_address,
        )

    if body.role_id is not None and body.role_id != user.role_id:
        new_role = get_or_404(db, Role, body.role_id, "No existe ese rol")
        previous_role = user.role_name
        user.role_id = new_role.id
        AdminAuditLogService.record(
            db,
            actor_type="admin",
            actor_user=actor,
            affected_user=user,
            tenant_id=user.tenant_id,
            action="role_changed",
            detail={"rol_anterior": previous_role, "rol_nuevo": new_role.name},
            ip_address=ip_address,
        )

    if body.membership_tenant_id is not None:
        tenant = get_or_404(db, Tenant, body.membership_tenant_id, "No existe ese negocio")
        role = get_or_404(db, Role, body.membership_role_id, "No existe ese rol")
        membership = db.execute(
            select(TenantMembership).where(
                TenantMembership.user_id == user.id,
                TenantMembership.tenant_id == tenant.id,
            )
        ).scalar_one_or_none()
        if membership is None:
            db.add(TenantMembership(user_id=user.id, tenant_id=tenant.id, role_id=role.id))
            detail = {"negocio": tenant.name, "rol": role.name}
        else:
            previous_role_name = membership.role.name if membership.role else None
            membership.role_id = role.id
            detail = {
                "negocio": tenant.name,
                "rol_anterior": previous_role_name,
                "rol_nuevo": role.name,
            }
        AdminAuditLogService.record(
            db,
            actor_type="admin",
            actor_user=actor,
            affected_user=user,
            tenant_id=tenant.id,
            action="profile_updated",
            detail=detail,
            ip_address=ip_address,
        )

    db.commit()
    db.refresh(user)
    return _to_user_detail(db, user)


@router.post(
    "/users/{user_id}/activate",
    response_model=UserDetail,
    summary="Activar, reactivar o desbloquear a un usuario",
    description=(
        "Un solo endpoint cubre activación manual (`pending_activation`), "
        "reactivación (`suspended`) y desbloqueo manual "
        "(`blocked_security`) -- la transición depende del `status` actual "
        "(FR-018). Limpia `suspended_at`/`suspended_reason` o "
        "`security_blocked_at`/`failed_login_attempts` según corresponda."
    ),
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "Se requiere acceso de super admin."},
        404: {"description": "No existe ese usuario."},
        409: {"description": "El usuario está anonimizado (estado terminal)."},
    },
)
async def activate_user(
    user_id: UUID,
    request: Request,
    db: Session = Depends(get_shared_db),
    actor: User = Depends(get_current_super_admin),
):
    user = _get_user_or_404(db, user_id)

    if user.status == "anonymized":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No se puede activar un usuario anonimizado.",
        )

    previous_status = user.status
    if previous_status == "active":
        # Idempotente: ya está activo, no hay transición que registrar.
        return _to_user_detail(db, user)

    now = utc_now_naive()
    if previous_status == "pending_activation":
        user.activated_at = now
    elif previous_status == "suspended":
        user.suspended_at = None
        user.suspended_reason = None
    elif previous_status == "blocked_security":
        user.security_blocked_at = None
        user.failed_login_attempts = 0
        # El desbloqueo manual también debe limpiar la ventana deslizante de
        # Redis (no solo el snapshot persistido): si no, un intento fallido
        # posterior dentro de la misma ventana vuelve a cruzar el umbral de
        # inmediato y re-bloquea a un usuario que el Super Admin acaba de
        # desbloquear (gap encontrado en la validación end-to-end, T100).
        from app.api.v1.auth.routes import _login_lockout_key, _reset_failed_login_counter

        await _reset_failed_login_counter(
            user, _login_lockout_key(user.tenant_id, user.email.strip().lower())
        )

    user.status = "active"
    user.active = True

    AdminAuditLogService.record(
        db,
        actor_type="admin",
        actor_user=actor,
        affected_user=user,
        tenant_id=user.tenant_id,
        action="activated",
        detail={"estado_anterior": previous_status},
        ip_address=_client_ip(request),
    )

    db.commit()
    db.refresh(user)
    return _to_user_detail(db, user)


@router.post(
    "/users/{user_id}/suspend",
    response_model=UserDetail,
    summary="Suspender a un usuario",
    description=(
        "Suspende a un usuario, exigiendo un motivo (FR-019). `409` si es el "
        "último Super Admin activo (research.md Decisión 8). Si "
        "`revoke_sessions`, revoca todas sus sesiones activas en la misma "
        "transacción."
    ),
    responses={
        400: {"description": "Falta el motivo de suspensión."},
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "Se requiere acceso de super admin."},
        404: {"description": "No existe ese usuario."},
        409: {"description": "El usuario es el último Super Admin activo, o ya está anonimizado."},
    },
)
def suspend_user(
    user_id: UUID,
    body: SuspendUserRequest,
    request: Request,
    db: Session = Depends(get_shared_db),
    actor: User = Depends(get_current_super_admin),
):
    user = _get_user_or_404(db, user_id)

    if not body.reason.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes indicar un motivo de suspensión.",
        )

    if user.status == "anonymized":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No se puede suspender un usuario anonimizado.",
        )

    ensure_action_keeps_a_super_admin_active(db, user)

    now = utc_now_naive()
    user.status = "suspended"
    user.active = False
    user.suspended_at = now
    user.suspended_reason = body.reason.strip()

    ip_address = _client_ip(request)

    revoked_count = 0
    if body.revoke_sessions:
        active_sessions = db.execute(
            select(UserSession).where(
                UserSession.user_id == user.id, UserSession.revoked_at.is_(None)
            )
        ).scalars().all()
        for session_row in active_sessions:
            session_row.revoked_at = now
        revoked_count = len(active_sessions)

    AdminAuditLogService.record(
        db,
        actor_type="admin",
        actor_user=actor,
        affected_user=user,
        tenant_id=user.tenant_id,
        action="suspended",
        detail={"motivo": body.reason.strip(), "sesiones_revocadas": revoked_count},
        ip_address=ip_address,
    )

    db.commit()
    db.refresh(user)
    return _to_user_detail(db, user)


def _revoke_all_sessions_inline(db: Session, user: User, now) -> int:
    """Mismo efecto que `revoke_all_sessions` pero invocable dentro de otra
    transacción ya abierta (anonimizar/suspender) -- no hace su propio
    `commit`, el caller decide cuándo confirmar."""
    active_sessions = db.execute(
        select(UserSession).where(
            UserSession.user_id == user.id, UserSession.revoked_at.is_(None)
        )
    ).scalars().all()
    for session_row in active_sessions:
        session_row.revoked_at = now
    user.tokens_valid_after = now
    return len(active_sessions)


@router.post(
    "/users/{user_id}/anonymize",
    response_model=UserDetail,
    summary="Anonimizar un usuario",
    description=(
        "Reemplaza irreversiblemente nombre/correo, invalida la contraseña, "
        "cierra todas las sesiones activas y marca `status='anonymized'` "
        "(FR-023/FR-025, research.md Decisión 7). Preserva la fila `User` y "
        "sus membresías -- el historial transaccional ya ligado a su "
        "`user_id` en cada tenant sigue existiendo sin datos personales "
        "legibles. `confirm_email` debe coincidir EXACTAMENTE con el correo "
        "actual."
    ),
    responses={
        400: {"description": "`confirm_email` no coincide con el correo actual."},
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "Se requiere acceso de super admin."},
        404: {"description": "No existe ese usuario."},
        409: {"description": "Ya está anonimizado, o es el último Super Admin activo."},
    },
)
def anonymize_user(
    user_id: UUID,
    body: ConfirmEmailRequest,
    request: Request,
    db: Session = Depends(get_shared_db),
    actor: User = Depends(get_current_super_admin),
):
    user = _get_user_or_404(db, user_id)

    if user.status == "anonymized":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Este usuario ya está anonimizado.",
        )

    if body.confirm_email != user.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El correo de confirmación no coincide con el correo actual del usuario.",
        )

    ensure_action_keeps_a_super_admin_active(db, user)

    ip_address = _client_ip(request)

    # Se registra ANTES de mutar nombre/correo: `AdminAuditLogService.record`
    # toma el snapshot de `affected_user.email` en el momento de la llamada
    # (FR-038 -- la bitácora conserva el correo ORIGINAL, no el anonimizado).
    AdminAuditLogService.record(
        db,
        actor_type="admin",
        actor_user=actor,
        affected_user=user,
        tenant_id=user.tenant_id,
        action="anonymized",
        detail={"nombre_anterior": user.name},
        ip_address=ip_address,
    )

    now = utc_now_naive()
    user.name = "Usuario anonimizado"
    user.email = f"anon-{uuid4()}@anonimizado.invalid"
    user.password_hash = generate_passwd_hash(secrets.token_urlsafe(32))
    user.status = "anonymized"
    user.active = False
    user.anonymized_at = now
    _revoke_all_sessions_inline(db, user, now)

    db.commit()
    db.refresh(user)
    return _to_user_detail(db, user)


@router.delete(
    "/users/{user_id}",
    summary="Eliminar permanentemente un usuario",
    description=(
        "Elimina la fila `User` y sus membresías, solo si no existe ninguna "
        "obligación de retención (FR-024/Edge case) ni se trataría del "
        "último Super Admin activo (research.md Decisión 8). Registra el "
        "`AdminAuditLog` ANTES de borrar, para que la referencia blanda a "
        "`affected_user_id` sobreviva al borrado (FR-038). `confirm_email` "
        "debe coincidir EXACTAMENTE con el correo actual."
    ),
    responses={
        400: {"description": "`confirm_email` no coincide con el correo actual."},
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "Se requiere acceso de super admin."},
        404: {"description": "No existe ese usuario."},
        409: {
            "description": (
                "Existe una obligación de retención vigente (ver "
                "`retention_tenants`), o es el último Super Admin activo."
            ),
            "model": DeleteBlockedByRetention,
        },
    },
)
def delete_user(
    user_id: UUID,
    body: ConfirmEmailRequest,
    request: Request,
    db: Session = Depends(get_shared_db),
    actor: User = Depends(get_current_super_admin),
):
    user = _get_user_or_404(db, user_id)

    if body.confirm_email != user.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El correo de confirmación no coincide con el correo actual del usuario.",
        )

    ensure_action_keeps_a_super_admin_active(db, user)

    retention_tenants = find_retention_tenants(db, user)
    if retention_tenants:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=DeleteBlockedByRetention(
                reason=(
                    "No se puede eliminar: existen facturas u órdenes de este "
                    "usuario en al menos un negocio. Puedes anonimizarlo en su "
                    "lugar."
                ),
                retention_tenants=retention_tenants,
            ).model_dump(mode="json"),
        )

    AdminAuditLogService.record(
        db,
        actor_type="admin",
        actor_user=actor,
        affected_user=user,
        tenant_id=user.tenant_id,
        action="deleted",
        detail={"nombre": user.name},
        ip_address=_client_ip(request),
    )

    db.delete(user)
    db.commit()
    return {"message": "El usuario fue eliminado permanentemente."}


def _build_reset_url_for_user(user: User, raw_token: str) -> str:
    """Igual que `auth/routes._build_reset_url`, pero sin requerir un
    `Tenant` del `request` (este endpoint es cross-tenant): usa el host del
    negocio del usuario, o `PLATFORM_HOST` si es un Super Admin sin tenant
    (mismo patrón que `auth/routes._build_login_url_for_user`)."""
    host = PLATFORM_HOST if user.tenant_id is None else user.tenant.host
    if settings.ENVIRONMENT == "prod":
        return f"https://{host}.skeilopos.com/reset-password?token={raw_token}"
    return f"http://{host}.localhost:4200/reset-password?token={raw_token}"


@router.post(
    "/users/{user_id}/sessions/revoke-all",
    response_model=UserSecurity,
    summary="Revocar todas las sesiones activas de un usuario",
    description=(
        "Marca `revoked_at` en todas las sesiones activas del usuario y "
        "sube `tokens_valid_after` como defensa adicional para cualquier "
        "token ya emitido sin `sid` (FR-022/FR-030, research.md Decisión 5)."
    ),
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "Se requiere acceso de super admin."},
        404: {"description": "No existe ese usuario."},
    },
)
def revoke_all_sessions(
    user_id: UUID,
    request: Request,
    db: Session = Depends(get_shared_db),
    actor: User = Depends(get_current_super_admin),
):
    user = _get_user_or_404(db, user_id)
    now = utc_now_naive()

    active_sessions = db.execute(
        select(UserSession).where(
            UserSession.user_id == user.id, UserSession.revoked_at.is_(None)
        )
    ).scalars().all()
    for session_row in active_sessions:
        session_row.revoked_at = now
    user.tokens_valid_after = now

    AdminAuditLogService.record(
        db,
        actor_type="admin",
        actor_user=actor,
        affected_user=user,
        tenant_id=user.tenant_id,
        action="sessions_revoked",
        detail={"sesiones_revocadas": len(active_sessions), "alcance": "todas"},
        ip_address=_client_ip(request),
    )

    db.commit()
    return UserSecurity(
        active_sessions=[],
        failed_login_attempts=user.failed_login_attempts,
        security_blocked_at=user.security_blocked_at,
    )


@router.delete(
    "/users/{user_id}/sessions/{session_id}",
    response_model=UserSecurity,
    summary="Revocar una sola sesión de un usuario",
    description=(
        "Revoca una sesión puntual (FR-030). 404 si la sesión no pertenece "
        "a ese usuario o ya estaba revocada."
    ),
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "Se requiere acceso de super admin."},
        404: {"description": "No existe ese usuario, o la sesión no le pertenece o ya estaba revocada."},
    },
)
def revoke_single_session(
    user_id: UUID,
    session_id: UUID,
    request: Request,
    db: Session = Depends(get_shared_db),
    actor: User = Depends(get_current_super_admin),
):
    user = _get_user_or_404(db, user_id)

    session_row = db.execute(
        select(UserSession).where(
            UserSession.id == session_id,
            UserSession.user_id == user.id,
            UserSession.revoked_at.is_(None),
        )
    ).scalar_one_or_none()
    if session_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="La sesión no existe, no pertenece a este usuario o ya estaba revocada.",
        )

    session_row.revoked_at = utc_now_naive()

    AdminAuditLogService.record(
        db,
        actor_type="admin",
        actor_user=actor,
        affected_user=user,
        tenant_id=user.tenant_id,
        action="sessions_revoked",
        detail={"sesiones_revocadas": 1, "alcance": "individual", "session_id": str(session_id)},
        ip_address=_client_ip(request),
    )

    db.commit()

    active_sessions = db.execute(
        select(UserSession)
        .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
        .order_by(UserSession.last_seen_at.desc())
    ).scalars().all()
    return UserSecurity(
        active_sessions=[UserSessionItem.model_validate(s) for s in active_sessions],
        failed_login_attempts=user.failed_login_attempts,
        security_blocked_at=user.security_blocked_at,
    )


@router.post(
    "/users/{user_id}/send-password-reset",
    summary="Enviar recuperación de contraseña",
    description=(
        "Reutiliza el mecanismo transaccional de `/auth/forgot-password` "
        "(spec 096) para emitir un `PasswordResetToken` y encolar el correo "
        "vía Celery (FR-021). Registra `password_reset_sent` en la "
        "auditoría administrativa."
    ),
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "Se requiere acceso de super admin."},
        404: {"description": "No existe ese usuario."},
        409: {"description": "El usuario está anonimizado."},
    },
)
def send_password_reset(
    user_id: UUID,
    request: Request,
    db: Session = Depends(get_shared_db),
    actor: User = Depends(get_current_super_admin),
):
    user = _get_user_or_404(db, user_id)

    if user.status == "anonymized":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No se puede enviar recuperación de contraseña a un usuario anonimizado.",
        )

    now = utc_now_naive()

    # Igual que `/auth/forgot-password`: a lo sumo un enlace vigente por
    # cuenta -- uno nuevo invalida cualquier enlace anterior sin usar.
    db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.invalidated_at.is_(None),
        )
        .values(invalidated_at=now)
    )

    raw_token = secrets.token_urlsafe(32)
    expiry_minutes = settings.PASSWORD_RESET_TOKEN_EXPIRY_MINUTES
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            email_snapshot=user.email,
            issued_at=now,
            expires_at=now + timedelta(minutes=expiry_minutes),
        )
    )

    AdminAuditLogService.record(
        db,
        actor_type="admin",
        actor_user=actor,
        affected_user=user,
        tenant_id=user.tenant_id,
        action="password_reset_sent",
        detail=None,
        ip_address=_client_ip(request),
    )

    db.commit()

    try:
        correo = construir_correo_restablecimiento(
            url_restablecimiento=_build_reset_url_for_user(user, raw_token),
            minutos_de_vigencia=expiry_minutes,
        )
        send_email_task.delay(
            recipients=[user.email],
            subject=correo.subject,
            body=correo.html_body,
            text_body=correo.text_body,
        )
    except Exception:
        logger.warning(
            "No se pudo encolar el correo de restablecimiento (Super Admin) para '%s'",
            user.email,
            exc_info=True,
        )

    return {"message": f"Se envió la recuperación de contraseña a {user.email}."}
