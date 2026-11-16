"""Auditoría administrativa de alcance plataforma (spec 159, User Story 7,
FR-033 a FR-038). Registrado bajo el mismo prefijo `/super-admin` que
`router.py`, reutilizando `get_current_super_admin` (sin duplicar la
dependencia aquí). Solo lectura: no escribe `AdminAuditLog`, eso ya lo hacen
los endpoints de administración (`users_router.py`) y `/auth/login`."""

from datetime import date
from math import ceil
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.dependencies import get_shared_db
from app.core.models import Tenant
from app.models.admin_audit_log import AdminAuditLog
from app.api.v1.super_admin.schemas import AdminAuditLogItem, AuditLogPage

router = APIRouter()


@router.get(
    "/audit-logs",
    response_model=AuditLogPage,
    summary="Auditoría administrativa",
    description=(
        "Listado paginado y filtrable de `shared.admin_audit_logs` "
        "(FR-033 a FR-038): toda acción administrativa (cambio de rol, "
        "suspensión, reactivación, revocación de sesiones, recuperación de "
        "contraseña, anonimización, eliminación) y todo evento automático "
        "relevante (bloqueo por intentos fallidos). Cada fila expone "
        "`actor_type` para distinguir Sistema de Administrador (FR-035). "
        "Los filtros (`affected_user_id`, `actor_user_id`, `tenant_id`, "
        "`action`, `date_from`, `date_to`) son combinables entre sí "
        "(FR-036)."
    ),
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "Se requiere acceso de super admin."},
    },
)
def list_audit_logs(
    page: int = Query(1, ge=1, description="Número de página (empieza en 1)."),
    size: int = Query(20, ge=1, le=100, description="Cantidad de elementos por página (máximo 100)."),
    affected_user_id: UUID | None = Query(None, description="Filtra por usuario afectado."),
    actor_user_id: UUID | None = Query(None, description="Filtra por administrador que ejecutó la acción."),
    tenant_id: int | None = Query(None, description="Filtra por negocio relacionado."),
    action: str | None = Query(None, description="Filtra por tipo de acción (p. ej. `suspended`)."),
    date_from: date | None = Query(None, description="Fecha mínima (inclusive)."),
    date_to: date | None = Query(None, description="Fecha máxima (inclusive)."),
    db: Session = Depends(get_shared_db),
):
    stmt = (
        select(
            AdminAuditLog.id,
            AdminAuditLog.actor_type,
            AdminAuditLog.actor_user_id,
            AdminAuditLog.actor_name_snapshot,
            AdminAuditLog.affected_user_id,
            AdminAuditLog.affected_user_email_snapshot,
            AdminAuditLog.tenant_id,
            Tenant.name.label("tenant_name"),
            AdminAuditLog.action,
            AdminAuditLog.detail,
            AdminAuditLog.ip_address,
            AdminAuditLog.created_at,
        )
        .join(Tenant, Tenant.id == AdminAuditLog.tenant_id, isouter=True)
    )

    if affected_user_id is not None:
        stmt = stmt.where(AdminAuditLog.affected_user_id == affected_user_id)
    if actor_user_id is not None:
        stmt = stmt.where(AdminAuditLog.actor_user_id == actor_user_id)
    if tenant_id is not None:
        stmt = stmt.where(AdminAuditLog.tenant_id == tenant_id)
    if action:
        stmt = stmt.where(AdminAuditLog.action == action)
    if date_from is not None:
        stmt = stmt.where(func.date(AdminAuditLog.created_at) >= date_from)
    if date_to is not None:
        stmt = stmt.where(func.date(AdminAuditLog.created_at) <= date_to)

    stmt = stmt.order_by(AdminAuditLog.created_at.desc())

    total = db.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar_one()

    rows = db.execute(stmt.offset((page - 1) * size).limit(size)).all()
    items = [AdminAuditLogItem.model_validate(dict(row._mapping)) for row in rows]

    has_active_filters = any(
        [affected_user_id, actor_user_id, tenant_id, action, date_from, date_to]
    )
    empty_reason = None if total else ("no_results" if has_active_filters else "no_data")

    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "pages": ceil(total / size) if total else 0,
        "empty_reason": empty_reason,
    }
