"""Validación, escape y formato de fecha compartidos por las tres
plantillas (spec 096, FR-040, FR-041, FR-042, FR-036).

Es el único módulo por el que pasa todo valor dinámico antes de insertarse
en un cuerpo de correo. A-119 ocurrió porque el escape era una convención
que había que recordar en cada interpolación, no una estructura — este
módulo convierte esa convención en la única puerta de entrada.
"""
import html
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.core.timezone import ZONAS_CURADAS, etiqueta_zona_correo

#: Zona de respaldo: usuario sin tenant (p. ej. Super Admin) y el envoltorio de Bogotá.
ZONA_RESPALDO = "America/Bogota"


class ErrorDePlantillaDeCorreo(ValueError):
    """Falta un valor obligatorio de la plantilla (FR-041).

    El mensaje nombra los campos faltantes y nunca incluye ningún valor:
    la contraseña temporal no puede llegar a un registro por esta vía
    (FR-042).
    """


def validar_campos_obligatorios(**campos: object) -> None:
    """Un campo falla si es `None`, cadena vacía o solo espacios; para un
    `int` (p. ej. `minutos_de_vigencia`), si no es mayor que 0. Lanza
    `ErrorDePlantillaDeCorreo` con los nombres de los campos faltantes y
    nada más — nunca compone un cuerpo parcial."""
    faltantes = []
    for nombre, valor in campos.items():
        if isinstance(valor, int) and not isinstance(valor, bool):
            if valor <= 0:
                faltantes.append(nombre)
        elif valor is None or not str(valor).strip():
            faltantes.append(nombre)
    if faltantes:
        raise ErrorDePlantillaDeCorreo(
            "Faltan campos obligatorios: " + ", ".join(faltantes)
        )


def escapar_valores(**valores: str) -> dict[str, str]:
    """Devuelve un diccionario nuevo con cada valor pasado por
    `html.escape(valor, quote=True)`. El cuerpo HTML interpola
    exclusivamente de este diccionario; el texto plano interpola del
    crudo que el llamante ya tiene (research.md Decisión 3) — ninguna
    plantilla llama a `html.escape` directamente."""
    return {nombre: html.escape(valor, quote=True) for nombre, valor in valores.items()}


def formatear_fecha_hora(momento: datetime, zona: str) -> str:
    """FR-036 y spec 138 (FR-011): `dd/mm/aaaa · hh:mm a. m./p. m. (hora de {zona})`, hora de
    12 con cero inicial, construido a mano sin `%p` ni `setlocale`
    (research.md Decisión 7) — `%p` depende del locale del proceso y
    produciría "PM" en vez de "p. m." fuera de un locale español.

    `zona` es la IANA del tenant. El paréntesis final: una zona de la lista curada usa su nombre
    («hora de Colombia», «hora de Ciudad de México»…); una zona válida fuera de la lista usa su
    desfase sin «hora de» («UTC−3», «UTC+5:30»).

    `momento` se interpreta siempre como UTC: si llega *naive* (el caso
    real de los dos llamantes) se le fija `timezone.utc` antes de
    convertir. Nunca se usa `astimezone()` sobre el valor naive tal
    cual — eso lo interpretaría como hora local del host, no como UTC
    (la trampa que data-model.md señala)."""
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    local = momento.astimezone(ZoneInfo(zona))
    hora_12 = local.hour % 12 or 12
    sufijo = "a. m." if local.hour < 12 else "p. m."
    fecha = local.strftime("%d/%m/%Y")
    minutos = local.strftime("%M")
    etiqueta = etiqueta_zona_correo(zona, momento)
    es_nombre = any(curada.iana == zona for curada in ZONAS_CURADAS)
    parentesis = f"(hora de {etiqueta})" if es_nombre else f"({etiqueta})"
    return f"{fecha} · {hora_12:02d}:{minutos} {sufijo} {parentesis}"


def formatear_fecha_hora_colombia(momento: datetime) -> str:
    """Envoltorio de Bogotá: devuelve EXACTAMENTE el texto de siempre
    («… (hora de Colombia)»). Se conserva porque tres archivos de caracterización de
    correo lo llaman y fijan su salida (Principio III); el correo real usa `formatear_fecha_hora`
    con la zona del tenant."""
    return formatear_fecha_hora(momento, ZONA_RESPALDO)
