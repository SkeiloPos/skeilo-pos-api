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


# ======================================================================
# Historia 4 — la confirmación del pago (webhook)
# ======================================================================

_REJECTING_STATUSES = ("DECLINED", "ERROR")


def apply_wompi_event(db: Session, event: dict, *, now: Optional[datetime] = None) -> str:
    """Aplica un evento `transaction.updated` YA VERIFICADO (`wompi.verify_event`) y devuelve `"processed"`,
    `"duplicate"` o `"ignored"`. Todo ocurre en **una** transacción de la base compartida (FR-023): el pago y las
    fechas del tenant cambian juntos o no cambian.

    El pago se localiza por `transaction.reference` (el aviso no llega por el subdominio del tenant, FR-020) y se
    bloquea con `SELECT … FOR UPDATE`, así que dos entregas simultáneas del mismo evento se serializan y la segunda
    ve el resultado de la primera. Una aprobación NO genera notificación (la spec define solo los tres avisos), así
    que el webhook nunca entra al esquema del tenant.

    Matriz (data-model.md § 2.1), con `S` = estado del pago:

    - `APPROVED`: desde `pending`, `expired` y `rejected` se aplica (el dinero ya entró, FR-025; un reintento
      dentro del mismo checkout puede aprobar tras un rechazo); desde `approved` es `duplicate`; desde `voided` se
      ignora.
    - `DECLINED`/`ERROR`: solo `pending → rejected`. Nunca retroceden un `approved`.
    - `VOIDED`: solo `approved → voided`, **sin revertir fechas** (lo resuelve soporte a mano, D7).
    - Cualquier otro estado (`PENDING`...) se ignora.

    Un evento cuyo monto o moneda no coincide con el pago, con referencia desconocida o con una transacción ya
    ligada a otro pago se ignora sin cambiar nada (FR-026); la respuesta es `200` para que Wompi no reintente.
    """
    now = now if now is not None else utc_now_naive()
    try:
        return _apply_wompi_event(db, event, now)
    except IntegrityError:
        # Carrera contra otra entrega que ligó la misma transacción a otro pago (índice único parcial).
        db.rollback()
        logger.warning("Evento de Wompi rechazado por el índice de transacción única (referencia %s)",
                       ((event.get("data") or {}).get("transaction") or {}).get("reference"))
        return "ignored"
    except Exception:
        db.rollback()  # el pago no queda a medias: Wompi reintentará y se procesará completo
        raise


def _apply_wompi_event(db: Session, event: dict, now: datetime) -> str:
    tx = (event.get("data") or {}).get("transaction")
    if not isinstance(tx, dict):
        return "ignored"
    reference = tx.get("reference")
    tx_id = str(tx["id"]) if tx.get("id") is not None else None
    status = str(tx.get("status") or "").upper()

    payment = None
    if reference:
        payment = db.execute(
            select(SubscriptionPayment)
            .where(SubscriptionPayment.gateway_reference == reference)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
    if payment is None:
        logger.warning("Evento de Wompi con referencia desconocida: reference=%s transaction.id=%s", reference, tx_id)
        return "ignored"

    # El monto y la moneda deben coincidir con lo que se cobró: lo que Wompi cobró es lo que firmamos.
    try:
        cents = int(tx.get("amount_in_cents"))
    except (TypeError, ValueError):
        cents = None
    if cents != wompi.amount_in_cents(payment.amount) or tx.get("currency") != wompi.CURRENCY:
        logger.warning(
            "Evento de Wompi con monto o moneda distintos del pago: reference=%s transaction.id=%s",
            reference, tx_id,
        )
        return "ignored"

    # Una transacción de Wompi no puede aplicarse a dos pagos distintos (defensa en profundidad: `reference`
    # no va entre los campos firmados del evento).
    if tx_id is not None:
        bound_elsewhere = db.execute(
            select(SubscriptionPayment.id).where(
                SubscriptionPayment.gateway_transaction_id == tx_id,
                SubscriptionPayment.id != payment.id,
            )
        ).first()
        if bound_elsewhere is not None:
            logger.warning(
                "Evento de Wompi con una transacción ya ligada a otro pago: reference=%s transaction.id=%s",
                reference, tx_id,
            )
            return "ignored"

    if status == "APPROVED":
        if payment.status == "approved":
            return "duplicate"
        if payment.status == "voided":
            return "ignored"
        return _approve(db, payment, tx_id, now)

    if status in _REJECTING_STATUSES:
        if payment.status != "pending":
            return "ignored"
        payment.status = "rejected"
        payment.gateway_transaction_id = tx_id
        db.commit()
        return "processed"

    if status == "VOIDED":
        if payment.status != "approved":
            return "ignored"
        payment.status = "voided"
        db.commit()
        return "processed"

    return "ignored"


def _approve(db: Session, payment: SubscriptionPayment, tx_id: Optional[str], now: datetime) -> str:
    """Marca el pago aprobado y recalcula la vigencia del tenant (FR-022, RN-7):

    - antes del vencimiento o durante la gracia → el período nuevo parte del vencimiento anterior;
    - con el tenant `bloqueada` → parte de la fecha de pago.

    Un tenant que ya no tiene ciclo mensual con vencimiento (p. ej. el Super Admin lo pasó a anual mientras el
    link seguía pendiente) deja el pago `approved` **sin** tocar fechas y se avisa en el log (FR-027)."""
    tenant = db.execute(
        select(Tenant).where(Tenant.id == payment.tenant_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()

    payment.status = "approved"
    payment.paid_at = now
    payment.gateway_transaction_id = tx_id

    if tenant.ciclo_facturacion == "mensual" and tenant.plan_vence_en is not None:
        blocked = compute_subscription_status(tenant.plan_vence_en, now).state == "bloqueada"
        start = now if blocked else tenant.plan_vence_en
        end = calculate_plan_vencimiento(start, "mensual")
        payment.period_start = start
        payment.period_end = end
        tenant.plan_iniciado_en = start
        tenant.plan_vence_en = end
    else:
        logger.warning(
            "Pago aprobado de un tenant sin ciclo mensual con vencimiento: no se tocan fechas, requiere revisión de "
            "soporte (reference=%s tenant_id=%s)",
            payment.gateway_reference, payment.tenant_id,
        )

    db.commit()
    return "processed"


# ======================================================================
# Historia 6 — el historial de pagos
# ======================================================================


def list_payments(db: Session, tenant_id: int, page: int, size: int) -> tuple[list[SubscriptionPayment], int]:
    """Pagos de UN tenant, del más reciente al más antiguo, paginados, y su total (FR-033, FR-034).

    El `tenant_id` lo pone siempre el llamador a partir del tenant resuelto por la petición (`x-tenant-host`) o
    de la ruta del Super Admin; **nunca** viene de un parámetro del cliente, así que un ADMIN no puede pedir los
    pagos de otro tenant (SC-010). Se desempata por `id` para que dos pagos con la misma fecha salgan siempre en
    el mismo orden entre páginas."""
    where = SubscriptionPayment.tenant_id == tenant_id
    total = db.execute(select(func.count()).select_from(SubscriptionPayment).where(where)).scalar_one()
    items = db.execute(
        select(SubscriptionPayment)
        .where(where)
        .order_by(SubscriptionPayment.created_at.desc(), SubscriptionPayment.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    ).scalars().all()
    return list(items), total
