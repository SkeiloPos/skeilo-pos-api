"""Lógica de la suscripción del tenant: el link de pago de Wompi (spec 161, Historia de Usuario 3,
FR-011 a FR-018; research.md Decisiones 2 y 10; contrato `subscription-api.md § 2`).

Historia de Usuario 4 (FR-019 a FR-027; research.md Decisiones 9 y 11; contrato `wompi-webhook.md § 2.2`): la
confirmación del pago por el webhook de Wompi.

Historia de Usuario 6 (FR-033 a FR-035): el historial de pagos.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import wompi
from app.core.config import settings
from app.core.exceptions import PaymentGatewayNotConfiguredError, SubscriptionNotPayableError
from app.core.models import Tenant, User
from app.core.plan_limits import calculate_plan_vencimiento
from app.core.subscription import compute_subscription_status
from app.core.timezone import utc_now_naive
from app.models.plan import Plan
from app.models.subscription_payment import SubscriptionPayment

logger = logging.getLogger(__name__)


# ======================================================================
# Historia 3 — el link de pago
# ======================================================================


def _find_pending(db: Session, tenant_id: int) -> Optional[SubscriptionPayment]:
    """El pago `pending` del tenant, si lo hay. El índice único parcial
    `ux__subscription_payments__tenant_id__pending` garantiza que haya a lo sumo uno."""
    return db.execute(
        select(SubscriptionPayment).where(
            SubscriptionPayment.tenant_id == tenant_id,
            SubscriptionPayment.status == "pending",
        )
    ).scalar_one_or_none()


def _ensure_payable(db: Session, tenant: Tenant, now: datetime) -> Plan:
    """FR-012/FR-013: solo se cobra la mensualidad de un tenant mensual con vencimiento, precio mensual y
    sin el ciclo en curso ya pagado. Cualquier otro caso es un conflicto controlado (`409`), antes de
    escribir nada. Devuelve el plan vigente (de donde sale el monto)."""
    if tenant.plan_vence_en is None:
        raise SubscriptionNotPayableError("no_expiry")
    if tenant.ciclo_facturacion != "mensual":
        raise SubscriptionNotPayableError("unsupported_cycle")
    plan = db.get(Plan, tenant.plan_id)
    # `<= 0` además de `None`: la tabla exige `amount > 0` y un precio de cero no es cobrable.
    if plan is None or plan.precio_mensual is None or plan.precio_mensual <= 0:
        raise SubscriptionNotPayableError("no_price")
    if compute_subscription_status(tenant.plan_vence_en, now).state == "activa":
        # Un solo período abierto: con el ciclo en curso ya pagado no se pagan meses por adelantado (D5).
        raise SubscriptionNotPayableError("already_active")
    return plan


def get_or_create_payment_link(
    db: Session, tenant: Tenant, user: User, *, now: Optional[datetime] = None
) -> tuple[SubscriptionPayment, bool]:
    """Devuelve el link de pago del tenant y si era uno ya existente (`reused`).

    Orden (contrato § 2; la primera comprobación que falla corta y **ninguna escribe antes de pasar todas**):

    1. Credenciales de Wompi → `PaymentGatewayNotConfiguredError` (503).
    2. Bloqueo de la fila del tenant (`FOR UPDATE`, mismo patrón que `enforce_plan_limit`): serializa las
       solicitudes del mismo tenant y asegura que el vencimiento que se evalúa es el vigente, no uno viejo.
    3. Estado, ciclo y precio → `SubscriptionNotPayableError` (409).
    4. Si ya hay un pago `pending` vigente se devuelve ese mismo (FR-016); si expiró, se marca `expired`.
    5. Se crea el pago `pending` con su link firmado.

    Red de seguridad: el índice único parcial de «un solo pendiente por tenant». En SQLite (las pruebas)
    `FOR UPDATE` no existe, pero el índice sí protege: si dos solicitudes llegan a insertar, la segunda choca
    y aquí se resuelve devolviendo el pago ganador.
    """
    now = now if now is not None else utc_now_naive()

    if not wompi.gateway_configured(settings):
        raise PaymentGatewayNotConfiguredError()

    locked = db.execute(
        select(Tenant).where(Tenant.id == tenant.id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()
    plan = _ensure_payable(db, locked, now)

    pending = _find_pending(db, locked.id)
    if pending is not None:
        if pending.payment_link_expires_at > now:
            return pending, True
        # Venció sin resultado: se marca y se hace `flush()` ANTES de insertar el nuevo, para que el índice
        # parcial no vea dos pendientes a la vez.
        pending.status = "expired"
        db.flush()

    payment_id = uuid.uuid4()
    reference = wompi.build_reference(payment_id)
    # Sin microsegundos: lo que se guarda tiene que ser exactamente lo que se firma y se manda a Wompi.
    expires_at = (now + timedelta(hours=settings.WOMPI_LINK_TTL_HOURS)).replace(microsecond=0)
    expiration = wompi.format_expiration(expires_at)
    cents = wompi.amount_in_cents(plan.precio_mensual)
    signature = wompi.integrity_signature(reference, cents, wompi.CURRENCY, expiration, settings.WOMPI_INTEGRITY_SECRET)
    url = wompi.build_checkout_url(
        base_url=settings.WOMPI_CHECKOUT_URL,
        public_key=settings.WOMPI_PUBLIC_KEY,
        reference=reference,
        amount_in_cents=cents,
        currency=wompi.CURRENCY,
        signature=signature,
        # `.replace` y no `.format`: una plantilla mal escrita con otras llaves no debe volverse un 500.
        redirect_url=settings.WOMPI_REDIRECT_URL_TEMPLATE.replace("{host}", locked.host),
        expiration=expiration,
    )

    payment = SubscriptionPayment(
        id=payment_id,
        tenant_id=locked.id,
        plan_id=plan.id,
        ciclo_facturacion="mensual",
        amount=plan.precio_mensual,
        status="pending",
        gateway="wompi",
        gateway_reference=reference,
        payment_link_url=url,
        payment_link_expires_at=expires_at,
        requested_by_user_id=user.id,
    )
    db.add(payment)
    try:
        db.commit()
    except IntegrityError:
        # Otra solicitud simultánea ya insertó el pendiente de este tenant: se devuelve el suyo.
        db.rollback()
        existing = _find_pending(db, tenant.id)
        if existing is None:
            raise
        logger.info("Solicitud simultánea de link de pago para el tenant %s: se reutiliza el pendiente", tenant.id)
        return existing, True
    return payment, False
