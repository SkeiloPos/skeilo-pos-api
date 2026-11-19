"""API de notificaciones del staff (spec 077). Cualquier usuario autenticado
del tenant puede leer y marcar como atendida — a diferencia de `/audit-logs`,
no exige `require_tenant_admin` (contracts/notifications-api.md).

Ningún endpoint acepta un identificador de tenant como parámetro: el tenant
sale siempre de `Depends(get_tenant)`, y las consultas ORM corren bajo el
`schema_translate_map` de ese tenant (NFR-003)."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.api.v1.notifications.schemas import (
    AttendResponse,
    NotificationListResponse,
    PublicKeyResponse,
    PushSubscriptionDeleteIn,
    PushSubscriptionIn,
)
from app.core.config import settings
from app.core.db import get_db
from app.core.dependencies import get_current_user
from app.core.models import User
from app.models.notification_event import NotificationEvent
from app.models.push_subscription import PushSubscription
from app.core.timezone import utc_now_naive

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _visible_to(role: str | None):
    """Condición SQL de las notificaciones que un rol puede ver (spec 161, A-164): las dirigidas a todo el personal
    (`audience_role` NULL, todas las anteriores a la spec 161) y las dirigidas a su propio rol. Un usuario sin rol
    conocido solo ve las primeras."""
    if not role:
        return NotificationEvent.audience_role.is_(None)
    return or_(NotificationEvent.audience_role.is_(None), NotificationEvent.audience_role == role)


@router.get("", response_model=NotificationListResponse, summary="Listar notificaciones del tenant")
def list_notifications(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    after_id: UUID | None = Query(None, description="Modo recuperación: posteriores a esta notificación"),
    limit: int = Query(100, ge=1, le=500),
    only_pending: bool = Query(False),
    db: Session = Depends(get_db),
    # El parámetro se sigue llamando `_`: `bootstrap/service.py` (spec 100) llama a esta función por nombre
    # (`_=user`). Renombrarlo rompería ese bloque en silencio, porque el bootstrap degrada a `null` en vez de fallar.
    _: User = Depends(get_current_user),
):
    # spec 161 (A-164): solo se ven las notificaciones dirigidas a todo el personal o al rol del usuario.
    # `getattr`: un usuario sin `role_name` (los dobles de prueba de la spec 077) cuenta como «sin rol».
    stmt = select(NotificationEvent).where(_visible_to(getattr(_, "role_name", None)))
    if only_pending:
        stmt = stmt.where(NotificationEvent.attended_at.is_(None))

    if after_id is not None:
        # Modo recuperación (RF-008): posteriores a `after_id`, ascendente,
        # sin paginar por página — `page`/`size`/`total` se omiten.
        referencia = db.get(NotificationEvent, after_id)
        stmt = stmt.order_by(NotificationEvent.created_at.asc())
        if referencia is not None:
            stmt = stmt.where(NotificationEvent.created_at > referencia.created_at)
        items = db.execute(stmt.limit(limit)).scalars().all()
        return NotificationListResponse(items=items)

    stmt = stmt.order_by(NotificationEvent.created_at.desc())
    total = db.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar_one()
    items = db.execute(stmt.offset((page - 1) * size).limit(size)).scalars().all()
    return NotificationListResponse(items=items, total=total, page=page, size=size)


@router.get(
    "/push/public-key",
    response_model=PublicKeyResponse,
    summary="Clave pública VAPID (misma para todos los tenants)",
)
def get_push_public_key(_: User = Depends(get_current_user)):
    if not settings.VAPID_PUBLIC_KEY:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Push no configurado")
    return PublicKeyResponse(public_key=settings.VAPID_PUBLIC_KEY)


@router.post(
    "/push/subscriptions",
    summary="Registrar o refrescar una suscripción push del usuario autenticado",
)
def create_push_subscription(
    body: PushSubscriptionIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    existente = db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == body.endpoint)
    ).scalar_one_or_none()

    now = utc_now_naive()
    if existente is not None:
        # UPSERT por endpoint (único): un endpoint de push es global al
        # dispositivo/navegador, así que se reasigna al usuario/tenant
        # actual (contracts/notifications-api.md).
        existente.user_id = user.id
        existente.p256dh_key = body.keys.p256dh
        existente.auth_key = body.keys.auth
        existente.user_agent = body.user_agent
        existente.active = True
        existente.last_seen_at = now
        db.commit()
        return Response(status_code=status.HTTP_200_OK)

    nueva = PushSubscription(
        user_id=user.id,
        endpoint=body.endpoint,
        p256dh_key=body.keys.p256dh,
        auth_key=body.keys.auth,
        user_agent=body.user_agent,
        last_seen_at=now,
        active=True,
    )
    db.add(nueva)
    db.commit()
    return Response(status_code=status.HTTP_201_CREATED)


@router.delete(
    "/push/subscriptions",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Dar de baja una suscripción push (idempotente)",
)
def delete_push_subscription(
    body: PushSubscriptionDeleteIn,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    db.execute(
        update(PushSubscription)
        .where(PushSubscription.endpoint == body.endpoint)
        .values(active=False)
    )
    db.commit()


@router.post(
    "/{notification_id}/attend",
    response_model=AttendResponse,
    summary="Marcar una notificación como atendida (compartido por tenant)",
)
def attend_notification(
    notification_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    notification = db.get(NotificationEvent, notification_id)
    # Una notificación dirigida a otro rol responde 404 y no 403: no se revela que existe (spec 161, A-164).
    role = getattr(user, "role_name", None)
    if notification is None or (notification.audience_role and notification.audience_role != role):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notificación no encontrada")

    # Idempotente: si ya la atendió otro cajero, no cambia nada y responde 200
    # igual (FR-006 clarificado) — "compartido" significa una sola marca por
    # tenant, no una por usuario.
    if notification.attended_at is None:
        notification.attended_at = utc_now_naive()
        notification.attended_by_user_id = user.id
        db.commit()

    return notification
