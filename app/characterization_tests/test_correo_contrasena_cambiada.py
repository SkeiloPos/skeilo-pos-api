"""Correo de aviso de contraseña cambiada (spec 096, US3): FR-033 a
FR-039.

    python -m unittest app.characterization_tests.test_correo_contrasena_cambiada -v
"""
import os
import unittest
from datetime import datetime

from app.characterization_tests import auth_fixtures as af  # noqa: F401 - fija settings de test
from app.core.mail_templates import ErrorDePlantillaDeCorreo, construir_correo_contrasena_cambiada


class CorreoContrasenaCambiadaTests(unittest.TestCase):
    def _tenant(self, **overrides):
        kwargs = dict(
            correo="ana@granizados.com",
            momento=datetime(2026, 10, 2, 21, 6),
            url_ingreso="https://granizados.skeilopos.com/login",
            es_super_admin=False,
            nombre_negocio="Granizados Del Sol",
        )
        kwargs.update(overrides)
        return construir_correo_contrasena_cambiada(**kwargs)

    def _super_admin(self, **overrides):
        kwargs = dict(
            correo="admin@skeilopos.com",
            momento=datetime(2026, 10, 2, 21, 6),
            url_ingreso="https://admin.skeilopos.com/login",
            es_super_admin=True,
            nombre_negocio=None,
        )
        kwargs.update(overrides)
        return construir_correo_contrasena_cambiada(**kwargs)

    # ---------------------------------------------------------- FR-010/A-113

    def test_asunto_literal(self):
        self.assertEqual(self._tenant().subject, "Tu contraseña de SkeiloPOS fue cambiada")

    # ---------------------------------------------------------- FR-033

    def test_etiqueta_y_titulo(self):
        html_body = self._tenant().html_body
        self.assertIn("CONTRASEÑA ACTUALIZADA", html_body)
        self.assertIn("Tu contraseña fue cambiada.", html_body)

    # ---------------------------------------------------------- FR-034

    def test_variante_tenant_tiene_las_tres_filas(self):
        correo = self._tenant(correo="ana@granizados.com", nombre_negocio="Granizados Del Sol")
        pos_cuenta = correo.html_body.index("CUENTA")
        pos_negocio = correo.html_body.index("NEGOCIO")
        pos_fecha = correo.html_body.index("FECHA Y HORA")
        self.assertLess(pos_cuenta, pos_negocio)
        self.assertLess(pos_negocio, pos_fecha)
        self.assertIn("Granizados Del Sol", correo.html_body)

    # ---------------------------------------------------------- FR-035

    def test_variante_super_admin_omite_la_fila_de_negocio_sin_hueco(self):
        correo = self._super_admin()
        self.assertNotIn("NEGOCIO", correo.html_body)
        self.assertNotIn("None", correo.html_body)
        self.assertIn("CUENTA", correo.html_body)
        self.assertIn("FECHA Y HORA", correo.html_body)

    # ---------------------------------------------------------- FR-036/A-118

    def test_ejemplo_normativo(self):
        correo = self._tenant(momento=datetime(2026, 10, 2, 21, 6))
        self.assertIn("02/10/2026 · 04:06 p. m. (hora de Colombia)", correo.html_body)

    def test_caso_de_madrugada_cero_inicial_y_am(self):
        # 05:30 UTC = 00:30 en Bogotá.
        correo = self._tenant(momento=datetime(2026, 10, 2, 5, 30))
        self.assertIn("02/10/2026 · 12:30 a. m. (hora de Colombia)", correo.html_body)

    def test_ejemplo_normativo_no_depende_del_tz_del_host(self):
        viejo_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/Bogota"
        try:
            correo = self._tenant(momento=datetime(2026, 10, 2, 21, 6))
            self.assertIn("02/10/2026 · 04:06 p. m. (hora de Colombia)", correo.html_body)
        finally:
            if viejo_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = viejo_tz

    # ---------------------------------------------------------- FR-037

    def test_boton_al_subdominio_del_negocio_en_la_variante_tenant(self):
        correo = self._tenant(url_ingreso="https://granizados.skeilopos.com/login")
        self.assertIn(
            'href="https://granizados.skeilopos.com/login" target="_blank"', correo.html_body
        )

    def test_boton_al_subdominio_de_plataforma_en_la_variante_super_admin(self):
        correo = self._super_admin(url_ingreso="https://admin.skeilopos.com/login")
        self.assertIn('href="https://admin.skeilopos.com/login" target="_blank"', correo.html_body)

    # ---------------------------------------------------------- FR-038

    def test_aviso_de_tenant_dice_contactar_a_un_administrador(self):
        self.assertIn(
            "Contacta de inmediato a un administrador de tu negocio.", self._tenant().html_body
        )

    def test_aviso_de_super_admin_dice_escribir_a_soporte(self):
        self.assertIn("Escribe a skeilo.tech@gmail.com.", self._super_admin().html_body)

    # ---------------------------------------------------------- FR-039

    def test_ninguna_contrasena_en_el_cuerpo(self):
        correo = self._tenant()
        # La entidad no recibe ninguna contraseña como campo (garantía
        # estructural): se confirma que la firma no admite ese parámetro.
        import inspect

        parametros = inspect.signature(construir_correo_contrasena_cambiada).parameters
        self.assertNotIn("contrasena", parametros)
        self.assertNotIn("password", parametros)

    # ---------------------------------------------------------- Consistencia obligatoria

    def test_super_admin_con_nombre_de_negocio_es_invalido(self):
        with self.assertRaises(ErrorDePlantillaDeCorreo):
            self._super_admin(nombre_negocio="Granizados Del Sol")

    def test_tenant_sin_nombre_de_negocio_es_invalido(self):
        with self.assertRaises(ErrorDePlantillaDeCorreo):
            self._tenant(nombre_negocio=None)


if __name__ == "__main__":
    unittest.main()
