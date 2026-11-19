"""Bloqueo total de un tenant con la suscripción `bloqueada` (spec 161, Historia de Usuario 2,
FR-006 a FR-010, A-162; research.md Decisión 5; contrato `blocked-access.md`).

Hasta la spec 161 un plan vencido solo cortaba la creación de recursos limitados y los módulos
gobernados por plan; ventas, órdenes, caja, catálogo, reportes y el menú QR seguían funcionando. Con
la suscripción `bloqueada` (pasadas 120 h desde `plan_vence_en` sin pago aprobado) ahora se rechaza
**toda** operación que resuelve al tenant, también la del comensal que usa el menú y el carrito
públicos, salvo las rutas exentas de abajo. Durante la gracia (`en_gracia`) no se bloquea nada.

Se aplica en los dos puntos por donde pasa todo tenant (`app/core/db.py`):

- `get_tenant`: staff y menú público, por la cabecera `x-tenant-host`. Recibe la `Request`, así que
  las exenciones por ruta funcionan.
- `resolve_tenant_by_id`: el comensal, cuyo tenant sale del token de sesión. No recibe `Request`:
  **no tiene exenciones**, porque ninguna ruta del comensal es exenta.

`get_optional_tenant` (solo `/bootstrap`, que es exenta) no lo llama.

Una ruta nueva que use `get_tenant` queda protegida por defecto; para eximirla hay que agregar su
prefijo a `EXEMPT_PREFIXES` (y `test_subscription_route_inventory.py` obliga a clasificarla).
"""
from __future__ import annotations

from typing import Optional

from starlette.requests import Request

from app.core.exceptions import SubscriptionBlockedError
from app.core.subscription import compute_subscription_status

_API_PREFIX = "/api/v1"

#: Rutas que siguen disponibles con el tenant `bloqueada` (FR-008, FR-010), por prefijo de segmento:
#:
#: - `/auth`: inicio, renovación y cierre de sesión, recuperación y cambio de contraseña. El inicio de
#:   sesión nunca se bloquea.
#: - `/bootstrap`: arranque agregado del panel; es lo que el frontend necesita para mostrar el estado.
#: - `/plan`: `GET /plan` y `/plan/subscription/*` (pedir el link de pago y ver el historial).
#: - `/notifications`: el ADMIN tiene que poder leer el aviso de bloqueo.
#: - `/realtime`: el ticket del staff, para que ese aviso llegue en vivo. El stream del comensal NO es
#:   exento: pasa por `resolve_tenant_by_id`, que no tiene exenciones.
#: - `/legal/platform-acceptance`: la aceptación de los documentos legales de la plataforma por el ADMIN (spec 162).
#:   Es un requisito de la plataforma, no una operación del negocio, y si el frontend la exige al arrancar,
#:   bloquearla dejaría al ADMIN de un tenant bloqueado atrapado antes de poder llegar a pagar. El resto de
#:   `/legal` (perfil y documento legal del tenant) es configuración del negocio y SÍ se bloquea.
#:
#: Las rutas sin tenant (`/super-admin`, `/health`, `/webhooks`) no llaman al guard, así que nunca se bloquean.
EXEMPT_PREFIXES = ("/auth", "/bootstrap", "/plan", "/notifications", "/realtime", "/legal/platform-acceptance")


def is_exempt_path(path: str) -> bool:
    """`path` sin el prefijo `/api/v1`. La comparación es por **segmento**: `/plan` y `/plan/...` sí,
    `/planes` no."""
    return any(path == prefix or path.startswith(prefix + "/") for prefix in EXEMPT_PREFIXES)


def _request_path(request: Request) -> str:
    """Ruta de la petición sin `/api/v1`. Usa la plantilla resuelta por Starlette (`path_format`, p. ej.
    `/notifications/{notification_id}/attend`) y, si aún no hay ruta resuelta, la URL. Mismo mecanismo
    que `_enforce_mesero_scope` en `app/core/dependencies.py`."""
    route = request.scope.get("route")
    path = getattr(route, "path_format", None) or getattr(route, "path", None) or request.url.path
    if path.startswith(_API_PREFIX):
        path = path[len(_API_PREFIX):]
    return path


def enforce_subscription_access(tenant, request: Optional[Request] = None) -> None:
    """Lanza `SubscriptionBlockedError` si la suscripción del tenant está `bloqueada` y la ruta no es exenta.

    Sin `request` (el comensal) no hay exenciones. El estado se recalcula en cada llamada a partir de
    `tenant.plan_vence_en`, sin cachés (FR-009): el tenant ya viene fresco de la base en cada petición,
    así que un pago aprobado desbloquea la petición siguiente.

    La `request` solo se toca cuando el tenant YA está bloqueado: con un tenant al día o en gracia (el
    caso de casi todas las peticiones) esta función no hace más que una comparación de fechas.
    """
    # `getattr`: un `Tenant` real siempre tiene la columna (NULL = nunca vence); el `default` solo existe para
    # los dobles de prueba que no la declaran (p. ej. `test_operational_log.py`, spec 074, que fija que
    # `get_tenant` devuelve el mismo objeto). Un objeto sin la columna cuenta como «sin vencimiento».
    if compute_subscription_status(getattr(tenant, "plan_vence_en", None)).state != "bloqueada":
        return
    if request is not None and is_exempt_path(_request_path(request)):
        return
    raise SubscriptionBlockedError()
