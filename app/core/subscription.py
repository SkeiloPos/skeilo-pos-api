"""Estado de la suscripción de un tenant (spec 161, Historia de Usuario 1, research.md
Decisión 4).

Es una **función pura**: recibe `plan_vence_en` y un `now` inyectable, y devuelve el estado.
Un único cálculo lo comparten `GET /plan`, `ensure_plan_not_expired`, el guard de bloqueo, el
job de avisos y la validación del link de pago, para que nunca discrepen.

Tramos exactos en UTC, no días calendario del tenant (decisión D6 de la spec):

====================================  ============
Condición                             Estado
====================================  ============
`plan_vence_en` es `NULL`             `activa`
`now < vence − 48 h`                  `activa`
`vence − 48 h ≤ now < vence`          `por_vencer`
`vence ≤ now < vence + 120 h`         `en_gracia`
`now ≥ vence + 120 h`                 `bloqueada`
====================================  ============

Solo `bloqueada` corta el servicio; en `en_gracia` todo opera con normalidad (A-161).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from app.core.timezone import utc_now_naive

#: Cuánto antes del vencimiento el tenant pasa a `por_vencer` (y se emite el primer aviso).
ALERT_WINDOW = timedelta(hours=48)
#: Cuánto dura la gracia tras el vencimiento. Cumplida, el tenant queda `bloqueada`.
GRACE_PERIOD = timedelta(hours=120)

SubscriptionState = Literal["activa", "por_vencer", "en_gracia", "bloqueada"]


@dataclass(frozen=True)
class SubscriptionStatus:
    """Resultado de `compute_subscription_status`. Inmutable: se comparte entre consumidores."""

    state: SubscriptionState
    #: Días enteros (redondeados hacia arriba) hasta el vencimiento mientras no ha vencido, hasta
    #: `fecha_limite_pago` durante la gracia y `0` si está bloqueada. `None` sin vencimiento.
    dias_restantes: Optional[int]
    #: `plan_vence_en + 120 h` (UTC naive). `None` sin vencimiento.
    fecha_limite_pago: Optional[datetime]


def _as_naive_utc(value: datetime) -> datetime:
    """Las columnas de fecha del proyecto son UTC *naive*; si llega un valor con zona se lleva a UTC
    y se le quita, para que la comparación nunca mezcle naive con aware."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _ceil_days(delta: timedelta) -> int:
    """Días enteros hacia arriba, sin pasar por `float` (evita errores de redondeo en fronteras)."""
    days, remainder = divmod(delta, timedelta(days=1))
    return days + (1 if remainder > timedelta(0) else 0)


def compute_subscription_status(
    plan_vence_en: Optional[datetime], now: Optional[datetime] = None
) -> SubscriptionStatus:
    """Estado de la suscripción para un `plan_vence_en` dado.

    `now` es inyectable para probar cada frontera sin tocar el reloj; por defecto es
    `utc_now_naive()` (spec 138, FR-005: única forma de leer "ahora" para columnas sin zona).
    """
    if plan_vence_en is None:
        return SubscriptionStatus(state="activa", dias_restantes=None, fecha_limite_pago=None)

    vence = _as_naive_utc(plan_vence_en)
    ahora = _as_naive_utc(now) if now is not None else utc_now_naive()
    limite = vence + GRACE_PERIOD

    if ahora >= limite:
        return SubscriptionStatus(state="bloqueada", dias_restantes=0, fecha_limite_pago=limite)
    if ahora >= vence:
        return SubscriptionStatus(
            state="en_gracia", dias_restantes=_ceil_days(limite - ahora), fecha_limite_pago=limite
        )
    state: SubscriptionState = "por_vencer" if ahora >= vence - ALERT_WINDOW else "activa"
    return SubscriptionStatus(state=state, dias_restantes=_ceil_days(vence - ahora), fecha_limite_pago=limite)
