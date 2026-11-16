import hashlib
import logging
import secrets
import uuid
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, status,Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.orm import joinedload
from sqlalchemy.exc import SQLAlchemyError, OperationalError
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.db import with_db, get_tenant
from app.core.models import User, Tenant, PasswordResetToken, UserInvitation
from app.core.reserved_hosts import PLATFORM_HOST
from app.core.utils import verify_password, create_access_token, generate_passwd_hash
from app.core.dependencies import RefreshTokenBearer,AccessTokenBearer, get_authenticated_user
from app.core.dependencies import get_shared_db, _reject_if_session_revoked
from app.core.rate_limit import enforce_sliding_window
from app.core.mail_templates import construir_correo_contrasena_cambiada, construir_correo_restablecimiento
from app.core.timezone import utc_now_naive
from app.celery_task import send_email_task
from app.api.v1.auth.schemas import (
    LoginRequest,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    ResetPasswordRequest,
)
from app.core.redis import add_jti_to_blocklist
from app.models.user_session import UserSession
from app.services.admin_audit_log_service import AdminAuditLogService
auth_router = APIRouter(prefix="/auth", tags=["auth"])

logger = logging.getLogger(__name__)

# Mensaje genérico de FR-003: idéntico exista o no la cuenta detrás del correo
# (SC-003) — nunca revela si hay una cuenta registrada con ese email.
_FORGOT_PASSWORD_GENERIC_MESSAGE = {
    "message": (
        "Si existe una cuenta con ese correo, te enviamos un enlace para "
        "restablecer tu contraseña. Revisa tu bandeja de entrada y la carpeta de spam."
    )
}


def _build_login_url_for_user(user: User) -> str:
    """FR-037: subdominio del negocio para un usuario de tenant, o
    `PLATFORM_HOST` (`admin`) para un Super Admin. Mismo patrón
    `prod → https`/`resto → localhost:4200` que los módulos vecinos — no se
    unifica con `_build_login_url` de `invitations/router.py` ni de
    `super_admin/router.py` (research Decisión 9, Principio V)."""
    host = PLATFORM_HOST if user.tenant_id is None else user.tenant.host
    if settings.ENVIRONMENT == "prod":
        return f"https://{host}.skeilopos.com/login"
    return f"http://{host}.localhost:4200/login"


def _dispatch_password_changed_email(user: User, when) -> None:
    """Correo de aviso tras cualquier cambio exitoso (FR-022). Nunca bloquea ni
    rompe la respuesta si el envío falla (FR-028). El constructor se evalúa
    dentro de este mismo `try`, que ya captura `Exception`: un fallo de
    FR-041 queda en `warning` sin romper la respuesta (research Decisión 10)."""
    try:
        correo = construir_correo_contrasena_cambiada(
            correo=user.email,
            momento=when,
            url_ingreso=_build_login_url_for_user(user),
            es_super_admin=user.tenant_id is None,
            nombre_negocio=user.tenant_name,
            # spec 138 (FR-011): la hora del aviso en la zona del negocio, no siempre en Bogotá.
            zona_horaria=user.tenant.timezone if user.tenant_id is not None else None,
        )
        send_email_task.delay(
            recipients=[user.email],
            subject=correo.subject,
            body=correo.html_body,
            text_body=correo.text_body,
        )
    except Exception:
        logger.warning(
            "No se pudo encolar el correo de aviso de cambio de contraseña para '%s'",
            user.email,
            exc_info=True,
        )


def _client_ip(req: Request) -> str | None:
    """IP de origen del login, para `UserSession.ip_address` (spec 159,
    research.md Decisión 5) y para la bitácora del bloqueo automático. `req`
    puede ser un doble de test sin atributo `client` (p. ej.
    `test_auth_session_name_claim.py`), de ahí el `getattr` defensivo."""
    client = getattr(req, "client", None)
    return client.host if client else None


def _parse_device_info(user_agent: str | None) -> tuple[str | None, str | None]:
    """Heurística mínima sobre el `User-Agent` (sin dependencia nueva, spec
    159/Principio IX): solo lo suficiente para mostrar algo útil como
    "Chrome en Windows" en la pestaña de Seguridad -- no pretende ser un
    parser completo de user agents."""
    if not user_agent:
        return None, None

    ua = user_agent.lower()
    if "edg/" in ua:
        browser = "Edge"
    elif "chrome/" in ua and "chromium" not in ua:
        browser = "Chrome"
    elif "firefox/" in ua:
        browser = "Firefox"
    elif "safari/" in ua and "chrome/" not in ua:
        browser = "Safari"
    else:
        browser = None

    if "windows" in ua:
        os_label = "Windows"
    elif "mac os" in ua or "macintosh" in ua:
        os_label = "macOS"
    elif "android" in ua:
        os_label = "Android"
    elif "iphone" in ua or "ipad" in ua:
        os_label = "iOS"
    elif "linux" in ua:
        os_label = "Linux"
    else:
        os_label = None

    if browser and os_label:
        device_label = f"{browser} en {os_label}"
    else:
        device_label = browser or os_label

    return device_label, browser


def _login_lockout_scope(tenant_id) -> str:
    """Clave de ámbito del contador de intentos fallidos: `"platform"` para
    Super Admin (`tenant_id IS NULL`), o el id del tenant -- mismo criterio
    de ámbito que ya usa `/auth/forgot-password` por tenant (research.md
    Decisión 4)."""
    return "platform" if tenant_id is None else str(tenant_id)


def _login_lockout_key(tenant_id, email_normalized: str) -> str:
    return f"rl:login:{_login_lockout_scope(tenant_id)}:{email_normalized}"


def _build_reset_url(tenant: Tenant, raw_token: str) -> str:
    if settings.ENVIRONMENT == "prod":
        return f"https://{tenant.host}.skeilopos.com/reset-password?token={raw_token}"
    return f"http://{tenant.host}.localhost:4200/reset-password?token={raw_token}"


def _resolve_reset_token(db: Session, raw_token: str, *, lock: bool = False):
    """Devuelve (token_row, user, reason). `reason` es `None` si vigente, o
    `"expired"`/`"used"`/`"invalid"` según el estado derivado de data-model.md.
    Con `lock=True` bloquea la fila (`WITH FOR UPDATE`, research.md Decisión 5)
    para que un doble consumo concurrente del mismo enlace no aplique un
    segundo cambio (FR-008)."""
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    stmt = select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
    if lock:
        stmt = stmt.with_for_update(of=PasswordResetToken)
    row = db.execute(stmt).scalar_one_or_none()

    if row is None:
        return None, None, "invalid"

    user = db.get(User, row.user_id)

    if row.used_at is not None:
        return row, user, "used"
    if row.invalidated_at is not None:
        return row, user, "invalid"
    if user is None or user.email != row.email_snapshot:
        return row, user, "invalid"
    if utc_now_naive() >= row.expires_at:
        return row, user, "expired"

    return row, user, None


def _consume_invitation_if_valid(db: Session, tenant: Tenant, email: str, password: str):
    """Consume una `UserInvitation` 'pending' cuyas credenciales coincidan
    (FR-007, research.md Decisión 7). Devuelve el `User` recién creado, o
    `None` si no hay ninguna invitación vigente o la contraseña no coincide
    — en ambos casos el llamador cae al mismo `401` de siempre, sin crear
    nada. `WITH FOR UPDATE` evita que dos logins casi simultáneos con la
    misma contraseña temporal consuman la misma invitación dos veces."""
    email_normalized = email.strip().lower()
    invitation = db.execute(
        select(UserInvitation)
        .where(
            UserInvitation.tenant_id == tenant.id,
            UserInvitation.email == email_normalized,
            UserInvitation.status == "pending",
        )
        .with_for_update(of=UserInvitation)
    ).scalar_one_or_none()

    if invitation is None or not verify_password(password, invitation.password_hash):
        return None

    new_user = User(
        name=invitation.name or invitation.email,
        email=invitation.email,
        password_hash=invitation.password_hash,
        phone=None,
        active=True,
        must_change_password=True,
        role_id=invitation.role_id,
        tenant_id=invitation.tenant_id,
    )
    db.add(new_user)
    invitation.status = "consumed"
    invitation.consumed_at = utc_now_naive()
    db.commit()
    db.refresh(new_user)
    return new_user


@auth_router.post("/login")
async def login(body: LoginRequest, req: Request):
    host_header = req.headers.get("x-tenant-host")
    host = host_header.split(":", 1)[0] if host_header else None
    logger.info(f"Intentando login para email: {body.email} (host: {host})")

    # spec 091, A-99: el ámbito de la cuenta lo decide el host explícito, nunca
    # su ausencia. `admin` abre la plataforma (Super Admin, `tenant_id IS NULL`);
    # el slug de un negocio registrado abre solo ese negocio; un host ausente,
    # vacío, desconocido o el dominio raíz no abre ningún ámbito (mismo 401 que
    # unas credenciales inválidas, sin revelar el motivo). Un solo ámbito por petición.
    is_platform = bool(host) and host.strip().lower() == PLATFORM_HOST

    try:
        with with_db(None) as db:
            tenant = None
            if is_platform:
                reason = "platform"
            elif not host:
                reason = "no_host"
            else:
                # Búsqueda del negocio por igualdad exacta con `Tenant.host` (FR-010).
                tenant = db.query(Tenant).filter(Tenant.host == host).one_or_none()
                reason = "tenant" if tenant is not None else "unknown_host"
            logger.info(f"Login: ámbito resuelto reason={reason} tenant={tenant.name if tenant else None}")

            if reason in ("no_host", "unknown_host"):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
                )

            stmt = select(User).options(joinedload(User.role)).where(User.email == body.email)
            if tenant is not None:
                stmt = stmt.where(User.tenant_id == tenant.id)   # usuario de tenant
            else:
                stmt = stmt.where(User.tenant_id.is_(None))      # plataforma (Super Admin)

            user = db.execute(stmt).scalar_one_or_none()

            # FR-007: si no hay `User` con ese correo pero sí una invitación
            # pendiente cuyas credenciales coinciden, esto crea la cuenta y
            # consume la invitación en el mismo intento — el resto del login
            # sigue exactamente igual que con cualquier usuario existente
            # (research.md Decisión 7). En el ámbito de plataforma nunca se
            # busca invitación.
            if user is None and tenant is not None:
                user = _consume_invitation_if_valid(db, tenant, body.email, body.password)

            # Validaciones dentro de la sesión para evitar objetos detached.
            logger.info(f"Usuario encontrado: {user.email if user else 'None'}")
            if not user or not verify_password(body.password, user.password_hash):
                # spec 159, research.md Decisión 4: contador deslizante por
                # (ámbito, correo) -- solo aplica cuando SÍ existe una cuenta
                # con ese correo (no hay nada que bloquear si no existe). El
                # 401 de esta respuesta no cambia en ningún caso; cruzar el
                # umbral solo cambia estado interno para el intento siguiente.
                if user is not None:
                    await _register_failed_login_attempt(db, user, req)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
                )

            if not user.active:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail="User account is inactive"
                )

            # Login exitoso: reinicia el contador de intentos fallidos
            # (research.md Decisión 4) y registra una sesión activa nueva
            # (Decisión 5) -- el `sid` resultante se firma en ambos tokens.
            await _reset_failed_login_counter(user, _login_lockout_key(
                user.tenant_id, body.email.strip().lower()
            ))

            session_row = UserSession(user_id=user.id)
            device_label, browser = _parse_device_info(req.headers.get("user-agent"))
            session_row.device_label = device_label
            session_row.browser = browser
            session_row.ip_address = _client_ip(req)
            db.add(session_row)
            db.commit()
            db.refresh(session_row)
            session_id = str(session_row.id)

            user_data = {
                "email": user.email,
                "name": user.name,
                "uid": str(user.id),
                "tenant_id": user.tenant_id,
                "is_super_admin": user.tenant_id is None,
                "role": user.role.name if user.role else None,
                "must_change_password": user.must_change_password,
            }
    except OperationalError as e:
        logger.error(f"Error de conexión a la base de datos: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database connection error"
        )
    except SQLAlchemyError as e:
        logger.error(f"Error en la consulta: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database query error"
        )

    access_token = create_access_token(user_data, sid=session_id)
    refresh_token = create_access_token(user_data, refresh=True, sid=session_id)

    return JSONResponse(
        content={
            "message": "Login successful",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "user": user_data,
        }
    )


async def _register_failed_login_attempt(db: Session, user: User, req: Request) -> None:
    """spec 159, research.md Decisión 4: incrementa el snapshot persistido y,
    al cruzar el umbral configurado (ventana deslizante en Redis, mismo
    mecanismo que `/auth/forgot-password`), bloquea la cuenta por seguridad.
    `enforce_sliding_window` es fail-open: si Redis no responde, nunca se
    bloquea una cuenta por un intento que no se pudo contar."""
    if user.status == "anonymized":
        return

    user.failed_login_attempts = (user.failed_login_attempts or 0) + 1

    key = _login_lockout_key(user.tenant_id, user.email.strip().lower())
    already_over_threshold = await enforce_sliding_window(
        key, settings.LOGIN_LOCKOUT_MAX_ATTEMPTS, settings.LOGIN_LOCKOUT_WINDOW_SECONDS
    )

    if already_over_threshold and user.status != "blocked_security":
        user.status = "blocked_security"
        user.active = False
        user.security_blocked_at = utc_now_naive()
        AdminAuditLogService.record(
            db,
            actor_type="system",
            actor_user=None,
            affected_user=user,
            tenant_id=user.tenant_id,
            action="blocked_security",
            detail={"intentos_fallidos": user.failed_login_attempts},
            ip_address=_client_ip(req),
        )

    db.commit()


async def _reset_failed_login_counter(user: User, redis_key: str) -> None:
    """Reinicia el conteo tras un login exitoso o un cambio de contraseña
    (research.md Decisión 4). El campo persistido se limpia siempre (no
    depende de Redis); borrar la clave de Redis es una limpieza best-effort
    (fail-open, igual criterio que `enforce_sliding_window`)."""
    if user.failed_login_attempts:
        user.failed_login_attempts = 0
    try:
        from app.core.redis import token_blocklist as _redis

        await _redis.delete(redis_key)
    except Exception:
        logger.warning("No se pudo limpiar el contador de intentos fallidos en Redis", exc_info=True)


@auth_router.post("/forgot-password")
async def forgot_password(
    body: ForgotPasswordRequest,
    tenant: Tenant = Depends(get_tenant),
    db: Session = Depends(get_shared_db),
):
    email_normalized = body.email.strip().lower()
    blocked = await enforce_sliding_window(
        f"rl:pwreset:{tenant.id}:{email_normalized}",
        settings.PASSWORD_RESET_MAX_REQUESTS,
        settings.PASSWORD_RESET_WINDOW_SECONDS,
    )
    if blocked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Has pedido demasiados enlaces. Vuelve a intentarlo en unos minutos.",
        )

    user = db.execute(
        select(User).where(
            User.email == body.email,
            User.tenant_id == tenant.id,
            User.active == True,
        )
    ).scalar_one_or_none()

    # Cuenta inexistente/inactiva/de otro tenant: mismo trato exacto que el
    # caso feliz, sin crear fila ni enviar correo (FR-004, SC-003).
    if not user:
        return JSONResponse(content=_FORGOT_PASSWORD_GENERIC_MESSAGE)

    now = utc_now_naive()

    # Un enlace nuevo invalida de inmediato cualquier enlace vigente anterior
    # de la misma cuenta (FR-005) — a lo sumo un `vigente` por cuenta.
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
    db.commit()

    try:
        # FR-030: la vigencia que el correo anuncia sale de la configuración
        # vigente, nunca de un literal. El constructor se evalúa dentro de
        # este `try`, que ya captura `Exception`: un fallo de FR-041 queda
        # en `warning` sin romper la respuesta genérica (research Decisión 10).
        correo = construir_correo_restablecimiento(
            url_restablecimiento=_build_reset_url(tenant, raw_token),
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
            "No se pudo encolar el correo de restablecimiento para '%s'", user.email, exc_info=True
        )

    return JSONResponse(content=_FORGOT_PASSWORD_GENERIC_MESSAGE)


@auth_router.get("/reset-password/validate")
def validate_reset_token(token: str, db: Session = Depends(get_shared_db)):
    """Sin efecto secundario — no consume el token (FR-007). Permite que la
    pantalla decida qué mostrar antes de pedir la contraseña nueva."""
    row, _, reason = _resolve_reset_token(db, token)
    if reason is None:
        return JSONResponse(content={"valid": True})

    status_code = status.HTTP_404_NOT_FOUND if row is None else status.HTTP_400_BAD_REQUEST
    return JSONResponse(
        status_code=status_code,
        content={"valid": False, "reason": reason},
    )


@auth_router.post("/reset-password")
def reset_password(body: ResetPasswordRequest, db: Session = Depends(get_shared_db)):
    row, user, reason = _resolve_reset_token(db, body.token, lock=True)

    if reason is not None:
        # Nunca confiar en una validación previa del cliente — se re-valida con
        # las mismas reglas de `.../validate` antes de aplicar ningún cambio.
        status_code = status.HTTP_404_NOT_FOUND if row is None else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=status_code, detail={"valid": False, "reason": reason})

    if verify_password(body.new_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La nueva contraseña debe ser distinta de la actual",
        )

    now = utc_now_naive()
    user.password_hash = generate_passwd_hash(body.new_password)
    user.must_change_password = False  # mismo criterio que RN-AUTH-02
    user.tokens_valid_after = now  # cierra TODAS las sesiones de la cuenta (FR-009)
    user.failed_login_attempts = 0  # research.md Decisión 4: un cambio de contraseña reinicia el conteo
    row.used_at = now  # consumido — un segundo POST con el mismo enlace ve reason="used"
    db.commit()

    _dispatch_password_changed_email(user, now)

    return JSONResponse(content={"message": "Contraseña actualizada correctamente."})


@auth_router.post("/change-password")
def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_authenticated_user),
    db: Session = Depends(get_shared_db),
):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )

    if verify_password(body.new_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La nueva contraseña debe ser distinta de la actual",
        )

    now = utc_now_naive()
    user.password_hash = generate_passwd_hash(body.new_password)
    user.must_change_password = False
    # Cierra todas las sesiones de la cuenta excepto la de origen (FR-017): el
    # frontend vuelve a loguearse con la contraseña nueva justo después de este
    # 200, obteniendo tokens acuñados después de este corte (research.md
    # Decisión 1) — el backend no distingue jtis, solo corta por tiempo.
    user.tokens_valid_after = now
    user.failed_login_attempts = 0  # research.md Decisión 4: un cambio de contraseña reinicia el conteo
    db.commit()

    _dispatch_password_changed_email(user, now)

    return JSONResponse(content={"message": "Password changed successfully"})


@auth_router.get("/refresh-token")
async def get_new_access_token(
    token_details: dict = Depends(RefreshTokenBearer()),
    db: Session = Depends(get_shared_db),
):
    # La expiración ya la validó PyJWT dentro de `decode_token`; si el token
    # estuviera vencido, RefreshTokenBearer habría respondido 401 antes de llegar aquí.
    uid = (token_details.get("user") or {}).get("uid")
    try:
        user_id = uuid.UUID(str(uid))
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload"
        )

    # Se relee el usuario en vez de reciclar los claims del refresh: si no,
    # una cuenta desactivada o con el rol cambiado seguiría emitiendo access
    # tokens válidos con datos obsoletos durante toda la vida del refresh.
    user = db.execute(
        select(User)
        .options(joinedload(User.role))
        .where(User.id == user_id, User.active == True)
    ).scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    _reject_if_session_revoked(user, token_details)

    # spec 159, research.md Decisión 5: un refresh token acuñado antes de
    # este cambio no lleva `sid` -- se trata como vigente (compatibilidad
    # retroactiva, data-model.md § UserSession). Mismo contrato/código ya
    # usado para una sesión cerrada por `tokens_valid_after`.
    sid = token_details.get("sid")
    session_row = None
    if sid is not None:
        try:
            session_row = db.get(UserSession, uuid.UUID(str(sid)))
        except (ValueError, TypeError):
            session_row = None
        if session_row is not None and session_row.revoked_at is not None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has been revoked",
            )

    if session_row is not None:
        session_row.last_seen_at = utc_now_naive()
        db.commit()

    user_data = {
        "email": user.email,
        "name": user.name,
        "uid": str(user.id),
        "tenant_id": user.tenant_id,
        "is_super_admin": user.tenant_id is None,
        "role": user.role.name if user.role else None,
        "must_change_password": user.must_change_password,
    }

    return JSONResponse(content={"access_token": create_access_token(user_data, sid=sid)})

@auth_router.get("/logout")
async def revoke_token(
    token_details: dict = Depends(AccessTokenBearer()),
    db: Session = Depends(get_shared_db),
):
    jti = token_details["jti"]

    await add_jti_to_blocklist(jti, token_details.get("exp"))

    # spec 159, research.md Decisión 5: además del blocklist de `jti` (sin
    # cambios), marca revocada la `UserSession` de este login si el token
    # lleva `sid` -- tokens acuñados antes de este cambio no lo llevan y
    # siguen cerrando sesión solo vía blocklist, como antes.
    sid = token_details.get("sid")
    if sid is not None:
        try:
            session_row = db.get(UserSession, uuid.UUID(str(sid)))
        except (ValueError, TypeError):
            session_row = None
        if session_row is not None and session_row.revoked_at is None:
            session_row.revoked_at = utc_now_naive()
            db.commit()

    return JSONResponse(
        content={"message": "Logged Out Successfully"}, status_code=status.HTTP_200_OK
    )
