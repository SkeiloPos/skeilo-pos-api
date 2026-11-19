from sqlalchemy.orm import Session

from app.core.models import Tenant
from app.core.plan_limits import RESOURCE_CONFIG, count_resource_usage
from app.core.subscription import compute_subscription_status
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

    # spec 161: el mismo cálculo puro que usan el bloqueo y el job de avisos, para que nunca discrepen.
    # `vencido` pasa a significar "bloqueada" (A-163): en gracia el tenant sigue operando con normalidad.
    subscription = compute_subscription_status(tenant.plan_vence_en)

    return {
        "plan_name": plan.name,
        "ciclo_facturacion": tenant.ciclo_facturacion,
        "plan_vence_en": tenant.plan_vence_en,
        "vencido": subscription.state == "bloqueada",
        "subscription_state": subscription.state,
        "dias_restantes": subscription.dias_restantes,
        "fecha_limite_pago": subscription.fecha_limite_pago,
        "resources": resources,
        "modules": {
            "inventario": plan.inventario_access,
            "promociones": plan.promociones_access,
            "cocina": plan.cocina_access,
        },
    }


def get_qr_payment_instruction_mode(db: Session, tenant: Tenant) -> str:
    """Modalidad de pago del menú QR del plan vigente del tenant (spec 154,
    FR-005). Sin caché: se recalcula en cada llamada desde `tenant.plan_id`,
    nunca persistida en la orden (FR-014) — ver `cart/router.py`."""
    plan = db.get(Plan, tenant.plan_id)
    return plan.modalidad_pago_qr
