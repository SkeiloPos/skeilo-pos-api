"""Spec 138, Historia 1 (FR-011, SC-010) — el correo de «contraseña cambiada» muestra la hora en la zona
del TENANT y no declara «hora de Colombia» a un negocio de otro país.

Antes la hora salía siempre en Bogotá con el texto fijo «(hora de Colombia)». Ahora:

  * Bogotá (y un usuario sin tenant, p. ej. Super Admin) conserva EXACTAMENTE el texto actual
    «… (hora de Colombia)»: los tres archivos de caracterización de correo que lo fijan no se tocan.
  * Una zona de la lista curada usa su nombre en español: «(hora de Ciudad de México)».
  * Una zona válida fuera de la lista usa su desfase, sin «hora de»: «(UTC−3)».

    python -m unittest app.characterization_tests.test_correo_zona_horaria -v
"""
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from app.characterization_tests import auth_fixtures as af  # noqa: F401 - fija settings de test
from app.api.v1.auth import routes as auth_routes
from app.core.mail_templates import construir_correo_contrasena_cambiada

# 2026-10-02 21:06 UTC. Bogotá 04:06 p. m., México 03:06 p. m., Madrid (UTC+2) 11:06 p. m.,
# Santiago y Buenos Aires (UTC−3) 06:06 p. m., Tokio (UTC+9) 06:06 a. m. del 03/10.
MOMENTO = datetime(2026, 10, 2, 21, 6)
MENOS = "−"


def _correo(zona=None, *, super_admin=False, **extra):
    kwargs = dict(
        correo="ana@negocio.com",
        momento=MOMENTO,
        url_ingreso="https://negocio.skeilopos.com/login",
        es_super_admin=super_admin,
        nombre_negocio=None if super_admin else "Negocio de prueba",
    )
    if zona is not None:
        kwargs["zona_horaria"] = zona
    kwargs.update(extra)
    return construir_correo_contrasena_cambiada(**kwargs)


def _linea_fecha_hora(correo) -> str:
    return next(l for l in correo.text_body.splitlines() if l.startswith("FECHA Y HORA: "))[len("FECHA Y HORA: "):]


class TestHoraSegunLaZonaDelTenant(unittest.TestCase):
    def test_bogota_es_identico_al_texto_de_hoy(self):
        sin_zona = _correo()
        con_zona = _correo("America/Bogota")
        self.assertEqual(_linea_fecha_hora(con_zona), "02/10/2026 · 04:06 p. m. (hora de Colombia)")
        self.assertEqual(con_zona.text_body, sin_zona.text_body)
        self.assertEqual(con_zona.html_body, sin_zona.html_body)

    def test_zonas_de_la_lista_curada_usan_su_nombre_y_su_hora_local(self):
        esperado = {
            "America/Mexico_City": "02/10/2026 · 03:06 p. m. (hora de Ciudad de México)",
            "America/Lima": "02/10/2026 · 04:06 p. m. (hora de Lima)",
            "America/Santiago": "02/10/2026 · 06:06 p. m. (hora de Santiago)",
            "America/Argentina/Buenos_Aires": "02/10/2026 · 06:06 p. m. (hora de Buenos Aires)",
            "Europe/Madrid": "02/10/2026 · 11:06 p. m. (hora de Madrid)",
            "Asia/Tokyo": "03/10/2026 · 06:06 a. m. (hora de Tokio)",
        }
        for zona, texto in esperado.items():
            with self.subTest(zona=zona):
                self.assertEqual(_linea_fecha_hora(_correo(zona)), texto)

    def test_zona_valida_fuera_de_la_lista_usa_el_desfase_sin_hora_de(self):
        linea = _linea_fecha_hora(_correo("America/Sao_Paulo"))
        self.assertEqual(linea, f"02/10/2026 · 06:06 p. m. (UTC{MENOS}3)")
        self.assertNotIn("hora de", linea)

    def test_zona_con_media_hora_de_desfase(self):
        self.assertEqual(
            _linea_fecha_hora(_correo("Asia/Kolkata")), "03/10/2026 · 02:36 a. m. (UTC+5:30)"
        )

    def test_sin_zona_equivale_a_bogota(self):
        # Super Admin / usuario sin tenant: `zona_horaria=None`.
        self.assertEqual(
            _linea_fecha_hora(_correo(None, super_admin=True)),
            "02/10/2026 · 04:06 p. m. (hora de Colombia)",
        )

    def test_ningun_texto_de_un_tenant_fuera_de_colombia_dice_colombia(self):
        """SC-010: ni el HTML ni el texto plano (incluida la vista previa) declaran «Colombia»."""
        for zona in ("America/Mexico_City", "America/Lima", "America/Santiago", "America/Argentina/Buenos_Aires",
                     "Europe/Madrid", "Asia/Tokyo", "America/Sao_Paulo", "Asia/Kolkata", "UTC"):
            with self.subTest(zona=zona):
                correo = _correo(zona)
                self.assertNotIn("Colombia", correo.html_body)
                self.assertNotIn("Colombia", correo.text_body)

    def test_un_tenant_de_bogota_si_dice_colombia(self):
        self.assertIn("(hora de Colombia)", _correo("America/Bogota").html_body)


class TestElDespachoPasaLaZonaDelTenant(unittest.TestCase):
    """`_dispatch_password_changed_email` entrega `user.tenant.timezone` si el usuario tiene tenant."""

    def _despachar(self, usuario) -> mock.MagicMock:
        with mock.patch.object(auth_routes, "send_email_task") as tarea, \
                mock.patch.object(auth_routes, "_build_login_url_for_user", return_value="https://x.skeilopos.com/login"):
            auth_routes._dispatch_password_changed_email(usuario, MOMENTO)
        return tarea

    def test_usuario_de_un_tenant_de_mexico_recibe_la_hora_de_mexico(self):
        usuario = SimpleNamespace(
            email="ana@negocio.com", tenant_id=7, tenant_name="Negocio",
            tenant=SimpleNamespace(timezone="America/Mexico_City", host="negocio"),
        )
        tarea = self._despachar(usuario)
        tarea.delay.assert_called_once()
        cuerpo = tarea.delay.call_args.kwargs["body"]
        self.assertIn("03:06 p. m. (hora de Ciudad de México)", cuerpo)
        self.assertNotIn("Colombia", cuerpo)

    def test_super_admin_sin_tenant_recibe_la_hora_de_bogota(self):
        usuario = SimpleNamespace(email="admin@skeilopos.com", tenant_id=None, tenant_name=None, tenant=None)
        tarea = self._despachar(usuario)
        tarea.delay.assert_called_once()
        self.assertIn("04:06 p. m. (hora de Colombia)", tarea.delay.call_args.kwargs["body"])


if __name__ == "__main__":
    unittest.main()
