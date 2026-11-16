from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.models import User


def ensure_action_keeps_a_super_admin_active(db: Session, target: User) -> None:
    """Edge case / FR-026 / research.md Decisión 8: ninguna acción
    administrativa (suspender, bloquear, anonimizar, eliminar) puede dejar a
    la plataforma sin ningún Super Admin activo. Un Super Admin es un `User`
    con `tenant_id IS NULL`; `target` solo importa cuando ya es uno. Cuenta
    cuántos OTROS Super Admin activos existen aparte de `target` -- si la
    acción reduciría ese conjunto a 0, la bloquea con 409."""
    if target.tenant_id is not None:
        return

    other_active_super_admins = db.execute(
        select(func.count())
        .select_from(User)
        .where(
            User.tenant_id.is_(None),
            User.status == "active",
            User.id != target.id,
        )
    ).scalar_one()

    if other_active_super_admins == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "No se puede continuar: esta acción dejaría a la plataforma "
                "sin ningún Super Admin activo."
            ),
        )
