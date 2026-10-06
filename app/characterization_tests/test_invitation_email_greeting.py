"""Saludo del correo de invitación (spec 091, A-100; spec 096, D11, FR-059).

El saludo se integra en el párrafo de entrada (D11): "Hola, {nombre}." con
nombre y "Hola." sin él, en vez del "Hola, {nombre}:" en línea propia que
usaba `invitation_email_body`. Se reapunta al constructor nuevo
(`construir_correo_invitacion`) citando FR-059 + D11.

    python -m unittest app.characterization_tests.test_invitation_email_greeting -v
"""
import unittest

from app.characterization_tests import auth_fixtures as af  # noqa: F401 - fija settings de test
from app.core.mail_templates import construir_correo_invitacion

_KW = dict(
    nombre_negocio="Acme",
    url_ingreso="https://acme.skeilopos.com/login",
    correo="ana@acme.com",
    contrasena_temporal="Temporal123!",
)


class InvitationEmailGreetingTests(unittest.TestCase):
    def test_con_nombre_saluda_por_el_nombre(self):
        correo = construir_correo_invitacion(**_KW, nombre_persona="María Pérez")
        self.assertIn("Hola, María Pérez.", correo.html_body)

    def test_el_apostrofe_se_escapa_en_html_y_va_crudo_en_texto(self):
        correo = construir_correo_invitacion(**_KW, nombre_persona="O'Brien")
        self.assertIn("Hola, O&#x27;Brien.", correo.html_body)
        self.assertNotIn("O'Brien", correo.html_body)
        # research.md Decisión 3: en texto plano el apóstrofo va crudo.
        self.assertIn("Hola, O'Brien.", correo.text_body)
        self.assertNotIn("&#x27;", correo.text_body)

    def test_sin_nombre_saluda_hola_y_nunca_con_el_correo(self):
        for nombre in (None, ""):
            with self.subTest(nombre=nombre):
                correo = construir_correo_invitacion(**_KW, nombre_persona=nombre)
                self.assertIn("Hola.", correo.html_body)
                self.assertNotIn("Hola, ", correo.html_body)

    def test_el_resto_del_cuerpo_no_cambia(self):
        for correo in (
            construir_correo_invitacion(**_KW),
            construir_correo_invitacion(**_KW, nombre_persona="Ana Pérez"),
        ):
            self.assertIn("Te invitaron a unirte a Acme", correo.html_body)
            self.assertIn("https://acme.skeilopos.com/login", correo.html_body)
            self.assertIn("ana@acme.com", correo.html_body)
            self.assertIn("Temporal123!", correo.html_body)
            self.assertIn("fijar tu propia contraseña", correo.html_body)

    def test_los_llamadores_sin_nombre_persona_siguen_funcionando(self):
        self.assertIn("Hola.", construir_correo_invitacion(**_KW).html_body)


if __name__ == "__main__":
    unittest.main()
