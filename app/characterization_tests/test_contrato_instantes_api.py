"""Spec 138, Historia 1 y 6 (FR-001, FR-002, FR-003, SC-008) — todo instante que devuelve la API lleva
offset UTC explícito.

Un `datetime` plano de Pydantic sale en el OpenAPI como `format: date-time` y viaja como
`2026-10-25T17:00:00` SIN offset: el navegador lo lee como hora LOCAL (en Bogotá, 5 h de error). El tipo
`UtcDatetime` (`app.core.timezone`) lo serializa como `2026-10-25T17:00:00+00:00` y en el OpenAPI sale como
`string` sin `format`. Esta prueba recorre el OpenAPI REAL de la aplicación y falla nombrando cada
`Esquema.campo` de una respuesta que siga siendo un `datetime` plano, para que el defecto no vuelva a colarse
(contracts/instantes-api.md §4).

**Detecta**: todo `datetime` de una respuesta con `response_model` (anidados, de `Page[T]` y heredados).
**No detecta**: respuestas `dict`/`JSONResponse` sin `response_model` (auditadas a mano; ninguna con instantes
sin offset).

    python -m unittest app.characterization_tests.test_contrato_instantes_api -v
"""
import unittest
from datetime import datetime
from unittest import mock

from fastapi import FastAPI
from pydantic import BaseModel

from app.characterization_tests import fixtures  # noqa: F401  - variables de entorno inertes antes de importar app.main
from app.core.timezone import UtcDatetime

#: Única excepción (FR-002): hora de pared local del tenant, no un instante absoluto. Agregar una exige citar la
#: decisión que la autoriza; si una entrada deja de existir, la prueba falla (para que la lista no se pudra).
INSTANTES_SIN_OFFSET_PERMITIDOS = {
    # Vigencia de la promoción: «desde el 25/10 a las 00:00 en la zona del negocio». Se guarda y se devuelve como
    # hora local del tenant sin offset a propósito (spec 138, FR-002; data-model.md §2).
    ("PromotionResponse", "starts_at"),
    ("PromotionResponse", "ends_at"),
}


def _refs(nodo, acumulado: set[str]) -> None:
    if isinstance(nodo, dict):
        for clave, valor in nodo.items():
            if clave == "$ref":
                acumulado.add(valor.rsplit("/", 1)[-1])
            else:
                _refs(valor, acumulado)
    elif isinstance(nodo, list):
        for valor in nodo:
            _refs(valor, acumulado)


def _tiene_date_time(nodo) -> bool:
    """¿Algún nodo (incluidos `anyOf`/`oneOf`/`allOf`/`items`) declara `format: date-time`?"""
    if isinstance(nodo, dict):
        if nodo.get("format") == "date-time":
            return True
        return any(_tiene_date_time(v) for v in nodo.values())
    if isinstance(nodo, list):
        return any(_tiene_date_time(v) for v in nodo)
    return False


def instantes_planos_en_respuestas(openapi: dict) -> set[tuple[str, str]]:
    """`{(Esquema, campo)}` de los esquemas alcanzables desde las RESPUESTAS con un `datetime` plano."""
    esquemas = openapi.get("components", {}).get("schemas", {})
    pendientes: set[str] = set()
    for ruta in openapi.get("paths", {}).values():
        for operacion in ruta.values():
            if isinstance(operacion, dict):
                for respuesta in operacion.get("responses", {}).values():
                    _refs(respuesta, pendientes)

    alcanzables: set[str] = set()
    while pendientes:
        nombre = pendientes.pop()
        if nombre in alcanzables or nombre not in esquemas:
            continue
        alcanzables.add(nombre)
        sub: set[str] = set()
        _refs(esquemas[nombre], sub)
        pendientes |= sub

    return {
        (nombre, campo)
        for nombre in alcanzables
        for campo, definicion in esquemas[nombre].get("properties", {}).items()
        if _tiene_date_time(definicion)
    }


def _openapi_de_la_aplicacion() -> dict:
    # `app.main` ejecuta `create_app()` al importarse y esta llama a `initialize_database()`, que abre
    # PostgreSQL y migra. Se sustituye por una función vacía ANTES de importar.
    with mock.patch("app.core.db.initialize_database", lambda *a, **k: None):
        from app.main import app
    return app.openapi()


class TestContratoDeInstantes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.encontrados = instantes_planos_en_respuestas(_openapi_de_la_aplicacion())

    def test_ningun_instante_de_una_respuesta_viaja_sin_offset(self):
        sobrantes = sorted(self.encontrados - INSTANTES_SIN_OFFSET_PERMITIDOS)
        self.assertEqual(
            sobrantes,
            [],
            "Instantes sin offset en respuestas de la API (FR-001): cambiar `datetime` por `UtcDatetime` "
            "(app.core.timezone) en:\n" + "\n".join(f"  {esquema}.{campo}" for esquema, campo in sobrantes),
        )

    def test_la_lista_de_excepciones_no_tiene_entradas_obsoletas(self):
        obsoletas = sorted(INSTANTES_SIN_OFFSET_PERMITIDOS - self.encontrados)
        self.assertEqual(
            obsoletas,
            [],
            "Entradas de INSTANTES_SIN_OFFSET_PERMITIDOS que ya no existen en el OpenAPI (quitarlas): "
            + ", ".join(f"{e}.{c}" for e, c in obsoletas),
        )

    def test_la_aplicacion_expone_respuestas_con_instantes(self):
        # Sin esto, un detector roto (que no recorre nada) pasaría en verde.
        self.assertIn(("PromotionResponse", "starts_at"), self.encontrados)


class TestSensibilidadDelDetector(unittest.TestCase):
    """US6-1: el detector discrimina. Un campo `datetime` plano aparece; con `UtcDatetime` no."""

    @staticmethod
    def _openapi_con(modelo) -> dict:
        mini = FastAPI()

        @mini.get("/x", response_model=modelo)
        def _():  # pragma: no cover - solo se genera el OpenAPI
            return {}

        return mini.openapi()

    def test_un_datetime_plano_aparece_en_el_conjunto(self):
        class ConDatetime(BaseModel):
            created_at: datetime
            opcional: datetime | None = None

        self.assertEqual(
            instantes_planos_en_respuestas(self._openapi_con(ConDatetime)),
            {("ConDatetime", "created_at"), ("ConDatetime", "opcional")},
        )

    def test_con_utcdatetime_no_aparece(self):
        class ConUtc(BaseModel):
            created_at: UtcDatetime
            opcional: UtcDatetime | None = None

        self.assertEqual(instantes_planos_en_respuestas(self._openapi_con(ConUtc)), set())

    def test_un_esquema_solo_de_entrada_no_cuenta(self):
        class Entrada(BaseModel):
            starts_at: datetime

        class Salida(BaseModel):
            ok: bool

        mini = FastAPI()

        @mini.post("/x", response_model=Salida)
        def _(cuerpo: Entrada):  # pragma: no cover
            return {"ok": True}

        self.assertEqual(instantes_planos_en_respuestas(mini.openapi()), set())

    def test_un_datetime_anidado_dentro_de_una_pagina_se_detecta(self):
        class Fila(BaseModel):
            sent_at: datetime

        class Pagina(BaseModel):
            items: list[Fila]

        self.assertEqual(
            instantes_planos_en_respuestas(self._openapi_con(Pagina)), {("Fila", "sent_at")}
        )


if __name__ == "__main__":
    unittest.main()
