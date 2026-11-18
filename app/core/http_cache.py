"""Revalidación condicional (ETag / 304) para los endpoints que se sondean.

El 95 % de los sondeos devuelven exactamente lo mismo. Un 304 no ahorra las
queries —esas ya se hicieron— pero sí la serialización del cuerpo, el ancho de
banda y el re-render del cliente.

El ETag se calcula **sobre los bytes del JSON ya serializado**, no sobre un
fingerprint de columnas (`MAX(updated_at) + COUNT(*)` y similares). Un
fingerprint ahorraría además la serialización, pero basta olvidar una columna
mutable —`estado_cocina`, justo la que cambia todo el rato— para servir un 304
mentiroso y dejar la pantalla de cocina congelada. Hashear el cuerpo garantiza
por construcción que *mismo ETag ⟺ mismos bytes*.

El cliente no hace nada: se emite `ETag` + `Cache-Control: no-cache` y el
navegador manda `If-None-Match` solo en la siguiente petición.
"""
from __future__ import annotations

import hashlib
from typing import Any

from fastapi import Request, Response
from pydantic import TypeAdapter

# Débil (`W/`) porque la igualdad es semántica, no byte-a-byte a nivel de
# transporte: nada aquí depende de rangos ni de comparación fuerte.
_PREFIX = "W/"


def weak_etag(body: bytes) -> str:
    """ETag débil del cuerpo. blake2b-128 basta: no es un hash criptográfico
    contra un adversario, solo tiene que no colisionar por accidente."""
    return f'{_PREFIX}"{hashlib.blake2b(body, digest_size=16).hexdigest()}"'


def _matches(if_none_match: str | None, etag: str) -> bool:
    """¿El cliente ya tiene esta versión?

    `If-None-Match` puede traer varios valores separados por coma, y el comodín
    `*`. La comparación es débil: se ignora el prefijo `W/` de ambos lados, como
    manda el RFC 9110 §8.8.3.2 para peticiones condicionales que no son de rango.
    """
    if not if_none_match:
        return False
    objetivo = etag.removeprefix(_PREFIX)
    for candidato in if_none_match.split(","):
        candidato = candidato.strip()
        if candidato == "*" or candidato.removeprefix(_PREFIX) == objetivo:
            return True
    return False


def json_or_304(request: Request, adapter: TypeAdapter[Any], data: Any) -> Response:
    """Serializa `data` con `adapter` y responde 200 con `ETag`, o 304 si el
    cliente ya tiene esa versión.

    `adapter` debe construirse **a nivel de módulo**: `TypeAdapter` compila el
    esquema, y hacerlo por request tira por tierra el ahorro que se persigue.
    """
    body = adapter.dump_json(data)
    etag = weak_etag(body)
    # `no-cache` no significa "no cachees": significa "cachea, pero revalida
    # siempre antes de usarlo". Es exactamente lo que queremos.
    headers = {"ETag": etag, "Cache-Control": "no-cache"}

    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)

    return Response(content=body, media_type="application/json", headers=headers)


def json_or_304_with_hash(
    request: Request, adapter: TypeAdapter[Any], data: Any, content_hash: str
) -> Response:
    """Variante de `json_or_304` para los documentos legales (spec 162,
    research.md Decisión 4): el `ETag` es directamente `content_hash` — ya
    persistido en columna propia, calculado sobre el contenido (Markdown),
    no sobre el cuerpo JSON servido — en vez de un hash del cuerpo. Dos
    respuestas con distinto `contenido_html` (p. ej. una futura mejora de
    render) pero el mismo `content_hash` siguen comparando igual, que es la
    semántica que pide la spec ("marca de verificación de contenido")."""
    etag = f'{_PREFIX}"{content_hash}"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}

    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)

    body = adapter.dump_json(data)
    return Response(content=body, media_type="application/json", headers=headers)
