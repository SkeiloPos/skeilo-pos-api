"""Spec 138, Historia 4 (FR-005) — los instantes se escriben de UNA sola forma.

Columna `DateTime` sin zona (UTC por convención) => `utc_now_naive()`. Columna con zona
(`promotion_evaluated_at`) => un `datetime` aware (`utc_now()`). Prohibido armar a mano:

  (a) `X.replace(tzinfo=None)` cuando `X` es `utc_now()` o `datetime.now(<…>.utc)`;
  (b) `datetime.utcnow()` (obsoleto y devuelve un naive que parece local);
  (c) `datetime.now()` sin argumentos (hora local de la máquina);
  (d) asignar un `utc_now()` aware a un atributo (`objeto.atributo = utc_now()`): casi siempre
      es una columna sin zona; si de verdad lo fuera de una con zona, usar `datetime.now(timezone.utc)`
      o el instante congelado, que no lo cuestiona.

Esta prueba recorre el código de producción con `ast` (no con una búsqueda de texto: un
comentario que cite `.replace(tzinfo=None)` no la dispara).

**Permitido** (hora de pared local o conversión de un instante, no «ahora» en UTC):
`.astimezone(...).replace(tzinfo=None)` y `datetime.now(tz).replace(tzinfo=None)` con la zona de
un tenant (`scheduler.py`, `promotions/service.py`, `cash/reports.py`).

**Límite documentado**: no detecta argumentos con nombre ni valores que pasan antes por una
variable local ni condicionales —p. ej. `last_cost_at=utc_now() if … else None` en
`supplier_items.py`, que se corrigió a mano—, porque `now=utc_now()` hacia
`promotion_evaluation_instant` es un uso aware legítimo y no se distingue sin conocer la columna.

    python -m unittest app.characterization_tests.test_instantes_forma_unica -v
"""
import ast
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.core.timezone import utc_now, utc_now_naive

_APP = Path(__file__).resolve().parents[1]


def _es_utc(expr: ast.expr) -> bool:
    """`timezone.utc`, `dt_timezone.utc`, `datetime.timezone.utc` o un nombre `UTC`/`utc`."""
    if isinstance(expr, ast.Attribute):
        return expr.attr == "utc"
    return isinstance(expr, ast.Name) and expr.id in ("UTC", "utc")


def _es_clase_datetime(expr: ast.expr) -> bool:
    """`datetime` o `<módulo>.datetime`."""
    if isinstance(expr, ast.Name):
        return expr.id == "datetime"
    return isinstance(expr, ast.Attribute) and expr.attr == "datetime"


def _es_llamada_utc_now(expr: ast.expr) -> bool:
    return isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == "utc_now"


def _es_ahora_utc(expr: ast.expr) -> bool:
    """`utc_now()` o `datetime.now(<…>.utc)`: «ahora» en UTC con zona, listo para volverse naive a mano."""
    if _es_llamada_utc_now(expr):
        return True
    return (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "now"
        and _es_clase_datetime(expr.func.value)
        and bool(expr.args)
        and _es_utc(expr.args[0])
    )


def hallazgos_en_codigo(fuente: str) -> list[tuple[int, str]]:
    """(línea, regla) de cada escritura de instantes que incumple FR-005 en `fuente`."""
    encontrados: list[tuple[int, str]] = []

    class Visitante(ast.NodeVisitor):
        def visit_Call(self, nodo: ast.Call):
            f = nodo.func
            if isinstance(f, ast.Attribute):
                quita_zona = f.attr == "replace" and any(
                    kw.arg == "tzinfo" and isinstance(kw.value, ast.Constant) and kw.value.value is None
                    for kw in nodo.keywords
                )
                if quita_zona and _es_ahora_utc(f.value):
                    encontrados.append((nodo.lineno, "a: .replace(tzinfo=None) sobre «ahora» en UTC"))
                if f.attr == "utcnow":
                    encontrados.append((nodo.lineno, "b: datetime.utcnow()"))
                if f.attr == "now" and _es_clase_datetime(f.value) and not nodo.args and not nodo.keywords:
                    encontrados.append((nodo.lineno, "c: datetime.now() sin zona"))
            self.generic_visit(nodo)

        def _asignacion(self, objetivos, valor, linea):
            if _es_llamada_utc_now(valor) and any(isinstance(o, ast.Attribute) for o in objetivos):
                encontrados.append((linea, "d: utc_now() aware asignado a un atributo"))

        def visit_Assign(self, nodo: ast.Assign):
            self._asignacion(nodo.targets, nodo.value, nodo.lineno)
            self.generic_visit(nodo)

        def visit_AnnAssign(self, nodo: ast.AnnAssign):
            if nodo.value is not None:
                self._asignacion([nodo.target], nodo.value, nodo.lineno)
            self.generic_visit(nodo)

    Visitante().visit(ast.parse(fuente))
    return sorted(encontrados)


def _archivos_de_produccion():
    for ruta in sorted(_APP.rglob("*.py")):
        relativa = ruta.relative_to(_APP)
        if "characterization_tests" in relativa.parts:
            continue
        if relativa.parts[0] == "scripts" and relativa.name.startswith("test_"):
            continue
        if relativa.as_posix() == "core/timezone.py":
            continue
        yield ruta, relativa


class TestUtcNowNaive(unittest.TestCase):
    def test_no_lleva_tzinfo(self):
        self.assertIsNone(utc_now_naive().tzinfo)

    def test_es_el_utc_actual(self):
        diferencia = abs((utc_now_naive() - utc_now().replace(tzinfo=None)).total_seconds())
        self.assertLess(diferencia, 1)

    def test_no_depende_de_la_zona_de_la_maquina(self):
        # Si usara `datetime.now()` a secas, bajo TZ=Asia/Tokyo se adelantaría 9 horas.
        referencia = datetime.now(timezone.utc).replace(tzinfo=None)
        self.assertLess(abs((utc_now_naive() - referencia).total_seconds()), 1)


class TestSensibilidadDeLaGuarda(unittest.TestCase):
    """La guarda detecta lo que dice detectar y deja pasar lo permitido (si no, no protege nada)."""

    def reglas(self, fuente: str) -> list[str]:
        return [regla[0] for _, regla in hallazgos_en_codigo(fuente)]

    def test_detecta_replace_sobre_utc_now(self):
        self.assertEqual(self.reglas("x = utc_now().replace(tzinfo=None)"), ["a"])

    def test_detecta_replace_sobre_datetime_now_utc_en_todas_sus_formas(self):
        for expr in (
            "datetime.now(timezone.utc)",
            "datetime.now(dt_timezone.utc)",
            "datetime.datetime.now(datetime.timezone.utc)",
            "datetime.now(UTC)",
        ):
            with self.subTest(expr=expr):
                self.assertEqual(self.reglas(f"x = {expr}.replace(tzinfo=None)"), ["a"])

    def test_detecta_utcnow(self):
        self.assertEqual(self.reglas("x = datetime.utcnow()"), ["b"])

    def test_detecta_datetime_now_sin_argumentos(self):
        self.assertEqual(self.reglas("x = datetime.now()"), ["c"])

    def test_detecta_utc_now_aware_asignado_a_un_atributo(self):
        self.assertEqual(self.reglas("intento.resolved_at = utc_now()"), ["d"])
        self.assertEqual(self.reglas("intento.resolved_at: datetime = utc_now()"), ["d"])

    def test_informa_la_linea(self):
        fuente = "a = 1\n\nb = utc_now().replace(tzinfo=None)\n"
        self.assertEqual([linea for linea, _ in hallazgos_en_codigo(fuente)], [3])

    def test_permite_la_hora_de_pared_local_y_la_conversion_de_un_instante(self):
        for permitido in (
            "x = datetime.now(tz).replace(tzinfo=None)",
            "x = now.astimezone(zona).replace(tzinfo=None)",
            "x = bucket_local.astimezone(dt_timezone.utc).replace(tzinfo=None)",
        ):
            with self.subTest(permitido=permitido):
                self.assertEqual(hallazgos_en_codigo(permitido), [])

    def test_permite_los_usos_aware_legitimos(self):
        for permitido in (
            "x = datetime.now(timezone.utc)",
            "instant = promotion_evaluation_instant([order], now=utc_now())",
            "now = utc_now()",
            "x = utc_now_naive()",
            "intento.resolved_at = utc_now_naive()",
            "x = datetime.now(timezone.utc).isoformat()",
        ):
            with self.subTest(permitido=permitido):
                self.assertEqual(hallazgos_en_codigo(permitido), [])

    def test_un_comentario_o_una_cadena_no_dispara_la_guarda(self):
        fuente = '# usa .replace(tzinfo=None)\nx = "utc_now().replace(tzinfo=None)"\n'
        self.assertEqual(hallazgos_en_codigo(fuente), [])


class TestGuardaSobreElCodigoDeProduccion(unittest.TestCase):
    def test_ninguna_escritura_de_instantes_fuera_de_utc_now_naive(self):
        fallos = []
        archivos = 0
        for ruta, relativa in _archivos_de_produccion():
            archivos += 1
            for linea, regla in hallazgos_en_codigo(ruta.read_text(encoding="utf-8")):
                fallos.append(f"{relativa.as_posix()}:{linea} — {regla}")
        self.assertGreater(archivos, 50, "la guarda no está recorriendo el código de producción")
        self.assertEqual(
            fallos,
            [],
            "Escrituras de instantes fuera de la forma única (FR-005): usar `utc_now_naive()` "
            "para columnas sin zona.\n" + "\n".join(fallos),
        )


if __name__ == "__main__":
    unittest.main()
