"""Escape, validación y formato de fecha de `app/core/mail_templates/_campos.py`
(spec 096, FR-040 a FR-043).

    python -m unittest app.characterization_tests.test_plantillas_correo_escape -v
"""
import os
import unittest
from datetime import datetime, timezone

from app.characterization_tests import auth_fixtures as af  # noqa: F401 - fija settings de test
from app.core.mail_templates import (
    ErrorDePlantillaDeCorreo,
    construir_correo_contrasena_cambiada,
    construir_correo_invitacion,
    construir_correo_restablecimiento,
)
from app.core.mail_templates._campos import (
    escapar_valores,
    formatear_fecha_hora_colombia,
    validar_campos_obligatorios,
)


class EscaparValoresTests(unittest.TestCase):
    def test_marcado_queda_escapado(self):
        self.assertEqual(escapar_valores(x="<b>x</b>")["x"], "&lt;b&gt;x&lt;/b&gt;")

    def test_ampersand_y_comillas_quedan_escapados(self):
        self.assertEqual(escapar_valores(x="Ñandú & Cía")["x"], "Ñandú &amp; Cía")
        self.assertIn("&#x27;", escapar_valores(x="O'Brien")["x"])
        self.assertIn("&quot;", escapar_valores(x='"x"')["x"])

    def test_el_apostrofe_no_queda_crudo_en_el_escapado(self):
        self.assertNotIn("O'Brien", escapar_valores(x="O'Brien")["x"])

    def test_varios_valores_en_una_sola_llamada(self):
        resultado = escapar_valores(a="<i>a</i>", b="O'Brien")
        self.assertEqual(resultado["a"], "&lt;i&gt;a&lt;/i&gt;")
        self.assertIn("&#x27;", resultado["b"])


class ValidarCamposObligatoriosTests(unittest.TestCase):
    def test_valor_none_levanta_error(self):
        with self.assertRaises(ErrorDePlantillaDeCorreo):
            validar_campos_obligatorios(correo=None)

    def test_cadena_vacia_levanta_error(self):
        with self.assertRaises(ErrorDePlantillaDeCorreo):
            validar_campos_obligatorios(correo="")

    def test_cadena_solo_espacios_levanta_error(self):
        with self.assertRaises(ErrorDePlantillaDeCorreo):
            validar_campos_obligatorios(correo="   ")

    def test_entero_cero_o_negativo_levanta_error(self):
        for valor in (0, -1):
            with self.subTest(valor=valor):
                with self.assertRaises(ErrorDePlantillaDeCorreo):
                    validar_campos_obligatorios(minutos_de_vigencia=valor)

    def test_entero_positivo_no_levanta_error(self):
        validar_campos_obligatorios(minutos_de_vigencia=30)  # no debe lanzar

    def test_valores_validos_no_levantan_error(self):
        validar_campos_obligatorios(correo="a@b.com", nombre="Ana")  # no debe lanzar

    def test_el_mensaje_nombra_los_campos_faltantes_y_ningun_valor(self):
        with self.assertRaises(ErrorDePlantillaDeCorreo) as ctx:
            validar_campos_obligatorios(correo="", contrasena_temporal="Secreta123!")
        mensaje = str(ctx.exception)
        self.assertIn("correo", mensaje)
        self.assertNotIn("Secreta123!", mensaje)
        self.assertNotIn("contrasena_temporal", mensaje)  # ese campo sí llegó con valor

    def test_varios_campos_faltantes_se_nombran_todos(self):
        with self.assertRaises(ErrorDePlantillaDeCorreo) as ctx:
            validar_campos_obligatorios(correo="", nombre_negocio=None)
        mensaje = str(ctx.exception)
        self.assertIn("correo", mensaje)
        self.assertIn("nombre_negocio", mensaje)


class FormatearFechaHoraColombiaTests(unittest.TestCase):
    def test_ejemplo_normativo_naive_interpretado_como_utc(self):
        momento = datetime(2026, 10, 2, 21, 6)  # naive, como lo pasan los dos llamantes
        self.assertEqual(
            formatear_fecha_hora_colombia(momento), "02/10/2026 · 04:06 p. m. (hora de Colombia)"
        )

    def test_ejemplo_normativo_no_depende_de_la_zona_del_host(self):
        viejo_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/Bogota"
        try:
            momento = datetime(2026, 10, 2, 21, 6)
            self.assertEqual(
                formatear_fecha_hora_colombia(momento),
                "02/10/2026 · 04:06 p. m. (hora de Colombia)",
            )
        finally:
            if viejo_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = viejo_tz

    def test_madrugada_usa_am_y_cero_inicial(self):
        # 05:30 UTC = 00:30 en Bogotá (UTC-5): medianoche de 12 horas, "a. m.".
        momento = datetime(2026, 10, 2, 5, 30)
        self.assertEqual(
            formatear_fecha_hora_colombia(momento), "02/10/2026 · 12:30 a. m. (hora de Colombia)"
        )

    def test_una_de_la_tarde_usa_una_con_cero_inicial(self):
        # 18:05 UTC = 13:05 en Bogotá: la hora 13 se muestra como "01", no "13".
        momento = datetime(2026, 10, 2, 18, 5)
        self.assertEqual(
            formatear_fecha_hora_colombia(momento), "02/10/2026 · 01:05 p. m. (hora de Colombia)"
        )

    def test_valor_con_tzinfo_explicito_se_respeta_sin_sobreescribirlo(self):
        # Ya viene en UTC explícito (no naive): debe convertirse igual, sin
        # que se le pise el tzinfo con el que ya llegó.
        momento = datetime(2026, 10, 2, 21, 6, tzinfo=timezone.utc)
        self.assertEqual(
            formatear_fecha_hora_colombia(momento), "02/10/2026 · 04:06 p. m. (hora de Colombia)"
        )


class CamposObligatoriosInvitacionTests(unittest.TestCase):
    """T019: los cuatro campos obligatorios del correo de invitación
    (FR-041, FR-042)."""

    _BASE = dict(
        nombre_negocio="Granizados Del Sol",
        url_ingreso="https://granizados.skeilopos.com/login",
        correo="maria@correo.com",
        contrasena_temporal="Temporal123!",
    )

    def test_cada_campo_obligatorio_vacio_nulo_o_solo_espacios_levanta_error(self):
        for campo in ("nombre_negocio", "url_ingreso", "correo", "contrasena_temporal"):
            for valor_invalido in (None, "", "   "):
                with self.subTest(campo=campo, valor=repr(valor_invalido)):
                    kwargs = dict(self._BASE)
                    kwargs[campo] = valor_invalido
                    with self.assertRaises(ErrorDePlantillaDeCorreo):
                        construir_correo_invitacion(**kwargs)

    def test_nombre_persona_ausente_no_es_error(self):
        for valor in (None, "", "   "):
            with self.subTest(valor=repr(valor)):
                construir_correo_invitacion(**self._BASE, nombre_persona=valor)  # no debe lanzar

    def test_el_mensaje_de_excepcion_no_contiene_la_contrasena_temporal(self):
        kwargs = dict(self._BASE)
        kwargs["nombre_negocio"] = ""
        kwargs["contrasena_temporal"] = "Secreta123!"
        with self.assertRaises(ErrorDePlantillaDeCorreo) as ctx:
            construir_correo_invitacion(**kwargs)
        self.assertNotIn("Secreta123!", str(ctx.exception))

    def test_ningun_cuerpo_contiene_none_ni_null(self):
        correo = construir_correo_invitacion(**self._BASE)
        for cuerpo in (correo.html_body, correo.text_body):
            self.assertNotIn("None", cuerpo)
            self.assertNotIn("null", cuerpo.lower())


class CamposObligatoriosRestablecimientoTests(unittest.TestCase):
    """T032: los dos campos obligatorios del correo de restablecimiento
    (FR-041)."""

    _BASE = dict(
        url_restablecimiento="https://granizados.skeilopos.com/reset-password?token=abc123",
        minutos_de_vigencia=30,
    )

    def test_url_restablecimiento_vacia_nula_o_solo_espacios_levanta_error(self):
        for valor_invalido in (None, "", "   "):
            with self.subTest(valor=repr(valor_invalido)):
                kwargs = dict(self._BASE)
                kwargs["url_restablecimiento"] = valor_invalido
                with self.assertRaises(ErrorDePlantillaDeCorreo):
                    construir_correo_restablecimiento(**kwargs)

    def test_minutos_de_vigencia_cero_o_negativo_levanta_error(self):
        for valor_invalido in (0, -1):
            with self.subTest(valor=valor_invalido):
                kwargs = dict(self._BASE)
                kwargs["minutos_de_vigencia"] = valor_invalido
                with self.assertRaises(ErrorDePlantillaDeCorreo):
                    construir_correo_restablecimiento(**kwargs)


class CamposObligatoriosContrasenaCambiadaTests(unittest.TestCase):
    """T043: los campos obligatorios del correo de contraseña cambiada y
    las dos combinaciones cruzadas inválidas (FR-041, data-model.md →
    Consistencia obligatoria)."""

    _BASE_TENANT = dict(
        correo="ana@granizados.com",
        momento=datetime(2026, 10, 2, 21, 6),
        url_ingreso="https://granizados.skeilopos.com/login",
        es_super_admin=False,
        nombre_negocio="Granizados Del Sol",
    )

    def test_correo_vacio_nulo_o_solo_espacios_levanta_error(self):
        for valor_invalido in (None, "", "   "):
            with self.subTest(valor=repr(valor_invalido)):
                kwargs = dict(self._BASE_TENANT)
                kwargs["correo"] = valor_invalido
                with self.assertRaises(ErrorDePlantillaDeCorreo):
                    construir_correo_contrasena_cambiada(**kwargs)

    def test_url_ingreso_vacia_nula_o_solo_espacios_levanta_error(self):
        for valor_invalido in (None, "", "   "):
            with self.subTest(valor=repr(valor_invalido)):
                kwargs = dict(self._BASE_TENANT)
                kwargs["url_ingreso"] = valor_invalido
                with self.assertRaises(ErrorDePlantillaDeCorreo):
                    construir_correo_contrasena_cambiada(**kwargs)

    def test_momento_none_levanta_error(self):
        kwargs = dict(self._BASE_TENANT)
        kwargs["momento"] = None
        with self.assertRaises(ErrorDePlantillaDeCorreo):
            construir_correo_contrasena_cambiada(**kwargs)

    def test_es_super_admin_true_con_nombre_negocio_no_vacio_levanta_error(self):
        kwargs = dict(self._BASE_TENANT)
        kwargs["es_super_admin"] = True
        # nombre_negocio sigue presente ("Granizados Del Sol") — combinación
        # cruzada inválida.
        with self.assertRaises(ErrorDePlantillaDeCorreo):
            construir_correo_contrasena_cambiada(**kwargs)

    def test_es_super_admin_false_sin_nombre_negocio_levanta_error(self):
        kwargs = dict(self._BASE_TENANT)
        kwargs["nombre_negocio"] = None
        with self.assertRaises(ErrorDePlantillaDeCorreo):
            construir_correo_contrasena_cambiada(**kwargs)


if __name__ == "__main__":
    unittest.main()
