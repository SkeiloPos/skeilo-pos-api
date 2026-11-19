from fastapi import HTTPException, status


class TenantNotFoundError(Exception):
    """Se lanza cuando no se encuentra el tenant."""

    def __init__(self, host: str):
        self.host = host
        super().__init__(f"Tenant not found for host '{host}'")


class InsufficientStockError(HTTPException):
    """Un insumo no tiene stock suficiente para el consumo pedido.

    Es HTTPException(400) para que FastAPI la traduzca sola y para que los
    `except HTTPException` que envuelven la transacción hagan rollback y re-lancen.

    `message` se usa **tal cual** como `detail`: es texto que el front muestra al
    usuario. Antes la clase lo envolvía en su propia plantilla, así que pasarle una
    frase ya formada producía un mensaje anidado e ilegible
    ("Stock insuficiente o vencido para el insumo 'Stock insuficiente para …'").
    """

    def __init__(self, message: str, *, item_name: str | None = None):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=message,
        )
        # spec 089 (D18): nombre del insumo que se agotó, para que quien agrega una línea
        # pueda decir qué PRODUCTO falla sin parsear `detail`. `None` si no se conoce.
        self.item_name = item_name


class ModuleNotInPlanError(HTTPException):
    """El plan vigente del tenant no incluye el módulo que pide la operación (spec 141, A-151).

    Es `HTTPException(403)` para que los `except HTTPException` existentes la sigan atrapando y para
    conservar la semántica de siempre. `detail` sigue siendo **siempre un `str`** (FR-007): es el texto que
    los clientes ya leen hoy. `code` y `module` viajan aparte en el cuerpo (ver `register_module_error_handler`
    en `app/core/error_middleware.py`) para que el front distinga este 403 de los de rol, límite o plan vencido.
    """

    code = "MODULE_NOT_IN_PLAN"

    def __init__(self, module: str, label: str):
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Tu plan actual no incluye el módulo de {label}.",
        )
        self.module = module


class SubscriptionBlockedError(HTTPException):
    """La suscripción del tenant está `bloqueada`: pasaron 120 h desde `plan_vence_en` sin un pago aprobado
    (spec 161, FR-006/FR-007, A-161/A-162).

    Es `HTTPException(403)` para que los `except HTTPException` existentes la sigan atrapando, y `detail`
    sigue siendo **siempre un `str`**, con el mismo texto de siempre del 403 por plan vencido (spec 033):
    solo gana `code`, que viaja aparte en el cuerpo (ver `register_subscription_error_handlers` en
    `app/core/error_middleware.py`) para que el front distinga este rechazo del de módulo fuera del plan
    (`MODULE_NOT_IN_PLAN`), de los de rol y de los de límite de recursos.

    El `code` va en minúsculas porque así lo fija la spec 161 (D1); `MODULE_NOT_IN_PLAN` va en mayúsculas.
    """

    code = "subscription_blocked"

    def __init__(self):
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tu plan venció. Debe renovarse para seguir usando el sistema.",
        )


#: Por qué un tenant no puede pedir un link de pago (spec 161, FR-012/FR-013, contrato `subscription-api.md § 2`).
#: Texto en español de Colombia, pensado para mostrarse tal cual al ADMIN.
SUBSCRIPTION_NOT_PAYABLE_REASONS = {
    # Ciclo en curso ya pagado: un solo período abierto, nunca se pagan meses por adelantado (D5).
    "already_active": "Tu suscripción está al día; podrás pagar la siguiente cuando falten 48 horas para vencer.",
    "no_expiry": "Tu plan no tiene vencimiento; no requiere pago.",
    "unsupported_cycle": "Tu plan se renueva con la plataforma; contacta a soporte.",
    "no_price": "Tu plan no tiene un precio mensual definido; contacta a soporte.",
}


class SubscriptionNotPayableError(HTTPException):
    """El tenant no puede pedir un link de pago por este flujo (spec 161, FR-012/FR-013): conflicto controlado.

    `reason` ∈ `SUBSCRIPTION_NOT_PAYABLE_REASONS` viaja aparte en el cuerpo junto con `code`, y `detail` sigue
    siendo siempre un `str`. Una razón desconocida es un error de programación y se rechaza al construir.
    """

    code = "subscription_not_payable"

    def __init__(self, reason: str):
        if reason not in SUBSCRIPTION_NOT_PAYABLE_REASONS:
            raise ValueError(f"reason inválida: {reason!r}")
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail=SUBSCRIPTION_NOT_PAYABLE_REASONS[reason],
        )
        self.reason = reason


class PaymentGatewayNotConfiguredError(HTTPException):
    """Faltan las credenciales de Wompi en la configuración del entorno (spec 161, FR-017): sin ellas no se
    puede firmar un link. Es un 503 controlado, nunca un 500, y se lanza **antes** de escribir nada. El
    mensaje no revela qué variable falta."""

    code = "payment_gateway_not_configured"

    def __init__(self):
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El pago en línea no está disponible por ahora. Intenta más tarde o contacta a soporte.",
        )


class PendingPaymentExit(HTTPException):
    """Un comensal intenta salir del Menú QR con un pago propio sin confirmar (spec 130, A-139).

    `leave_session`/`POST /cart/leave` deja de ser infalible: si el participante tiene al menos
    un pedido propio no cancelado con el pago pendiente, la salida se bloquea con `409` en vez de
    cerrar la sesión.
    """

    def __init__(self):
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "pago_pendiente",
                "message": (
                    "Tienes un pago pendiente. Para salir, dirígete a la caja. Si no deseas "
                    "llevar tu pedido, indícalo allí."
                ),
            },
        )


class InvalidToken(Exception):
    """User has provided an invalid or expired token"""

    pass


class RevokedToken(Exception):
    """User has provided a token that has been revoked"""

    pass


class AccessTokenRequired(Exception):
    """User has provided a refresh token when an access token is needed"""

    pass


class RefreshTokenRequired(Exception):
    """User has provided an access token when a refresh token is needed"""

    pass


class UserNotFound(Exception):
    """User Not found"""

    pass
