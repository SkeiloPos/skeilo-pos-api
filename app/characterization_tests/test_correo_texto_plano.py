"""Texto plano y payload del transporte (spec 096, FR-044 a FR-048).

Esta primera sección cubre `create_message`/`send_email_task` (FR-047): la
clave `textBody` se añade solo cuando hay texto, y el resto del payload
queda idéntico al actual. Las secciones de cada correo (invitación,
restablecimiento, contraseña cambiada) se añaden en sus historias
respectivas (T015a, T028a, T039a).

    python -m unittest app.characterization_tests.test_correo_texto_plano -v
"""
import unittest
from unittest.mock import patch

from app.characterization_tests import auth_fixtures as af  # noqa: F401 - fija settings de test
from app.core.mail import create_message
from app.core.config import settings


class CreateMessageTextBodyTests(unittest.TestCase):
    def test_sin_text_body_el_payload_no_lleva_textbody(self):
        message = create_message(["a@b.com"], "Asunto", "<p>html</p>")
        self.assertNotIn("textBody", message)

    def test_con_text_body_el_payload_incluye_textbody(self):
        message = create_message(["a@b.com"], "Asunto", "<p>html</p>", text_body="texto plano")
        self.assertEqual(message["textBody"], "texto plano")

    def test_text_body_none_explicito_omite_la_clave(self):
        message = create_message(["a@b.com"], "Asunto", "<p>html</p>", text_body=None)
        self.assertNotIn("textBody", message)

    def test_el_resto_del_payload_no_cambia(self):
        message = create_message(["a@b.com"], "Asunto", "<p>html</p>", text_body="texto")
        self.assertEqual(message["to"], ["a@b.com"])
        self.assertEqual(message["subject"], "Asunto")
        self.assertEqual(message["htmlBody"], "<p>html</p>")
        self.assertEqual(message["from"], f"{settings.MAIL_FROM_NAME} <{settings.MAIL_FROM}>")
        self.assertEqual(set(message.keys()), {"from", "to", "subject", "htmlBody", "textBody"})

    def test_sin_text_body_las_claves_son_las_de_siempre(self):
        message = create_message(["a@b.com"], "Asunto", "<p>html</p>")
        self.assertEqual(set(message.keys()), {"from", "to", "subject", "htmlBody"})


class SendEmailTaskTextBodyTests(unittest.TestCase):
    def test_send_email_task_pasa_text_body_a_create_message(self):
        from app.celery_task import send_email_task

        with patch("app.celery_task.create_message") as mock_create, patch(
            "app.celery_task.send_email"
        ):
            send_email_task.run(
                recipients=["a@b.com"], subject="Asunto", body="<p>html</p>", text_body="texto"
            )
        mock_create.assert_called_once_with(
            recipients=["a@b.com"], subject="Asunto", body="<p>html</p>", text_body="texto"
        )

    def test_send_email_task_sin_text_body_sigue_funcionando(self):
        from app.celery_task import send_email_task

        with patch("app.celery_task.create_message") as mock_create, patch(
            "app.celery_task.send_email"
        ):
            send_email_task.run(recipients=["a@b.com"], subject="Asunto", body="<p>html</p>")
        mock_create.assert_called_once_with(
            recipients=["a@b.com"], subject="Asunto", body="<p>html</p>", text_body=None
        )


class TextoPlanoInvitacionTests(unittest.TestCase):
    def _construir(self, **overrides):
        from app.core.mail_templates import construir_correo_invitacion

        kwargs = dict(
            nombre_negocio="Granizados Del Sol",
            url_ingreso="https://granizados.skeilopos.com/login",
            correo="maria@correo.com",
            contrasena_temporal="Temporal123!",
            nombre_persona="María Pérez",
        )
        kwargs.update(overrides)
        return construir_correo_invitacion(**kwargs)

    def test_lleva_titulo_saludo_datos_aviso_y_pie(self):
        correo = self._construir()
        self.assertIn("Te invitaron a unirte a Granizados Del Sol.", correo.text_body)
        self.assertIn("Hola, María Pérez.", correo.text_body)
        self.assertIn("USUARIO: maria@correo.com", correo.text_body)
        self.assertIn("CONTRASEÑA TEMPORAL: Temporal123!", correo.text_body)
        self.assertIn("Antes de continuar", correo.text_body)
        self.assertIn("skeilo.tech@gmail.com", correo.text_body)

    def test_la_url_de_ingreso_aparece_completa_y_en_linea_propia(self):
        correo = self._construir(url_ingreso="https://granizados.skeilopos.com/login")
        lineas = correo.text_body.split("\n")
        self.assertIn("https://granizados.skeilopos.com/login", lineas)

    def test_la_contrasena_no_esta_en_la_vista_previa_ni_en_el_asunto(self):
        correo = self._construir(contrasena_temporal="Secreta123!")
        self.assertNotIn("Secreta123!", correo.subject)
        # El texto plano no tiene "vista previa" propia: la garantía de
        # FR-023 sobre la vista previa es exclusiva del HTML.
        primera_linea = correo.text_body.split("\n", 1)[0]
        self.assertNotIn("Secreta123!", primera_linea)

    def test_marcado_y_apostrofe_van_crudos_en_texto_plano(self):
        correo = self._construir(nombre_persona="<b>x</b>")
        self.assertIn("Hola, <b>x</b>.", correo.text_body)
        self.assertNotIn("&lt;b&gt;", correo.text_body)

        correo2 = self._construir(nombre_persona="O'Brien")
        self.assertIn("Hola, O'Brien.", correo2.text_body)
        self.assertNotIn("&#x27;", correo2.text_body)


class TextoPlanoRestablecimientoTests(unittest.TestCase):
    def _construir(self, **overrides):
        from app.core.mail_templates import construir_correo_restablecimiento

        kwargs = dict(
            url_restablecimiento="https://granizados.skeilopos.com/reset-password?token=abc123",
            minutos_de_vigencia=45,
        )
        kwargs.update(overrides)
        return construir_correo_restablecimiento(**kwargs)

    def test_el_enlace_de_un_solo_uso_aparece_completo_en_linea_propia(self):
        correo = self._construir(
            url_restablecimiento="https://granizados.skeilopos.com/reset-password?token=abc123"
        )
        lineas = correo.text_body.split("\n")
        self.assertIn("https://granizados.skeilopos.com/reset-password?token=abc123", lineas)

    def test_los_minutos_salen_del_parametro_y_no_del_literal_30(self):
        correo = self._construir(minutos_de_vigencia=45)
        self.assertIn("Válido por 45 minutos.", correo.text_body)
        self.assertNotIn("30 minutos", correo.text_body)

    def test_advertencia_de_un_solo_uso_y_frase_de_cierre(self):
        correo = self._construir()
        self.assertIn("solo puede usarse una vez", correo.text_body.lower())
        self.assertIn("tu contraseña actual sigue funcionando", correo.text_body.lower())


class TextoPlanoContrasenaCambiadaTests(unittest.TestCase):
    def _construir(self, **overrides):
        from datetime import datetime

        from app.core.mail_templates import construir_correo_contrasena_cambiada

        kwargs = dict(
            correo="ana@granizados.com",
            momento=datetime(2026, 10, 2, 21, 6),
            url_ingreso="https://granizados.skeilopos.com/login",
            es_super_admin=False,
            nombre_negocio="Granizados Del Sol",
        )
        kwargs.update(overrides)
        return construir_correo_contrasena_cambiada(**kwargs)

    def test_misma_hora_de_colombia_que_el_html_sobre_el_mismo_momento(self):
        correo = self._construir()
        self.assertIn("02/10/2026 · 04:06 p. m. (hora de Colombia)", correo.text_body)
        self.assertIn("02/10/2026 · 04:06 p. m. (hora de Colombia)", correo.html_body)

    def test_aviso_de_tenant_y_fila_de_negocio(self):
        correo = self._construir(es_super_admin=False, nombre_negocio="Granizados Del Sol")
        self.assertIn("NEGOCIO: Granizados Del Sol", correo.text_body)
        self.assertIn("contacta de inmediato a un administrador de tu negocio", correo.text_body.lower())

    def test_aviso_de_super_admin_sin_fila_de_negocio(self):
        correo = self._construir(es_super_admin=True, nombre_negocio=None)
        self.assertNotIn("NEGOCIO:", correo.text_body)
        self.assertIn("skeilo.tech@gmail.com", correo.text_body)

    def test_el_enlace_de_ingreso_completo_en_linea_propia(self):
        correo = self._construir(url_ingreso="https://granizados.skeilopos.com/login")
        lineas = correo.text_body.split("\n")
        self.assertIn("https://granizados.skeilopos.com/login", lineas)


if __name__ == "__main__":
    unittest.main()
