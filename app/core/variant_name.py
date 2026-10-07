"""Regla del nombre propio de una variante de producto (spec 099).

Definición única en `specs/099-nombre-tamano-variante/contracts/variant-name-rules.md`; el
frontend la replica en `shared/validators/variant-name.validator.ts` y ambas suites
recorren los mismos vectores. Sin dependencias nuevas: solo `re`.
"""
import re

VARIANT_NAME_LENGTH_MESSAGE = "El nombre admite hasta 60 caracteres"

VARIANT_NAME_MAX_LENGTH = 60

_WHITESPACE = re.compile(r"\s+")


def normalize_variant_name(value: str | None) -> str:
    """Recorta, colapsa espacios internos y valida la longitud máxima. Devuelve el
    valor a guardar (puede ser `""`, nunca `None`) o lanza `ValueError` si la entrada,
    tal como se escribió (antes de recortar), supera los 60 caracteres."""
    raw = value or ""

    if len(raw) > VARIANT_NAME_MAX_LENGTH:
        raise ValueError(VARIANT_NAME_LENGTH_MESSAGE)

    return _WHITESPACE.sub(" ", raw.strip())
