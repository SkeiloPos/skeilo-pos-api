import logging
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone
from itsdangerous import URLSafeTimedSerializer

import bcrypt
import jwt

from app.core.config import settings


def generate_passwd_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")


def generate_random_password(length: int = 12) -> str:
    """Genera una contraseña aleatoria segura (para credenciales emitidas por el sistema)."""
    alphabet = string.ascii_letters + string.digits + "!@#$%*?"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def verify_password(password: str, hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8")[:72], hash.encode("utf-8"))


def create_access_token(
    user_data: dict, expiry: timedelta = None, refresh: bool = False, sid: str | None = None
):
    """Acuña un JWT de usuario.

    Con `refresh=True` el token vive `REFRESH_TOKEN_EXPIRY_MINUTES` en vez de
    `ACCESS_TOKEN_EXPIRY`: si ambos duraran lo mismo, el refresh caducaría junto
    al access y `/auth/refresh-token` nunca podría renovar nada.

    `sid` (spec 159, research.md Decisión 5) es el id de la fila
    `shared.user_sessions` asociada a este login -- distinto del `jti` propio
    de cada token (access y refresh tienen cada uno su propio `jti`, pero
    comparten el mismo `sid` cuando se acuñan juntos en el mismo login).
    Opcional para no romper ningún llamador existente que no lo pase.
    """
    if expiry is None:
        minutes = (
            settings.REFRESH_TOKEN_EXPIRY_MINUTES
            if refresh
            else settings.ACCESS_TOKEN_EXPIRY
        )
        expiry = timedelta(minutes=minutes)

    now = datetime.now(timezone.utc)

    payload = {}

    payload["user"] = user_data
    payload["iat"] = now
    payload["exp"] = now + expiry
    payload["jti"] = str(uuid.uuid4())

    payload["refresh"] = refresh

    if sid is not None:
        payload["sid"] = sid

    token = jwt.encode(
        payload=payload, key=settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM
    )

    return token


def decode_token(token: str) -> dict:
    try:
        token_data = jwt.decode(
            jwt=token, key=settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )

        return token_data

    except jwt.PyJWTError as e:
        logging.exception(e)
        return None

serializer = URLSafeTimedSerializer(
    secret_key=settings.JWT_SECRET, salt="email-configuration"
)

def create_url_safe_token(data: dict):

    token = serializer.dumps(data)

    return token

def decode_url_safe_token(token:str):
    try:
        token_data = serializer.loads(token)

        return token_data
    
    except Exception as e:
        logging.error(str(e))
        