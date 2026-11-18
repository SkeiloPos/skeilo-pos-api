"""Sustitución literal de variables en la plantilla legal maestra (spec 162,
FR-008/FR-009/FR-011, research.md Decisión 3). Sin motor de plantillas: solo
`re.sub` contra una lista cerrada de nombres, para que ningún placeholder
pueda evaluarse como expresión."""
import hashlib
import html
import re

ALLOWED_TEMPLATE_VARIABLES = {
    "nombre_negocio",
    "identificacion_legal",
    "direccion_legal",
    "ciudad",
    "correo_contacto",
    "telefono_contacto",
}

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def validate_variables(contenido_markdown: str) -> list[str]:
    """Nombres de placeholder presentes en `contenido_markdown` que NO están
    en `ALLOWED_TEMPLATE_VARIABLES` (FR-009), en orden de aparición, sin
    duplicados."""
    vistos: list[str] = []
    for nombre in _PLACEHOLDER_RE.findall(contenido_markdown):
        if nombre not in ALLOWED_TEMPLATE_VARIABLES and nombre not in vistos:
            vistos.append(nombre)
    return vistos


def substitute_variables(contenido_markdown: str, values: dict[str, object]) -> str:
    """Sustituye cada `{{variable}}` presente en `values` por su valor,
    escapado (FR-011: "sustitución literal de texto, sin evaluar código ni
    expresiones"). Un placeholder sin valor en `values` (no debería ocurrir
    tras `validate_variables` + completitud del perfil) se deja tal cual."""

    def _replace(match: re.Match) -> str:
        nombre = match.group(1)
        if nombre not in values or values[nombre] is None:
            return match.group(0)
        return html.escape(str(values[nombre]))

    return _PLACEHOLDER_RE.sub(_replace, contenido_markdown)


def content_hash(content: str) -> str:
    """Huella de verificación de contenido (`content_hash`, research.md
    Decisión 4): blake2b-128 hex de los bytes UTF-8, igual algoritmo que
    `http_cache.py::weak_etag` pero sin el envoltorio `W/"..."` de ETag —
    este valor se persiste en columna propia y también se usa directamente
    como `ETag` en los endpoints públicos."""
    return hashlib.blake2b(content.encode("utf-8"), digest_size=16).hexdigest()
