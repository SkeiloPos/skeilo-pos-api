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
