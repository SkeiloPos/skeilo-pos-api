import logging
import uuid
from datetime import timezone
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.core.db import find_tenant_by_host, get_db, get_tenant, with_db
from app.core.models import User, Tenant
from app.core.utils import decode_token
from app.core.redis import token_in_blocklist
bearer_scheme = HTTPBearer()

logger = logging.getLogger(__name__)

from typing import Any, List, Optional

from fastapi import Depends, Request
from fastapi.security import HTTPBearer
from fastapi.security.http import HTTPAuthorizationCredentials

# from src.db.redis import token_in_blocklist

from .utils import decode_token
from .exceptions import (
    InvalidToken,
    RefreshTokenRequired,
    AccessTokenRequired
)



class TokenBearer(HTTPBearer):
    def __init__(self, auto_error=True):
        super().__init__(auto_error=auto_error)

    async def __call__(self, request: Request) -> HTTPAuthorizationCredentials | None:
        creds = await super().__call__(request)

        token_data = decode_token(creds.credentials)

        # 401 y no 403: un token caducado o inválido se resuelve re-logueándose,
        # no es una cuestión de permisos. El resto de dependencias ya usa 401.
        if token_data is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

        # Un token de QR/sesión (claim `typ`) nunca es un token de usuario.
        if token_data.get("typ"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

        if await token_in_blocklist(token_data.get("jti") or ""):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail={
                                    "error": "Token has been revoked",
                                    "resolution": "Please log in again to obtain a new token"
                                })

        self.verify_token_data(token_data)

        return token_data

    def verify_token_data(self, token_data):
        raise NotImplementedError("Please Override this method in child classes")


class AccessTokenBearer(TokenBearer):
    def verify_token_data(self, token_data: dict) -> None:
        if token_data.get("refresh"):
            raise AccessTokenRequired()


class RefreshTokenBearer(TokenBearer):
    def verify_token_data(self, token_data: dict) -> None:
        if not token_data.get("refresh"):
            raise RefreshTokenRequired()


async def get_valid_token_data(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    """Valida un access token de usuario: firma/expiración, que no sea un refresh
    ni un token de QR/sesión, y que su jti no esté revocado.

    Se declara `async` (a diferencia de las dependencias que la consumen) para
    poder consultar el blocklist en Redis sin volver `async` a `get_current_user`,
    cuyo `db.execute()` bloqueante debe seguir corriendo en el threadpool.
    """
    token_data = decode_token(credentials.credentials)
    if not token_data or token_data.get("refresh") or token_data.get("typ"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    if await token_in_blocklist(token_data.get("jti") or ""):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
        )

    return token_data


def _reject_if_session_revoked(user: User, token_data: dict) -> None:
    """Cierre de sesiones (spec 031, FR-009/FR-017): un JWT acuñado antes del
    último cambio de contraseña exitoso deja de aceptarse. Se evalúa **después**
    de la relectura por id + `active==True` de RN-AUTH-07/A-23, nunca en su
    lugar — el detalle es distinguible de "revocado por logout" y de
    "inactivo" (research.md Decisión 1).
    """
    if user.tokens_valid_after is None:
        return
    iat = token_data.get("iat")
    # `tokens_valid_after` es una columna DateTime naive que guarda un instante
    # UTC (convención del proyecto, app/core/timezone.py::utc_now); `.timestamp()`
    # sobre un naive lo interpretaría como hora local del servidor, no UTC.
    # Se trunca a segundo entero porque PyJWT también trunca `iat` a segundo
    # entero (`timegm(iat.utctimetuple())`): sin esto, un re-login del propio
    # Flujo B que cae en el mismo segundo de reloj que el corte (carrera real,
    # no solo de tests) generaría un `iat` truncado hacia abajo que compararía
    # como "anterior" a un `tokens_valid_after` con fracción de segundo, y el
    # backend rechazaría la sesión de origen que FR-017 exige preservar.
    cutover = int(user.tokens_valid_after.replace(tzinfo=timezone.utc).timestamp())
    if iat is not None and iat < cutover:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session revoked due to password change",
        )


_API_PREFIX = "/api/v1"

# Lista exhaustiva de (método HTTP, plantilla de ruta sin el prefijo `/api/v1`)
# a la que puede llegar un usuario con rol MESERO (spec 075, research.md D2/D3;
# lista autoritativa en specs/075-rol-mesero/contracts/backend-endpoint-access.md
# del repo pos-specs). Default-deny: cualquier combinación que no esté aquí se
# rechaza para MESERO, sin importar el router al que pertenezca — incluye
# endpoints nuevos que se agreguen en el futuro y no se agreguen explícitamente
# a esta lista.
_MESERO_ALLOWED_ROUTES: frozenset[tuple[str, str]] = frozenset({
    # Terminal de Mesas: table_sessions/router.py completo (sesiones de mesa,
    # staff — se consulta, se cobra y se cierra).
    ("GET", "/table-sessions"),
    ("GET", "/table-sessions/{table_session_id}"),
    ("POST", "/table-sessions/{table_session_id}/participants"),
    ("DELETE", "/table-sessions/{table_session_id}/participants/{participant_id}"),
    ("PUT", "/table-sessions/{table_session_id}/assignments"),
    ("GET", "/table-sessions/{table_session_id}/bill"),
    ("POST", "/table-sessions/{table_session_id}/close"),
    ("POST", "/table-sessions/{table_session_id}/release"),
    # Terminal de Mesas y Órdenes: subconjunto de orders/router.py (el resto
    # de ese router, configuración de mesas/QR, ya exige `require_tenant_admin`
    # y por lo tanto ya está bloqueado para MESERO sin necesidad de listarlo).
    ("GET", "/orders/tables"),
    ("PATCH", "/orders/tables/{table_id}/status"),
    ("POST", "/orders/{order_id}/move"),
    ("POST", "/orders/merge"),
    ("GET", "/orders/group/{group_id}/bill"),
    ("POST", "/orders/{order_id}/confirm"),
    ("GET", "/orders/{order_id}/payment-attempts"),
    ("POST", "/orders/payment-attempts/{attempt_id}/approve"),
    ("POST", "/orders/payment-attempts/{attempt_id}/reject"),
    ("POST", "/orders/payment-attempts/{attempt_id}/confirm-cash"),
    ("POST", "/orders/tables/{table_id}/consolidate"),
    ("POST", "/orders/tables/{table_id}/items"),
    ("PATCH", "/orders/items/{item_id}/kitchen"),
    ("POST", "/orders/{order_id}/ready"),
    ("POST", "/orders/items/{item_id}/void"),
    ("GET", "/orders/tables/{table_id}/bill"),
    ("GET", "/orders/{order_id}/checkout-preview"),
    ("POST", "/orders/draft-preview"),
    ("POST", "/orders/{order_id}/block"),
    ("POST", "/orders/{order_id}/pay"),
    ("POST", "/orders/{order_id}/checkout-and-send"),
    ("POST", "/orders/{order_id}/cancel"),
    ("POST", "/orders/tables/{table_id}/release"),
    ("POST", "/orders"),
    ("GET", "/orders"),
    ("GET", "/orders/{order_id}"),
    # Solo lectura de apoyo: datos que la Terminal de Mesas necesita de
    # routers que, por lo demás, quedan bloqueados para MESERO por completo.
    ("GET", "/promotions"),
    ("GET", "/sales/payment-methods"),
    ("GET", "/sales/{sale_id}"),
    ("GET", "/cash/shifts/current"),
    ("GET", "/invoices"),
    ("GET", "/invoices/{invoice_id}"),
    ("GET", "/tenant"),
    ("POST", "/realtime/ticket"),
})


def _enforce_mesero_scope(user: User, req: Request | None) -> None:
    """Bloqueo real del lado del servidor para el rol Mesero (spec 075,
    FR-007): default-deny contra `_MESERO_ALLOWED_ROUTES`. No hace nada para
    ningún otro rol — Admin y Cajero no cambian de comportamiento (FR-008).

    `req` puede ser `None` cuando esta función se invoca directamente sin
    FastAPI de por medio (mismo caso ya documentado en `get_current_user`);
    en ese caso no hay ruta que verificar y no se aplica ninguna restricción.
    """
    if req is None:
        return
    if not user.role or user.role.name != "MESERO":
        return

    route = req.scope.get("route")
    path = getattr(route, "path_format", None) or getattr(route, "path", None)
    if path is not None and path.startswith(_API_PREFIX):
        path = path[len(_API_PREFIX):]
    method = "GET" if req.method == "HEAD" else req.method

    if (method, path) not in _MESERO_ALLOWED_ROUTES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Fuera del alcance del rol Mesero",
        )


def get_current_user(
    token_data: dict = Depends(get_valid_token_data),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_tenant),
    req: Request = None,
) -> User:
    """`req` (spec 074, extensión de logging operativo) lo inyecta FastAPI solo
    por su anotación `Request` — no es un parámetro que ningún llamador vía
    `Depends` tenga que pasar. Tiene default `None` a propósito: hay código que
    invoca esta función directamente, sin FastAPI de por medio (p. ej.
    `app/characterization_tests/test_auth_session_revocation.py`), y ese
    contrato no cambia. Se usa para el efecto colateral de logging de abajo y,
    desde spec 075, también para el bloqueo de alcance del rol Mesero."""
    user = db.execute(
        select(User).where(
            User.email == token_data["user"]["email"],
            User.tenant_id == tenant.id,
            User.active == True,
        )
    ).scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    _reject_if_session_revoked(user, token_data)

    # spec 074 (research.md § 8): efecto colateral puro para
    # `OperationalLogMiddleware` — el actor ya está resuelto y validado aquí,
    # así que el middleware no repite ninguna lógica de autenticación. No
    # cambia qué devuelve esta función ni cuándo falla.
    if req is not None:
        req.state.actor_id = str(user.id)
        req.state.actor_type = "staff"

    # spec 075 (research.md D2): se evalúa después del efecto de logging de
    # arriba para que un intento bloqueado también quede en el log operativo
    # con su actor ya resuelto, no antes.
    _enforce_mesero_scope(user, req)

    return user


def get_shared_db():
    """Sesión sobre el schema compartido (shared). Para endpoints sin tenant
    (super admin), donde viven users/tenants/roles."""
    with with_db(None) as db:
        yield db


def get_optional_tenant(req: Request) -> Optional[Tenant]:
    """Gemelo de `get_tenant` (spec 100, research.md Decisión 2): misma
    validación de cabecera (`400` si falta `x-tenant-host`) y misma consulta
    (`find_tenant_by_host`), pero devuelve `None` en vez de levantar `404`
    cuando el host no resuelve a ningún `Tenant` — lo necesita el arranque
    agregado del panel para tolerar al Super Admin (FR-007)."""
    tenant_host = req.headers.get("x-tenant-host")
    if not tenant_host:
        raise HTTPException(
            status_code=400, detail="Falta la cabecera 'x-tenant-host'"
        )
    host_without_port = tenant_host.split(":", 1)[0]

    tenant = find_tenant_by_host(host_without_port)
    if tenant is not None:
        req.state.tenant_id = tenant.id

    return tenant


def get_optional_db(tenant: Optional[Tenant] = Depends(get_optional_tenant)) -> Session:
    """Espejo de `get_db` (`db.py`), tolerante a `tenant=None` (Super Admin):
    sin tenant resuelto, abre la sesión sobre el schema por defecto en vez de
    fallar."""
    with with_db(tenant.schema if tenant else None) as db:
        yield db


def get_authenticated_user(
    token_data: dict = Depends(get_valid_token_data),
    db: Session = Depends(get_shared_db),
) -> User:
    """Usuario autenticado por JWT contra el schema shared. Vale para super admin
    (tenant_id NULL) y usuarios de tenant, sin necesitar x-tenant-host."""
    uid = (token_data.get("user") or {}).get("uid")
    try:
        user_id = uuid.UUID(str(uid))
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )

    user = db.execute(
        select(User).where(User.id == user_id, User.active == True)
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    _reject_if_session_revoked(user, token_data)

    return user


def get_current_super_admin(
    request: Request,
    token_data: dict = Depends(get_valid_token_data),
    db: Session = Depends(get_shared_db),
) -> User:
    """Autentica al super admin global por JWT (sin requerir x-tenant-host)."""
    payload = token_data.get("user") or {}
    if not payload.get("is_super_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super admin access required",
        )

    user = db.execute(
        select(User).where(
            User.email == payload.get("email"),
            User.tenant_id.is_(None),
            User.active == True,
        )
    ).scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Super admin not found or inactive",
        )

    # Contexto para observabilidad (spec 068): el middleware de errores de
    # super-admin lo usa para etiquetar los eventos de Sentry con quién
    # originó la solicitud. Aditivo: no cambia el valor de retorno ni los
    # `raise` de arriba.
    request.state.super_admin_id = user.id

    return user


def require_tenant_admin(user: User = Depends(get_current_user)) -> User:
    """Exige que el usuario autenticado del tenant tenga rol ADMIN."""
    if not user.role or user.role.name != "ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant admin access required",
        )
    return user


def require_tenant_admin_or_cashier(user: User = Depends(get_current_user)) -> User:
    """Exige que el usuario autenticado tenga rol ADMIN o CASHIER (spec 093,
    research.md D1/D2): el interruptor "Agotado" del catálogo es la única acción de
    escritura sobre `Product` que un Cajero puede alcanzar -- todo lo demás
    (crear/editar/desactivar producto) sigue exclusivo de `require_tenant_admin`."""
    if not user.role or user.role.name not in {"ADMIN", "CASHIER"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso de administrador o cajero requerido",
        )
    return user


def forbid_cashier(user: User = Depends(get_current_user)) -> User:
    """Bloquea solo a CASHIER (spec 093, FR-025/escenario 12, research.md D8): la
    receta (BOM) de una variante expone `inventory_item_id`, dato de inventario sin
    ningún propósito en la toma de pedidos. No toca a ningún otro rol -- en
    particular, no restringe `/variants/{id}/option-groups`, que el Cajero ya usa
    hoy desde la Terminal de mesas para ofrecer opciones/adicionales."""
    if user.role and user.role.name == "CASHIER":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para consultar la receta de este producto",
        )
    return user
