"""Correo de restablecimiento de contraseña (spec 096, US2): FR-027 a
FR-032.

    python -m unittest app.characterization_tests.test_correo_restablecimiento -v
"""
import re
import unittest

from app.characterization_tests import auth_fixtures as af  # noqa: F401 - fija settings de test
from app.core.mail_templates import construir_correo_restablecimiento


class CorreoRestablecimientoTests(unittest.TestCase):
    def _construir(self, **overrides):
        kwargs = dict(
            url_restablecimiento="https://granizados.skeilopos.com/reset-password?token=abc123",
            minutos_de_vigencia=30,
        )
        kwargs.update(overrides)
        return construir_correo_restablecimiento(**kwargs)

    # ---------------------------------------------------------- FR-010/A-113

    def test_asunto_literal(self):
        self.assertEqual(self._construir().subject, "Restablece tu contraseña de SkeiloPOS")

    # ---------------------------------------------------------- FR-027

    def test_etiqueta_titulo_y_parrafo(self):
        html_body = self._construir().html_body
        self.assertIn("SEGURIDAD DE LA CUENTA", html_body)
        self.assertIn("Restablece tu contraseña.", html_body)

    # ---------------------------------------------------------- FR-028

    def test_el_boton_apunta_al_enlace_de_un_solo_uso(self):
        html_body = self._construir(
            url_restablecimiento="https://granizados.skeilopos.com/reset-password?token=abc123"
        ).html_body
        self.assertIn("Restablecer contraseña", html_body)
        self.assertIn(
            'href="https://granizados.skeilopos.com/reset-password?token=abc123" target="_blank"',
            html_body,
        )

    # ---------------------------------------------------------- FR-029/FR-030

    def test_vigencia_sale_del_parametro_no_de_un_literal(self):
        html_body = self._construir(minutos_de_vigencia=45).html_body
        self.assertIn("Válido por 45 minutos.", html_body)
        self.assertNotIn("Válido por 30 minutos.", html_body)
        self.assertIn("El enlace solo puede usarse una vez.", html_body)

    def test_la_vista_previa_tambien_usa_los_minutos_del_parametro(self):
        html_body = self._construir(minutos_de_vigencia=45).html_body
        preview_start = html_body.index("display:none")
        preview_end = html_body.index("</div>", preview_start)
        preview = html_body[preview_start:preview_end]
        self.assertIn("45 minutos", preview)
        self.assertNotIn("30 minutos", preview)

    # ---------------------------------------------------------- FR-031/A-116/FR-060

    def test_el_enlace_visible_aparece_despues_del_href_del_boton(self):
        html_body = self._construir().html_body
        pos_boton = html_body.index('target="_blank"')
        pos_enlace_visible = html_body.index(">https://granizados.skeilopos.com/reset-password")
        self.assertLess(pos_boton, pos_enlace_visible)

    def test_el_regex_de_extraccion_del_token_encuentra_el_del_boton(self):
        html_body = self._construir(
            url_restablecimiento="https://granizados.skeilopos.com/reset-password?token=elTokenDelBoton"
        ).html_body
        match = re.search(r'token=([^"&]+)', html_body)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "elTokenDelBoton")

    # ---------------------------------------------------------- FR-032

    def test_frase_de_cierre(self):
        html_body = self._construir().html_body
        self.assertIn("tu contraseña actual sigue funcionando", html_body)

    # ---------------------------------------------------------- FR-043

    def test_ningun_nombre_de_negocio_ni_subdominio_ajeno(self):
        correo = self._construir(
            url_restablecimiento="https://granizados.skeilopos.com/reset-password?token=abc123"
        )
        for cuerpo in (correo.html_body, correo.text_body):
            self.assertNotIn("NEGOCIO", cuerpo)
            # Ningún subdominio distinto del propio: solo debe aparecer el
            # host incluido en la URL de restablecimiento.
            dominios = re.findall(r"https://([a-z0-9.-]+)/", cuerpo)
            self.assertTrue(all(d in ("granizados.skeilopos.com", "assets.skeilopos.com") for d in dominios))


if __name__ == "__main__":
    unittest.main()
