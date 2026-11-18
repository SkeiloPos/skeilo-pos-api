"""Rate limiting de las rutas públicas del QR, sobre el Redis que ya usa el
blocklist de JWT.

Deliberadamente sin `slowapi`: son ~40 líneas de INCR+EXPIRE y así no se añade una
dependencia ni un estado global de middleware para esto.

Se limita por **dos** claves a la vez:

- por IP, contra el abuso genérico;
- por mesa, porque una sola mesa detrás de un NAT compartido no debe poder agotar
  el cupo del local — y al revés, un atacante que rote IPs sigue topado por mesa.

El `table_id` sale siempre del token firmado ya verificado, nunca de un campo del
body: si viniera del cliente, saltarse el límite sería tan fácil como cambiarlo.
"""
import logging
import time
import uuid

from fastapi import HTTPException, Request, status

from app.core.config import settings
from app.core.redis import token_blocklist as redis

logger = logging.getLogger(__name__)


def _client_ip(request: Request) -> str:
    """IP del cliente. Detrás de proxy se usa el primer salto de X-Forwarded-For."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "desconocida"


async def _hit(key: str, limit: int, window: int) -> int:
    """Suma una petición a la ventana y devuelve cuántas van. El EXPIRE se pone
    solo en la primera, así la ventana es fija desde el primer hit y no se renueva
    indefinidamente con el tráfico."""
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window)
    return count


#: Límite de IP propio por bucket (spec 162, research.md Decisión 9): a
#: diferencia de los buckets del QR (todos comparten `RATE_LIMIT_PER_IP`),
#: los endpoints públicos de documentos legales no tienen mesa asociada y
#: necesitan su propio umbral, más bajo. Un bucket ausente de este mapa sigue
#: usando `settings.RATE_LIMIT_PER_IP` (comportamiento sin cambios).
_PER_IP_LIMIT_OVERRIDES = {
    "legal_public": lambda: settings.RATE_LIMIT_LEGAL_PUBLIC_PER_IP,
}


async def enforce(request: Request, bucket: str, *, table_id=None) -> None:
    """Aplica el límite al par (IP, mesa). Lanza 429 con `Retry-After`.

    Si Redis no responde **no se bloquea la petición**: preferimos servir de más a
    tirar el menú del local entero por una caída de la caché. Queda en el log.
    """
    if not settings.RATE_LIMIT_ENABLED:
        return

    window = settings.RATE_LIMIT_WINDOW_SECONDS
    per_ip_limit = _PER_IP_LIMIT_OVERRIDES.get(bucket, lambda: settings.RATE_LIMIT_PER_IP)()
    checks = [(f"rl:{bucket}:ip:{_client_ip(request)}", per_ip_limit)]
    if table_id is not None:
        checks.append((f"rl:{bucket}:tb:{table_id}", settings.RATE_LIMIT_PER_TABLE))

    try:
        for key, limit in checks:
            if await _hit(key, limit, window) > limit:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Demasiadas peticiones. Inténtalo en un momento.",
                    headers={"Retry-After": str(window)},
                )
    except HTTPException:
        raise
    except Exception:
        logger.warning("Rate limiting no disponible (Redis); se deja pasar", exc_info=True)


async def enforce_sliding_window(key: str, limit: int, window_seconds: int) -> bool:
    """Ventana deslizante genuina (ZSET), a diferencia de `enforce()` (ventana
    fija). Devuelve `True` si la clave está bloqueada (ya alcanzó `limit` en los
    últimos `window_seconds`), sin lanzar excepción — el caller decide cómo
    responder (spec 031, FR-010, research.md Decisión 3).

    Fail-open: si Redis no responde, no se bloquea (mismo criterio que `enforce()`).
    """
    now = time.time()
    try:
        pipe = redis.pipeline()
        pipe.zremrangebyscore(key, "-inf", now - window_seconds)
        pipe.zcard(key)
        _, count = await pipe.execute()

        if count >= limit:
            return True

        pipe = redis.pipeline()
        pipe.zadd(key, {f"{now}:{uuid.uuid4()}": now})
        pipe.expire(key, window_seconds)
        await pipe.execute()
        return False
    except Exception:
        logger.warning("Rate limiting deslizante no disponible (Redis); se deja pasar", exc_info=True)
        return False
