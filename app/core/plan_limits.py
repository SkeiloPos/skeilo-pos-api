"""Cumplimiento del plan de suscripción de un tenant (spec 033): límites
numéricos, acceso a módulos, y vencimiento. Consumido por los cinco
endpoints de creación de recursos limitados (mesas/cajas/usuarios/productos/
métodos de pago activos) y por los routers de inventario/compras/
promociones.

`ensure_plan_not_expired` (Historia de Usuario 5, FR-019/FR-020/FR-021) se
llama como primer paso dentro de `enforce_plan_limit`/`require_module_access`
— ningún router de recurso o módulo necesita tocarse para heredar el
bloqueo por vencimiento (research.md Decisión 14).
"""
from dataclasses import dataclass
from typing import Callable, Optional

from dateutil.relativedelta import relativedelta
from fastapi import Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_db, get_tenant
from app.core.dependencies import get_current_user
from app.core.models import Tenant, User, UserInvitation
from app.core.timezone import utc_now_naive
from app.models.cash_register import CashRegister
from app.models.dining_table import DiningTable
from app.models.payment import PaymentMethod
from app.models.plan import Plan
from app.models.product import Product


@dataclass(frozen=True)
class _ResourceConfig:
    label: str
    model: type
    limit_column: str
    filter_active: bool
    scope: str  # "schema" (tabla del esquema tenant) | "tenant_id" (tabla shared filtrada por tenant_id)


RESOURCE_CONFIG: dict[str, _ResourceConfig] = {
    "mesas": _ResourceConfig("mesas", DiningTable, "mesas_limit", filter_active=False, scope="schema"),
    "cajas": _ResourceConfig("cajas", CashRegister, "cajas_limit", filter_active=False, scope="schema"),
    "usuarios": _ResourceConfig("usuarios", User, "usuarios_limit", filter_active=False, scope="tenant_id"),
    "productos": _ResourceConfig("productos", Product, "productos_limit", filter_active=False, scope="schema"),
    "metodos_pago_activos": _ResourceConfig(
        "métodos de pago", PaymentMethod, "metodos_pago_activos_limit", filter_active=True, scope="schema"
    ),
}


def _count_resource(db: Session, tenant: Tenant, config: _ResourceConfig) -> int:
    stmt = select(func.count()).select_from(config.model)
    if config.scope == "tenant_id":
        stmt = stmt.where(config.model.tenant_id == tenant.id)
    if config.filter_active:
        stmt = stmt.where(config.model.active.is_(True))
    count = db.execute(stmt).scalar_one()

    # spec 037, research.md Decisión 5: el cupo de "usuarios" también reserva
    # las invitaciones aún no consumidas — si no, invitar sería una vía para
    # eludir en silencio el límite del plan (nunca se inserta un `User` hasta
    # que la persona invitada consume la invitación).
    if config.model is User:
        count += db.execute(
            select(func.count())
            .select_from(UserInvitation)
            .where(
                UserInvitation.tenant_id == tenant.id,
                UserInvitation.status == "pending",
            )
        ).scalar_one()

    return count


def count_resource_usage(db: Session, tenant: Tenant, resource_key: str) -> int:
    """Mismo conteo que usa `enforce_plan_limit`, expuesto para lectura pura
    (sin lock) — usado por `GET /plan` (Historia de Usuario 6, FR-013)."""
    return _count_resource(db, tenant, RESOURCE_CONFIG[resource_key])


def ensure_plan_not_expired(tenant: Tenant) -> None:
    """Bloquea (límites y módulos por igual) si `tenant.plan_vence_en` ya
    pasó sin que el Super Admin haya renovado (FR-019). `NULL` = nunca
    vence (FR-021) — no bloquea nunca por este motivo. Llamada como primer
    paso de `enforce_plan_limit`/`require_module_access` (research.md
    Decisión 14): ningún router necesita invocarla por su cuenta."""
    if tenant.plan_vence_en is None:
        return
    if tenant.plan_vence_en < utc_now_naive():
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Tu plan venció. Debe renovarse para seguir usando el sistema.",
        )


def enforce_plan_limit(db: Session, tenant: Tenant, resource_key: str) -> None:
    """Bloquea la creación de `resource_key` si el tenant ya alcanzó el
    límite de su plan vigente (FR-005/FR-006/FR-007), o si su plan venció
    (FR-019). Lockea la fila del tenant (`FOR UPDATE`) antes de contar,
    garantizando que dos solicitudes concurrentes para el mismo cupo nunca
    ambas pasen (FR-015, research.md Decisión 5) — debe llamarse dentro de
    la misma transacción que el `insert` posterior, antes de hacer
    `commit()`."""
    config = RESOURCE_CONFIG[resource_key]

    locked_tenant = db.execute(
        select(Tenant).where(Tenant.id == tenant.id).with_for_update()
    ).scalar_one()

    ensure_plan_not_expired(locked_tenant)

    plan = db.get(Plan, locked_tenant.plan_id)
    limit = getattr(plan, config.limit_column)
    if limit is None:
        return  # ilimitado (FR-007)

    count = _count_resource(db, locked_tenant, config)
    if count >= limit:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Límite de {config.label} alcanzado ({limit}). Actualiza tu plan para crear más.",
        )


def ensure_module_access(db: Session, tenant: Tenant, module_key: str) -> None:
    """Misma comprobación que `require_module_access` (acceso a módulo por plan,
    FR-008/FR-009/FR-019), pero invocable directamente dentro de un handler —
    no solo como dependencia de ruta completa de FastAPI. Existe para gatear
    campos concretos (ej. `Product.tracks_inventory`, `Option.inventory_item_id`)
    dentro de endpoints que deben seguir funcionando para tenants sin el módulo
    (spec 064, research.md Decisión 4) — a diferencia de `unit_measures`/`reports`
    (spec 062), donde el módulo completo se gatea a nivel de router/ruta."""
    ensure_plan_not_expired(tenant)
    access_column = f"{module_key}_access"
    labels = {"inventario": "inventario", "compras": "compras", "promociones": "promociones"}
    label = labels.get(module_key, module_key)

    plan = db.get(Plan, tenant.plan_id)
    if not getattr(plan, access_column):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Tu plan actual no incluye el módulo de {label}.",
        )


def has_module_access(db: Session, tenant: Tenant, module_key: str) -> bool:
    """Versión no-lanzante de `ensure_module_access` (spec 123): para un
    endpoint que debe seguir funcionando igual con o sin el módulo, solo
    cambiando qué validación aplica (ej. "¿se valida stock de esta adición?").

    A propósito NO delega en `ensure_module_access` envuelto en un
    `try/except HTTPException`: ese envoltorio también atraparía el 403 de
    `ensure_plan_not_expired` (plan vencido) y lo confundiría con "sin
    módulo", cuando son dos motivos de negocio distintos — un plan vencido
    debería seguir siendo un problema de plan, no degradar en silencio a "sin
    inventario". Esta función solo mira el acceso al módulo en sí."""
    plan = db.get(Plan, tenant.plan_id)
    return bool(getattr(plan, f"{module_key}_access", False))


def require_module_access(module_key: str) -> Callable:
    """Dependencia FastAPI: deniega el acceso si el plan vigente del tenant
    no incluye `module_key` (FR-008/FR-009), o si el plan venció (FR-019).
    Sin lock — solo lectura, no hay condición de carrera que resolver para
    un acceso de módulo. Wrapper delgado sobre `ensure_module_access` (spec 064)
    — mismo comportamiento de siempre para los routers que ya la usan."""

    def _dependency(
        tenant: Tenant = Depends(get_tenant),
        db: Session = Depends(get_db),
        _user: User = Depends(get_current_user),
    ) -> None:
        ensure_module_access(db, tenant, module_key)

    return _dependency


def calculate_plan_vencimiento(inicio, ciclo: Optional[str]):
    """`inicio + 1 mes` (ciclo "mensual") o `+ 1 año` (ciclo "anual"), vía
    `dateutil.relativedelta` (research.md Decisión 13). `None` si `ciclo` es
    `None` — sin vencimiento (FR-021)."""
    if ciclo is None:
        return None
    if ciclo == "mensual":
        return inicio + relativedelta(months=1)
    if ciclo == "anual":
        return inicio + relativedelta(years=1)
    raise ValueError(f"ciclo_facturacion inválido: {ciclo!r}")


def validate_billing_cycle_price(plan: Plan, ciclo: Optional[str]) -> None:
    """`409` si el plan no tiene precio definido para el ciclo elegido
    (FR-017). `None` (sin vencimiento) nunca requiere precio."""
    if ciclo is None:
        return
    price_column = {"mensual": "precio_mensual", "anual": "precio_anual"}.get(ciclo)
    if price_column is None:
        raise ValueError(f"ciclo_facturacion inválido: {ciclo!r}")
    if getattr(plan, price_column) is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"El plan '{plan.name}' no tiene precio definido para el ciclo '{ciclo}'.",
        )
