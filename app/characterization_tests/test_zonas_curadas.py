"""Spec 138 (FR-010, FR-011) — lista curada de zonas horarias y etiqueta del correo.

La lista de siete zonas es una constante duplicada en el backend
(`app.core.timezone.ZONAS_CURADAS`) y en el frontend
(`src/app/shared/tenant-timezones.ts`); cada lado compara contra **el mismo literal**
(precedente: spec 091, subdominios reservados). Si cambia en un lado, una prueba obliga
a cambiarla en el otro. El literal de abajo se repite en
`tenant-timezones.parity.spec.ts`.

No toca Postgres: son funciones puras.

    python -m unittest app.characterization_tests.test_zonas_curadas -v
"""
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.core.timezone import ZONAS_CURADAS, etiqueta_zona_correo

# (IANA, nombre en el selector, etiqueta del correo) — data-model.md §3.
LITERAL_ZONAS_CURADAS = [
    ("America/Bogota", "Bogotá (Colombia)", "Colombia"),
    ("America/Mexico_City", "Ciudad de México", "Ciudad de México"),
    ("America/Lima", "Lima (Perú)", "Lima"),
    ("America/Santiago", "Santiago (Chile)", "Santiago"),
    ("America/Argentina/Buenos_Aires", "Buenos Aires (Argentina)", "Buenos Aires"),
    ("Europe/Madrid", "Madrid (España)", "Madrid"),
    ("Asia/Tokyo", "Tokio (Japón)", "Tokio"),
]

_MENOS = "−"  # signo menos tipográfico, no el guion ASCII


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class TestZonasCuradas(unittest.TestCase):
    def test_la_lista_es_exactamente_el_literal_acordado(self):
        obtenida = [(z.iana, z.nombre, z.etiqueta) for z in ZONAS_CURADAS]
        self.assertEqual(obtenida, LITERAL_ZONAS_CURADAS)

    def test_la_lista_es_inmutable(self):
        self.assertIsInstance(ZONAS_CURADAS, tuple)
        with self.assertRaises(AttributeError):
            ZONAS_CURADAS[0].iana = "Nowhere/Land"

    def test_toda_zona_curada_se_resuelve_con_zoneinfo(self):
        for zona in ZONAS_CURADAS:
            with self.subTest(zona=zona.iana):
                self.assertEqual(ZoneInfo(zona.iana).key, zona.iana)


class TestEtiquetaZonaCorreo(unittest.TestCase):
    def test_cada_zona_curada_devuelve_su_etiqueta(self):
        instante = _utc(2026, 10, 25, 17, 0)
        for iana, _nombre, etiqueta in LITERAL_ZONAS_CURADAS:
            with self.subTest(zona=iana):
                self.assertEqual(etiqueta_zona_correo(iana, instante), etiqueta)

    def test_zona_valida_fuera_de_la_lista_devuelve_el_desfase(self):
        instante = _utc(2026, 10, 25, 17, 0)
        self.assertEqual(etiqueta_zona_correo("America/Sao_Paulo", instante), f"UTC{_MENOS}3")
        self.assertEqual(etiqueta_zona_correo("Asia/Kolkata", instante), "UTC+5:30")
        self.assertEqual(etiqueta_zona_correo("UTC", instante), "UTC")

    def test_el_signo_menos_es_el_tipografico(self):
        etiqueta = etiqueta_zona_correo("America/Sao_Paulo", _utc(2026, 10, 25, 17, 0))
        self.assertIn(_MENOS, etiqueta)
        self.assertNotIn("-", etiqueta)

    def test_los_minutos_solo_aparecen_si_no_son_cero(self):
        instante = _utc(2026, 10, 25, 17, 0)
        self.assertEqual(etiqueta_zona_correo("Asia/Kathmandu", instante), "UTC+5:45")
        self.assertEqual(etiqueta_zona_correo("Asia/Dubai", instante), "UTC+4")

    def test_el_desfase_se_calcula_para_el_instante_dado(self):
        # Berlín no está en la lista curada y tiene horario de verano.
        self.assertEqual(etiqueta_zona_correo("Europe/Berlin", _utc(2026, 1, 15, 12, 0)), "UTC+1")
        self.assertEqual(etiqueta_zona_correo("Europe/Berlin", _utc(2026, 7, 15, 12, 0)), "UTC+2")
        # Y una zona que a veces coincide con UTC cae en «UTC» ese día.
        self.assertEqual(etiqueta_zona_correo("Europe/London", _utc(2026, 1, 15, 12, 0)), "UTC")
        self.assertEqual(etiqueta_zona_correo("Europe/London", _utc(2026, 7, 15, 12, 0)), "UTC+1")

    def test_un_instante_naive_se_interpreta_como_utc(self):
        # Los llamantes reales pasan un `datetime` sin zona guardado en UTC.
        naive = datetime(2026, 7, 15, 12, 0)
        self.assertEqual(etiqueta_zona_correo("Europe/Berlin", naive), "UTC+2")


if __name__ == "__main__":
    unittest.main()
