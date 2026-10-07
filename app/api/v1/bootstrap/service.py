import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.api.v1.notifications import router as notifications_router
from app.api.v1.plan import service as plan_service
from app.core.models import Tenant, User

logger = logging.getLogger(__name__)


def build_bootstrap(db: Session, tenant: Optional[Tenant], user: User) -> dict:
    """Composición de los 3 bloques del arranque del panel (spec 100, US1):
    reutiliza, sin duplicar lógica, lo que ya existe para `/tenant`, `/plan`
    y `/notifications` por separado."""
    if tenant is None:
        # Super Admin (o cualquier host sin tenant resuelto, FR-007): los 3
        # bloques en null, incluyendo `notifications` — es una fuente
        # tenant-scoped igual que las otras dos (research.md Decisión 6). Sin
        # intentar ninguna consulta contra un schema que no existe.
        return {"tenant": None, "plan": None, "notifications": None}

    # Degradación granular por bloque (US3, FR-006): cada bloque va en su
    # propio try/except independiente — un fallo en uno nunca interrumpe a
    # los otros dos; el que falla queda `None`, la respuesta siempre es `200`.
    try:
        tenant_block = tenant
    except Exception:
        logger.exception("Fallo al resolver el bloque 'tenant' del bootstrap")
        tenant_block = None

    try:
        plan_block = plan_service.build_plan_summary(db, tenant)
    except Exception:
        logger.exception("Fallo al resolver el bloque 'plan' del bootstrap")
        plan_block = None

    try:
        # Los defaults de `list_notifications` son `Query(...)` de FastAPI:
        # solo se resuelven a su valor (1/20/None/100/False) cuando FastAPI
        # invoca la ruta por una petición HTTP real. Llamada aquí como
        # función Python directa (sin ese paso de resolución), hay que
        # pasarlos explícitos — de lo contrario `after_id` llega como el
        # objeto `Query`, no `None`, y entra al modo recuperación por error.
        notifications_block = notifications_router.list_notifications(
            page=1, size=20, after_id=None, limit=100, only_pending=False, db=db, _=user,
        )
    except Exception:
        logger.exception("Fallo al resolver el bloque 'notifications' del bootstrap")
        notifications_block = None

    return {
        "tenant": tenant_block,
        "plan": plan_block,
        "notifications": notifications_block,
    }
