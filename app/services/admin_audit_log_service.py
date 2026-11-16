from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.models import User
from app.models.admin_audit_log import AdminAuditLog


class AdminAuditLogService:
    """Registra toda acción administrativa (y todo evento automático relevante)
    en `shared.admin_audit_logs` (spec 159, research.md Decisión 6). Punto
    único de escritura -- cada endpoint de administración de usuarios llama
    a `record(...)` en vez de construir `AdminAuditLog` directamente."""

    @staticmethod
    def record(
        db: Session,
        *,
        actor_type: str,
        actor_user: Optional[User],
        affected_user: Optional[User],
        tenant_id: Optional[int],
        action: str,
        detail: Optional[dict[str, Any]] = None,
        ip_address: Optional[str] = None,
    ) -> AdminAuditLog:
        entry = AdminAuditLog(
            actor_type=actor_type,
            actor_user_id=actor_user.id if actor_user else None,
            actor_name_snapshot=actor_user.name if actor_user else None,
            affected_user_id=affected_user.id if affected_user else None,
            affected_user_email_snapshot=affected_user.email if affected_user else None,
            tenant_id=tenant_id,
            action=action,
            detail=detail,
            ip_address=ip_address,
        )
        db.add(entry)
        db.flush()
        return entry
