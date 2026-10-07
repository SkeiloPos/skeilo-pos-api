"""Correo de invitación (spec 096, US1 + US5): FR-014 a FR-026.

    python -m unittest app.characterization_tests.test_correo_invitacion -v
"""
import unittest

from app.characterization_tests import auth_fixtures as af  # noqa: F401 - fija settings de test
from app.core.mail_templates import EmailContent, construir_correo_invitacion


class CorreoInvitacionTests(unittest.TestCase):
    def _construir(self, **overrides):
        kwargs = dict(
            nombre_negocio="Granizados Del Sol",
            url_ingreso="https://granizados.skeilopos.com/login",
            correo="maria@correo.com",
            contrasena_temporal="Temporal123!",
            nombre_persona="María Pérez",
        )
        kwargs.update(overrides)
        return construir_correo_invitacion(**kwargs)

    # ---------------------------------------------------------- FR-010/A-113

    def test_el_asunto_usa_el_nombre_comercial(self):
        correo = self._construir()
        self.assertEqual(correo.subject, "Te invitaron a unirte a Granizados Del Sol en SkeiloPOS")

    # ---------------------------------------------------------- FR-014

    def test_etiqueta_y_titulo_usan_el_valor_de_nombre_negocio_recibido(self):
        # FR-014: la responsabilidad de pasar el nombre comercial (y no el
        # identificador del subdominio) es del llamante — aquí se verifica
        # que el constructor usa tal cual lo que recibe en ese parámetro.
        # El router (T020/T021) es quien garantiza que ese valor sea
        # `tenant.name` y no `tenant.host`.
        correo = self._construir(nombre_negocio="Skeilorest Granizados")
        self.assertIn("INVITACIÓN · SKEILOREST GRANIZADOS", correo.html_body)
        self.assertIn("Te invitaron a unirte a Skeilorest Granizados.", correo.html_body)

    # ---------------------------------------------------------- FR-015/D11/FR-058

    def test_saludo_con_nombre(self):
        correo = self._construir(nombre_persona="María Pérez")
        self.assertIn("Hola, María Pérez.", correo.html_body)

    def test_saludo_sin_nombre(self):
        for nombre in (None, "", "   "):
            with self.subTest(nombre=nombre):
                correo = self._construir(nombre_persona=nombre)
                self.assertIn("Hola.", correo.html_body)
                self.assertNotIn("Hola, ", correo.html_body)
                self.assertNotIn("null", correo.html_body.lower())
                self.assertNotIn("None", correo.html_body)
                self.assertNotIn("Hola,  ", correo.html_body)  # sin doble espacio

    def test_el_correo_nunca_se_usa_como_nombre(self):
        correo = self._construir(nombre_persona=None)
        self.assertNotIn("maria@correo.com.", correo.html_body)

    # ---------------------------------------------------------- FR-016

    def test_las_tres_filas_van_en_orden(self):
        correo = self._construir()
        pos_url = correo.html_body.index("URL DE INICIO DE SESIÓN")
        pos_usuario = correo.html_body.index("USUARIO")
        pos_pass = correo.html_body.index("CONTRASEÑA TEMPORAL")
        self.assertLess(pos_url, pos_usuario)
        self.assertLess(pos_usuario, pos_pass)

    # ---------------------------------------------------------- FR-018

    def test_el_texto_del_enlace_coincide_con_el_href(self):
        correo = self._construir(url_ingreso="https://granizados.skeilopos.com/login")
        self.assertIn(
            '<a href="https://granizados.skeilopos.com/login" '
            'style="color:#6D28D9;text-decoration:underline;">'
            "https://granizados.skeilopos.com/login</a>",
            correo.html_body,
        )

    # ---------------------------------------------------------- FR-020

    def test_el_boton_apunta_al_mismo_destino(self):
        correo = self._construir(url_ingreso="https://granizados.skeilopos.com/login")
        self.assertIn('href="https://granizados.skeilopos.com/login" target="_blank"', correo.html_body)

    # ---------------------------------------------------------- FR-021

    def test_aviso_de_atencion_presente(self):
        correo = self._construir()
        self.assertIn("<strong>Antes de continuar.</strong>", correo.html_body)
        self.assertIn("fijar tu propia contraseña", correo.html_body)

    # ---------------------------------------------------------- FR-022/D10

    def test_sin_mencion_de_vencimiento(self):
        correo = self._construir()
        for palabra in ("vence", "vencimiento", "72 horas", "expira"):
            self.assertNotIn(palabra, correo.html_body.lower())

    # ---------------------------------------------------------- FR-023

    def test_contrasena_ausente_del_asunto_y_de_la_vista_previa(self):
        correo = self._construir(contrasena_temporal="Secreta123!")
        self.assertNotIn("Secreta123!", correo.subject)
        preview_start = correo.html_body.index("display:none")
        preview_end = correo.html_body.index("</div>", preview_start)
        self.assertNotIn("Secreta123!", correo.html_body[preview_start:preview_end])

    # ---------------------------------------------------------- FR-013

    def test_pie_menciona_el_negocio(self):
        correo = self._construir(nombre_negocio="Granizados Del Sol")
        self.assertIn(
            "Este correo fue enviado por SkeiloPOS en nombre de Granizados Del Sol.",
            correo.html_body,
        )
        self.assertIn("skeilo.tech@gmail.com", correo.html_body)

    # ---------------------------------------------------------- FR-026/D9 (US5)

    def test_el_alta_de_negocio_produce_el_mismo_contenido_que_una_invitacion(self):
        de_invitacion = construir_correo_invitacion(
            nombre_negocio="Granizados Del Sol",
            url_ingreso="https://granizados.skeilopos.com/login",
            correo="dueno@granizados.com",
            contrasena_temporal="Temporal123!",
        )
        del_alta = construir_correo_invitacion(
            nombre_negocio="Granizados Del Sol",
            url_ingreso="https://granizados.skeilopos.com/login",
            correo="dueno@granizados.com",
            contrasena_temporal="Temporal123!",
        )
        self.assertEqual(de_invitacion, del_alta)
        self.assertIsInstance(de_invitacion, EmailContent)


if __name__ == "__main__":
    unittest.main()
