from sqlalchemy.orm import Session

from app.core.models import Tenant
from app.core.plan_limits import RESOURCE_CONFIG, count_resource_usage
from app.core.timezone import utc_now_naive
from app.models.plan import Plan


def build_plan_summary(db: Session, tenant: Tenant) -> dict:
    """Arma el resumen de consumo del tenant (Historia de Usuario 6,
    FR-013) reutilizando la misma configuración de recursos que
    `enforce_plan_limit` (data-model.md tabla de reglas de conteo) — sin
    lock, es una lectura de solo consulta (no participa en la garantía de
    concurrencia de FR-015)."""
    plan = db.get(Plan, tenant.plan_id)

    resources = {}
    for resource_key, config in RESOURCE_CONFIG.items():
        resources[resource_key] = {
            "used": count_resource_usage(db, tenant, resource_key),
            "limit": getattr(plan, config.limit_column),
        }

    vencido = tenant.plan_vence_en is not None and tenant.plan_vence_en < utc_now_naive()

    return {
        "plan_name": plan.name,
        "ciclo_facturacion": tenant.ciclo_facturacion,
        "plan_vence_en": tenant.plan_vence_en,
        "vencido": vencido,
        "resources": resources,
        "modules": {
            "inventario": plan.inventario_access,
            "compras": plan.compras_access,
            "promociones": plan.promociones_access,
        },
    }
