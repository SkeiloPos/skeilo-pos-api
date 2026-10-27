import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.db import get_db, get_tenant
from app.core.dependencies import require_tenant_admin
from app.core.mail import create_message, send_email
from app.core.mail_templates import ErrorDePlantillaDeCorreo, construir_correo_invitacion
from app.core.models import Role, Tenant, User, UserInvitation
from app.core.pagination import Page, paginate
from app.core.plan_limits import enforce_plan_limit
from app.core.timezone import utc_now_naive
from app.core.utils import generate_passwd_hash, generate_random_password
from app.api.v1.invitations.schemas import InvitationCreate, InvitationResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/invitations", tags=["invitations"])


def _build_login_url(tenant: Tenant) -> str:
    if settings.ENVIRONMENT == "prod":
        return f"https://{tenant.host}.skeilopos.com/login"
    return f"http://{tenant.host}.localhost:4200/login"


@router.get(
    "",
    response_model=Page[InvitationResponse],
    summary="Listar invitaciones pendientes del tenant",
    description=(
        "Devuelve, de forma paginada, las invitaciones con status='pending' del tenant del "
        "admin autenticado (FR-009/FR-013) — nunca de otro tenant, ni las ya consumidas/canceladas."
    ),
    response_description="Página de invitaciones pendientes del tenant.",
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "El usuario no es administrador del tenant."},
    },
)
def list_pending_invitations(
    page: int = Query(1, ge=1, description="Número de página (empieza en 1)."),
    size: int = Query(20, ge=1, le=100, description="Cantidad de elementos por página (máximo 100)."),
    db: Session = Depends(get_db),
    admin: User = Depends(require_tenant_admin),
):
    stmt = (
        select(UserInvitation)
        .options(selectinload(UserInvitation.role))
        .where(UserInvitation.tenant_id == admin.tenant_id, UserInvitation.status == "pending")
        .order_by(UserInvitation.sent_at.desc())
    )
    return paginate(db, stmt, page, size)


@router.post(
    "",
    response_model=InvitationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Invitar a una persona a unirse al tenant",
    description=(
        "Crea una invitación pendiente para el correo y rol indicados — único mecanismo "
        "para dar de alta un usuario interno del tenant (FR-001/FR-003/FR-004). No crea "
        "ningún `User`; la contraseña temporal generada nunca se expone (FR-006)."
    ),
    response_description="La invitación creada.",
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "No es ADMIN del tenant, o el plan venció / alcanzó su límite de usuarios."},
        404: {"description": "El rol indicado no existe."},
        409: {"description": "Ya existe un usuario o una invitación pendiente con ese correo en el tenant."},
        422: {"description": "Datos de entrada inválidos."},
        502: {"description": "No se pudo enviar el correo de invitación."},
    },
)
def create_invitation(
    body: InvitationCreate,
    tenant: Tenant = Depends(get_tenant),
    admin: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    email = body.email.strip().lower()

    # 1) Plan vencido (FR-018) o límite de "usuarios" alcanzado, contando
    # también invitaciones pendientes (research.md Decisiones 5/6).
    enforce_plan_limit(db, tenant, "usuarios")

    # 2) Correo ya usado por un User del tenant, activo o inactivo (FR-015,
    # Clarification 1) — no se libera el correo de una cuenta desactivada.
    existing_user = db.execute(
        select(User).where(User.tenant_id == tenant.id, User.email == email)
    ).scalar_one_or_none()
    if existing_user is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existe un usuario con ese correo en el tenant",
        )

    role = db.execute(
        select(Role).where(Role.name == body.role.value)
    ).scalar_one_or_none()
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role '{body.role.value}' not found",
        )

    password = generate_random_password()
    invitation = UserInvitation(
        tenant_id=tenant.id,
        email=email,
        name=body.name,
        role_id=role.id,
        password_hash=generate_passwd_hash(password),
    )
    db.add(invitation)

    # 3) A lo sumo una invitación 'pending' por correo+tenant (índice único
    # parcial, research.md Decisión 3) — dos ADMIN invitando casi al mismo
    # tiempo dejan una sola fila viva.
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existe una invitación pendiente para ese correo",
        )

    # 4) Envío síncrono (research.md Decisión 4): un fallo deja la invitación
    # sin persistir (FR-012) — nunca se confirma un envío que no salió.
    # FR-041/A-121: un valor obligatorio vacío o nulo hace que el
    # constructor levante ErrorDePlantillaDeCorreo, que entra por el mismo
    # camino que un fallo del servicio de correo (FR-057) — mismo 502,
    # mismo rollback, sin vía de error nueva.
    try:
        correo = construir_correo_invitacion(
            nombre_negocio=tenant.name,
            url_ingreso=_build_login_url(tenant),
            correo=email,
            contrasena_temporal=password,
            nombre_persona=body.name,
        )
        send_email(
            create_message(
                [email], correo.subject, correo.html_body, text_body=correo.text_body
            )
        )
    except (RuntimeError, ErrorDePlantillaDeCorreo) as exc:
        logger.warning(
            "No se pudo generar o enviar el correo de invitación para '%s': %s",
            email,
            exc,
        )
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo enviar el correo de invitación. Intenta de nuevo.",
        )

    db.commit()
    db.refresh(invitation)
    return invitation


def _get_pending_invitation_for_tenant(db: Session, admin: User, invitation_id: UUID) -> UserInvitation:
    invitation = db.execute(
        select(UserInvitation)
        .where(UserInvitation.id == invitation_id, UserInvitation.tenant_id == admin.tenant_id)
        .with_for_update(of=UserInvitation)
    ).scalar_one_or_none()
    if invitation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invitación no encontrada",
        )
    if invitation.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="La invitación ya no está pendiente",
        )
    return invitation


@router.post(
    "/{invitation_id}/resend",
    response_model=InvitationResponse,
    summary="Reenviar una invitación pendiente",
    description=(
        "Genera una nueva contraseña temporal, invalida de inmediato la anterior, actualiza la "
        "fecha de envío y despacha un nuevo correo (FR-010)."
    ),
    response_description="La invitación reenviada.",
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "El usuario no es administrador del tenant."},
        404: {"description": "La invitación no existe en el tenant del admin."},
        409: {"description": "La invitación ya no está pendiente."},
        502: {"description": "No se pudo enviar el correo de invitación."},
    },
)
def resend_invitation(
    invitation_id: UUID,
    tenant: Tenant = Depends(get_tenant),
    admin: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    invitation = _get_pending_invitation_for_tenant(db, admin, invitation_id)

    password = generate_random_password()

    # research.md Decisión 10: el correo se despacha *antes* de sobrescribir
    # la fila — si falla, la contraseña anterior sigue intacta y sirviendo
    # (FR-012 aplicado al reenvío: nunca deja la invitación sin ninguna
    # contraseña funcional conocida por nadie). Mismo correo que la
    # invitación original, misma presentación y asunto (FR-025).
    try:
        correo = construir_correo_invitacion(
            nombre_negocio=tenant.name,
            url_ingreso=_build_login_url(tenant),
            correo=invitation.email,
            contrasena_temporal=password,
            nombre_persona=invitation.name,
        )
        send_email(
            create_message(
                [invitation.email], correo.subject, correo.html_body, text_body=correo.text_body
            )
        )
    except (RuntimeError, ErrorDePlantillaDeCorreo) as exc:
        logger.warning(
            "No se pudo generar o enviar el correo de reenvío de invitación para '%s': %s",
            invitation.email,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo enviar el correo de invitación. Intenta de nuevo.",
        )

    invitation.password_hash = generate_passwd_hash(password)
    invitation.sent_at = utc_now_naive()
    db.commit()
    db.refresh(invitation)
    return invitation


@router.post(
    "/{invitation_id}/cancel",
    response_model=InvitationResponse,
    summary="Cancelar una invitación pendiente",
    description="Invalida de inmediato su contraseña temporal y deja de aparecer como pendiente (FR-011).",
    response_description="La invitación cancelada.",
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "El usuario no es administrador del tenant."},
        404: {"description": "La invitación no existe en el tenant del admin."},
        409: {"description": "La invitación ya no está pendiente."},
    },
)
def cancel_invitation(
    invitation_id: UUID,
    admin: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    invitation = _get_pending_invitation_for_tenant(db, admin, invitation_id)

    invitation.status = "cancelled"
    invitation.cancelled_at = utc_now_naive()
    db.commit()
    db.refresh(invitation)
    return invitation
