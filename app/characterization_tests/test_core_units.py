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
from app.models.unit_measure_group import UnitMeasureGroup


class ConvertWithDuckTypedUnitsTests(unittest.TestCase):
    """`convert()` solo accede a `.group_id`, `.group.name` y `.factor_to_base`
    por duck typing; no exige que el objeto sea un `UnitMeasure` real. Se
    congela el comportamiento aritmético usando objetos mínimos con esos
    atributos, porque el modelo real (ver clase siguiente) no los tiene hoy.

    Spec 139: `convert()` compara `group_id` en vez de `dimension` (eliminada
    por ser un espejo redundante de `UnitMeasureGroup.name`, deuda técnica
    aceptada por la spec 137)."""

    def test_rn_cat_40_convierte_entre_unidades_del_mismo_grupo(self):
        grupo_masa = SimpleNamespace(name="masa")
        kg = SimpleNamespace(abbreviation="kg", group_id="masa", group=grupo_masa, factor_to_base=Decimal("1000"))
        g = SimpleNamespace(abbreviation="g", group_id="masa", group=grupo_masa, factor_to_base=Decimal("1"))
        self.assertEqual(convert(Decimal("2"), kg, g), Decimal("2000"))
        self.assertEqual(convert(Decimal("500"), g, kg), Decimal("0.5"))

    def test_rn_cat_40_grupos_distintos_lanza_422(self):
        g = SimpleNamespace(abbreviation="g", group_id="masa", group=SimpleNamespace(name="masa"), factor_to_base=Decimal("1"))
        ml = SimpleNamespace(abbreviation="ml", group_id="volumen", group=SimpleNamespace(name="volumen"), factor_to_base=Decimal("1"))
        with self.assertRaises(HTTPException) as ctx:
            convert(Decimal("500"), g, ml)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("Unidades incompatibles", ctx.exception.detail)
        self.assertIn("g (masa)", ctx.exception.detail)
        self.assertIn("ml (volumen)", ctx.exception.detail)

    def test_convertir_a_la_misma_unidad_es_identidad(self):
        g = SimpleNamespace(abbreviation="g", group_id="masa", group=SimpleNamespace(name="masa"), factor_to_base=Decimal("1"))
        self.assertEqual(convert(Decimal("123.456"), g, g), Decimal("123.456"))


class ConvertWithRealUnitMeasureModelTests(unittest.TestCase):
    """RN-CAT-41 [RESUELTO por spec 134]: el modelo real `UnitMeasure`
    (`app/models/unit_measure.py`) declara `group_id`/`factor_to_base` --
    `convert()` opera sobre instancias reales exactamente igual que sobre los
    objetos duck-typed de arriba.

    Spec 139: `dimension` se elimina del modelo; `group_id` -> `UnitMeasureGroup`
    pasa a ser la única fuente de verdad del grupo de una unidad."""

    def setUp(self):
        self.db = f.new_session()

    def test_rn_cat_41_unit_measure_real_ya_convierte_entre_grupos_iguales(self):
        grupo = UnitMeasureGroup(id=uuid.uuid4(), name="masa")
        self.db.add(grupo)
        self.db.flush()
        kg = f.make_unit(self.db, name="kilogramo", abbreviation="kg", group_id=grupo.id, factor_to_base=Decimal("1000"))
        g = f.make_unit(self.db, name="gramo", abbreviation="g", group_id=grupo.id, factor_to_base=Decimal("1"))
        self.assertEqual(convert(Decimal("2"), kg, g), Decimal("2000"))

    def test_rn_cat_41_unit_measure_real_ya_no_declara_dimension(self):
        self.assertNotIn("dimension", UnitMeasure.__table__.columns.keys())
        self.assertIn("factor_to_base", UnitMeasure.__table__.columns.keys())


if __name__ == "__main__":
    unittest.main()
