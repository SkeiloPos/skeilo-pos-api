"""Paquete de plantillas de correo transaccional de SkeiloPOS (spec 096).

Superficie pública (`contracts/constructores-de-correo.md`): `EmailContent`,
`ErrorDePlantillaDeCorreo` y los tres constructores. El transporte
(`app/core/mail.py`) y los disparadores importan únicamente de aquí, nunca
de los módulos internos (`_marca`, `_layout`, `_campos`).
"""
from dataclasses import dataclass

from ._campos import ErrorDePlantillaDeCorreo

__all__ = [
    "EmailContent",
    "ErrorDePlantillaDeCorreo",
    "construir_correo_invitacion",
    "construir_correo_restablecimiento",
    "construir_correo_contrasena_cambiada",
]


@dataclass(frozen=True)
class EmailContent:
    """Resultado de construir un correo (data-model.md Entidad 1). Objeto
    de valor inmutable, de un solo uso: sin estado, sin comportamiento."""

    subject: str
    html_body: str
    text_body: str


# Los tres módulos de correo construyen su `EmailContent` importándolo de
# este mismo paquete (`from app.core.mail_templates import EmailContent`):
# funciona porque la clase ya quedó definida arriba antes de que Python
# ejecute estos imports, aunque la inicialización del paquete no haya
# terminado.
from .invitacion import construir_correo_invitacion  # noqa: E402
from .restablecimiento import construir_correo_restablecimiento  # noqa: E402
from .contrasena_cambiada import construir_correo_contrasena_cambiada  # noqa: E402
