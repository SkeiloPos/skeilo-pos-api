"""Traducción a HTTP de los errores del módulo super-admin, con alcance de
prefijo de ruta (spec 068).

Se apoya en dos piezas separadas, a propósito:

- `RequestIdMiddleware`: middleware ASGI que solo estampa un `request_id` por
  solicitud (para el prefijo dado) y, para ese mismo prefijo, atrapa
  cualquier excepción **no anticipada** que llegue hasta aquí sin haber sido
  ya convertida en una respuesta. Para cualquier otra ruta no hace nada
  distinto de hoy: ni genera `request_id` ni envuelve `call_next` en un
  `try/except` — así una falla no manejada en otro módulo sigue propagándose
  exactamente como hoy hasta `ServerErrorMiddleware` (Starlette), sin que
  este spec toque su comportamiento.
- `register_error_handlers`: registra los `@app.exception_handler` para
  `HTTPException`, `RequestValidationError` y `DomainError`. Estos tres tipos
  los intercepta `ExceptionMiddleware` de Starlette **antes** de que la
  excepción llegue a `RequestIdMiddleware` — por eso no se atrapan ahí, sino
  aquí, inspeccionando `request.url.path` para decidir si envolver la
  respuesta o delegar en el handler por defecto de FastAPI/Starlette.

Deliberadamente **no** se registra un `@app.exception_handler(Exception)`
global: Starlette enruta ese caso a `ServerErrorMiddleware` (el middleware
más externo, fuera de cualquier middleware de usuario), donde reemplazar su
comportamiento por defecto para otras rutas exigiría reproducir manualmente
una pieza interna de Starlette — con el riesgo real de dejar una ruta de
otro módulo sin respuesta ante un 500 no manejado. Atrapar la excepción no
anticipada en `RequestIdMiddleware`, solo para el prefijo de super-admin,
consigue lo mismo sin ese riesgo.

Cómo migrar otro módulo a este mismo patrón, más adelante: hoy
`RequestIdMiddleware`/`register_error_handlers` aceptan un único
`path_prefix: str` (`app/main.py` solo pasa `SUPER_ADMIN_ERROR_PREFIX`) —
llamarlos una segunda vez con otro prefijo **no** basta, porque
`@app.exception_handler` registra por tipo de excepción: la segunda llamada
sobrescribiría los handlers de la primera, dejando sin envelope al primer
prefijo. Cuando de verdad haga falta un segundo módulo, el cambio es
mecánico y pequeño — que `path_prefix` acepte una tupla de prefijos en
ambas funciones — pero no vale la pena construirlo antes de tener un
segundo módulo real que lo necesite. En cualquier caso, no hace falta
duplicar `domain_errors.py`, `error_response.py` ni este archivo: ya son
infraestructura reutilizable, independiente de super-admin.

Desde el spec 074 (extensión de logging operativo, FR-015–FR-021) este mismo
archivo aloja además una tercera pieza, `OperationalLogMiddleware`, con el
alcance exactamente complementario: registra en Sentry Logs una entrada por
cada petición **mutativa** de todo el backend **salvo** super-admin, tanto en
éxito como en error. Es independiente de las dos de arriba —no las modifica,
no comparte estado con ellas y sus alcances no se solapan— y vive aquí por
proximidad temática (el patrón `request_id` de la petición), no por
acoplamiento. Ver la sección al final del archivo.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Awaitable, Callable, Optional

import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler as default_http_exception_handler,
)
from fastapi.exception_handlers import (
    request_validation_exception_handler as default_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.core.config import settings
from app.core.domain_errors import DomainError
from app.core.exceptions import ModuleNotInPlanError
from app.core.error_response import (
    envelope_from_domain_error,
    envelope_from_http_exception,
    envelope_from_unexpected_exception,
    envelope_from_validation_error,
)

logger = logging.getLogger(__name__)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or str(uuid.uuid4())


def _report_unexpected_exception(request: Request, exc: Exception, request_id: str, *, module: str) -> None:
    operation = f"{request.method} {request.url.path}"
    user_id = getattr(request.state, "super_admin_id", None)

    # Mismo contexto que se envía a Sentry (module/operation/request_id/
    # user_id), en la terminal, siempre — sin importar el entorno, para que
    # se pueda depurar en desarrollo igual que se correlaciona en producción
    # (la traza completa se agrega sola: `logger.exception` adjunta
    # `exc_info` después de este mensaje).
    logger.exception(
        "Falla técnica inesperada | module=%s operation=%s request_id=%s user_id=%s",
        module, operation, request_id, user_id if user_id is not None else "-",
    )

    # Fuera de producción, sentry_sdk.init() nunca se llamó (app/main.py) — sin
    # cliente activo, capture_exception ya sería un no-op, pero se corta antes
    # para dejar explícito el gate (research.md § 6) y no depender de eso.
    if settings.ENVIRONMENT != "prod":
        return

    with sentry_sdk.push_scope() as scope:
        scope.set_tag("module", module)
        scope.set_tag("operation", operation)
        scope.set_tag("request_id", request_id)
        if user_id is not None:
            scope.set_user({"id": str(user_id)})
        sentry_sdk.capture_exception(exc)


class RequestIdMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, path_prefix: str, *, module: str = "super-admin"):
        super().__init__(app)
        self.path_prefix = path_prefix
        self.module = module

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not request.url.path.startswith(self.path_prefix):
            return await call_next(request)

        request.state.request_id = str(uuid.uuid4())
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001 - punto de captura deliberado
            request_id = _request_id(request)
            _report_unexpected_exception(request, exc, request_id, module=self.module)
            status_code, body = envelope_from_unexpected_exception(request_id)
            return JSONResponse(body, status_code=status_code)


# ==========================================================================
# Logging operativo general (spec 074, extensión FR-015–FR-021)
# ==========================================================================
#
# Pieza **independiente** de `RequestIdMiddleware`/`register_error_handlers`
# de arriba, y deliberadamente complementaria: aquellos traducen errores a
# HTTP dentro de `/api/v1/super-admin` y no registran nada en el camino feliz;
# este registra una entrada estructurada por cada petición **mutativa** de
# todo el resto del backend, tanto si termina bien como si falla, sin tocar
# jamás la respuesta. Los dos alcances son mutuamente excluyentes por ruta
# (ver `_EXCLUDED_PREFIX`), así que ninguna petición pasa por ambos
# mecanismos de `request_id` — ver `research.md` § 7 del spec 074.

#: Únicos métodos en alcance (FR-016): una lectura (`GET`/`HEAD`/`OPTIONS`),
#: incluido el polling de tiempo real, nunca genera una entrada.
_MUTATIVE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Prefijo excluido (FR-015): super-admin conserva su propio mecanismo, sin
#: cambios. Mismo valor que `app.main.SUPER_ADMIN_ERROR_PREFIX`.
_EXCLUDED_PREFIX = "/api/v1/super-admin"


def current_request_id(request: Optional[Request]) -> Optional[str]:
    """El `request_id` que `OperationalLogMiddleware` estampó para esta
    petición, o `None` si la petición no está en su alcance.

    Punto de lectura único para los routers que propagan el `request_id` a la
    auditoría de orden (FR-021), para no repetir el `getattr` defensivo en
    cada endpoint. Nunca genera uno nuevo: si no hay, el evento de auditoría
    viaja sin ese atributo, exactamente como antes de esta extensión.

    Tolera deliberadamente que `request` sea `None` o un doble de test sin
    `.state`: varios characterization tests ya existentes invocan las funciones
    de endpoint directamente, en proceso, con un `_FakeRequest` mínimo (p. ej.
    `test_cart_router.py`). Leer el `request_id` no puede ser motivo para que
    ninguno de esos contratos cambie.
    """
    state = getattr(request, "state", None)
    return getattr(state, "request_id", None)


def _route_pattern(request: Request) -> str:
    """El patrón de ruta registrado (p. ej. `/api/v1/orders/{order_id}/cancel`),
    no la URL con los valores reales (research.md § 10).

    Solo está disponible después de que el enrutamiento ocurrió (FastAPI
    estampa `scope["route"]` al hacer match). Si no hay match —404, o una
    respuesta producida por otro middleware antes de enrutar— se cae a
    `request.url.path`: es el único caso donde el valor no agrega, y es
    preferible a perder la entrada entera.
    """
    route = request.scope.get("route")
    return getattr(route, "path", None) or request.url.path


def _emit_operational_log(request: Request, *, status_code: int, started_at: float) -> None:
    """Emite la entrada de log operativo hacia Sentry Logs.

    Nunca propaga una excepción (regla 13 de `data-model.md`): la petición real
    ya terminó cuando esto corre, y un fallo al construir o enviar la entrada
    no puede alterar su respuesta.
    """
    try:
        # Mismo gate de entorno que el resto del código que toca sentry_sdk en
        # este proyecto (research.md § 2): fuera de prod, `sentry_sdk.init()`
        # nunca se llamó.
        if settings.ENVIRONMENT != "prod":
            return

        route = _route_pattern(request)
        # Atributos planos: Sentry Logs solo preserva como atributo filtrable
        # un escalar (misma razón que en `app/core/order_audit.py`). Nunca el
        # cuerpo de la petición ni de la respuesta, en ninguna forma (FR-018):
        # este diccionario es la lista completa, no una lista de exclusión.
        attributes: dict = {
            "method": request.method,
            "route": route,
            "status": status_code,
            "duration_ms": round((time.monotonic() - started_at) * 1000, 3),
        }
        request_id = current_request_id(request)
        if request_id is not None:
            attributes["request_id"] = request_id
        # Actor/tenant los resolvieron, como efecto colateral, las 3
        # dependencias compartidas (`get_tenant`, `get_current_user`,
        # `get_session_context` — research.md § 8). Se omiten, nunca se envían
        # como `None`, cuando la petición no pasó por ninguna de ellas.
        for key in ("actor_id", "actor_type", "tenant_id"):
            value = getattr(request.state, key, None)
            if value is not None:
                attributes[key] = value

        if status_code < 400:
            emit = sentry_sdk.logger.info
        elif status_code < 500:
            emit = sentry_sdk.logger.warning
        else:
            emit = sentry_sdk.logger.error
        # Etiqueta derivada automáticamente de método + ruta (FR-019, regla 11
        # de `data-model.md`) — sin tabla curada por endpoint.
        emit(f"{request.method} {route}", attributes=attributes)
    except Exception:  # noqa: BLE001 - no bloqueante por diseño
        logger.exception(
            "No se pudo registrar la entrada de log operativo (spec 074) para %s %s",
            request.method,
            request.url.path,
        )


class OperationalLogMiddleware(BaseHTTPMiddleware):
    """Registra una entrada estructurada por petición mutativa (FR-015–FR-021).

    Alcance: todo el backend **salvo** `/api/v1/super-admin`, y solo
    `POST`/`PUT`/`PATCH`/`DELETE`. Fuera de ese alcance delega en `call_next`
    sin hacer absolutamente nada más — ni estampa `request_id` ni mide tiempo.

    No modifica la respuesta nunca: devuelve tal cual la que produjo
    `call_next`, y si la petición terminó en una excepción no manejada, la
    vuelve a lanzar intacta después de dejar constancia (`status=500`), para
    que siga llegando a `ServerErrorMiddleware` exactamente como hoy.
    """

    def __init__(self, app, *, excluded_prefix: str = _EXCLUDED_PREFIX):
        super().__init__(app)
        self.excluded_prefix = excluded_prefix

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if (
            request.method not in _MUTATIVE_METHODS
            or request.url.path.startswith(self.excluded_prefix)
        ):
            return await call_next(request)

        # Antes de `call_next` a propósito: el `request_id` tiene que estar
        # disponible para el código que corre *durante* la petición — los 8
        # eventos de auditoría de orden lo leen de aquí (FR-021). Se reusa el
        # que ya hubiera estampado otra capa en vez de pisarlo.
        if getattr(request.state, "request_id", None) is None:
            request.state.request_id = str(uuid.uuid4())

        started_at = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            # Falla no manejada: queda constancia como error y la excepción
            # sigue su curso sin alterarse (ni se convierte en respuesta aquí:
            # eso es competencia de `ServerErrorMiddleware`, no de esta capa).
            _emit_operational_log(request, status_code=500, started_at=started_at)
            raise

        _emit_operational_log(request, status_code=response.status_code, started_at=started_at)
        return response


def register_error_handlers(app: FastAPI, path_prefix: str, *, module: str = "super-admin") -> None:
    """Registra los handlers de traducción a HTTP con alcance a `path_prefix`.

    Para cualquier ruta que no empiece por `path_prefix`, cada handler delega
    en el comportamiento por defecto de FastAPI/Starlette — ninguna otra ruta
    del backend cambia de forma de respuesta por este registro.
    """

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        if not request.url.path.startswith(path_prefix):
            return await default_http_exception_handler(request, exc)
        request_id = _request_id(request)
        if exc.status_code >= 500:
            # Un HTTPException >=500 sigue siendo una falla técnica, así ya
            # venga envuelta desde más abajo (p. ej. `tenant_create` en
            # `app/core/db.py`, que atrapa cualquier excepción inesperada y
            # la relanza como HTTPException(500, "Internal server error")).
            # Sin esto, ese camino nunca pasaba por `RequestIdMiddleware` (ya
            # llega convertido en respuesta antes de esa capa) y ni el
            # `request_id` ni el reporte a Sentry quedaban correlacionados
            # con el error real — capturar `exc` aquí igual reporta a Sentry
            # la excepción original completa, por el encadenamiento
            # implícito de Python (`__cause__`/`__context__`).
            _report_unexpected_exception(request, exc, request_id, module=module)
        status_code, body = envelope_from_http_exception(exc, request_id)
        return JSONResponse(body, status_code=status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(request: Request, exc: RequestValidationError):
        if not request.url.path.startswith(path_prefix):
            return await default_validation_exception_handler(request, exc)
        status_code, body = envelope_from_validation_error(exc, _request_id(request))
        return JSONResponse(body, status_code=status_code)

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request: Request, exc: DomainError):
        # DomainError es un tipo nuevo que hoy nadie más lanza (data-model.md
        # § 1): no hace falta comprobar el prefijo, pero se deja expresado
        # igual por claridad y por si algún día deja de ser exclusivo.
        status_code, body = envelope_from_domain_error(exc, _request_id(request))
        return JSONResponse(body, status_code=status_code)


# ==========================================================================
# Módulo no incluido en el plan (spec 141, A-151)
# ==========================================================================


def register_module_error_handler(app: FastAPI) -> None:
    """Responde el 403 por módulo no incluido con `{detail, code, module}` en **toda** la API.

    Es deliberadamente independiente de `register_error_handlers` (que solo cubre `/api/v1/super-admin` y
    delega en el handler por defecto fuera de ese prefijo): Starlette elige el handler recorriendo el MRO de
    la excepción, así que este tipo más específico gana sobre el de `HTTPException` sin alterar el
    comportamiento de ningún otro rechazo (rol, límite, plan vencido). Ningún módulo gobernado por plan vive
    bajo `/api/v1/super-admin`, así que los dos mecanismos no se solapan.
    """

    @app.exception_handler(ModuleNotInPlanError)
    async def _module_not_in_plan_handler(request: Request, exc: ModuleNotInPlanError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "code": exc.code, "module": exc.module},
            headers=getattr(exc, "headers", None),
        )
