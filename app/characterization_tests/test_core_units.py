"""CONGELA app/core/units.py — conversión de unidades de medida.

Referencias: `reglas-de-negocio.md` RN-CAT-40/RN-CAT-41 y
`registro-de-anomalias.md` A-31 (candidato a completar la migración,
"necesitan conversión litros↔onzas para el granizado") del repositorio
`pos-specs`.
"""
import unittest
import uuid
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException

from app.characterization_tests import fixtures as f
from app.core.units import convert
from app.models.unit_measure import UnitMeasure


class ConvertWithDuckTypedUnitsTests(unittest.TestCase):
    """`convert()` solo accede a `.dimension` y `.factor_to_base` por duck
    typing; no exige que el objeto sea un `UnitMeasure` real. Se congela el
    comportamiento aritmético usando objetos mínimos con esos dos atributos,
    porque el modelo real (ver clase siguiente) no los tiene hoy."""

    def test_rn_cat_40_convierte_entre_unidades_de_la_misma_dimension(self):
        kg = SimpleNamespace(abbreviation="kg", dimension="masa", factor_to_base=Decimal("1000"))
        g = SimpleNamespace(abbreviation="g", dimension="masa", factor_to_base=Decimal("1"))
        self.assertEqual(convert(Decimal("2"), kg, g), Decimal("2000"))
        self.assertEqual(convert(Decimal("500"), g, kg), Decimal("0.5"))

    def test_rn_cat_40_dimensiones_distintas_lanza_422(self):
        g = SimpleNamespace(abbreviation="g", dimension="masa", factor_to_base=Decimal("1"))
        ml = SimpleNamespace(abbreviation="ml", dimension="volumen", factor_to_base=Decimal("1"))
        with self.assertRaises(HTTPException) as ctx:
            convert(Decimal("500"), g, ml)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("Unidades incompatibles", ctx.exception.detail)
        self.assertIn("g (masa)", ctx.exception.detail)
        self.assertIn("ml (volumen)", ctx.exception.detail)

    def test_convertir_a_la_misma_unidad_es_identidad(self):
        g = SimpleNamespace(abbreviation="g", dimension="masa", factor_to_base=Decimal("1"))
        self.assertEqual(convert(Decimal("123.456"), g, g), Decimal("123.456"))


class ConvertWithRealUnitMeasureModelTests(unittest.TestCase):
    """RN-CAT-41 [RESUELTO por spec 134]: el modelo real `UnitMeasure`
    (`app/models/unit_measure.py`) ya declara `dimension`/`factor_to_base` --
    `convert()` deja de ser código muerto y opera sobre instancias reales
    exactamente igual que sobre los objetos duck-typed de arriba."""

    def setUp(self):
        self.db = f.new_session()

    def test_rn_cat_41_unit_measure_real_ya_convierte_entre_dimensiones_iguales(self):
        kg = f.make_unit(self.db, name="kilogramo", abbreviation="kg", dimension="masa", factor_to_base=Decimal("1000"))
        g = f.make_unit(self.db, name="gramo", abbreviation="g", dimension="masa", factor_to_base=Decimal("1"))
        self.assertEqual(convert(Decimal("2"), kg, g), Decimal("2000"))

    def test_rn_cat_41_unit_measure_real_ya_declara_esas_columnas(self):
        self.assertIn("dimension", UnitMeasure.__table__.columns.keys())
        self.assertIn("factor_to_base", UnitMeasure.__table__.columns.keys())


if __name__ == "__main__":
    unittest.main()
