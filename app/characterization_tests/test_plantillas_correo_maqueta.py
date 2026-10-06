"""Maqueta y marca compartidas por las tres plantillas (spec 096, FR-001 a
FR-013, FR-049 a FR-054).

Esta primera sección cubre el cuerpo de invitación (US1); las secciones de
restablecimiento (US2, T031) y contraseña cambiada (US3, T042) se añaden en
sus historias, y la sección de conformidad de construcción sobre los tres
cuerpos a la vez (US4, T047) se añade al final.

    python -m unittest app.characterization_tests.test_plantillas_correo_maqueta -v
"""
import re
import unittest

from datetime import datetime

from app.characterization_tests import auth_fixtures as af  # noqa: F401 - fija settings de test
from app.core.mail_templates import (
    construir_correo_contrasena_cambiada,
    construir_correo_invitacion,
    construir_correo_restablecimiento,
)

_CUERPOS_PARA_MAQUETA = {}


def _invitacion():
    if "invitacion" not in _CUERPOS_PARA_MAQUETA:
        _CUERPOS_PARA_MAQUETA["invitacion"] = construir_correo_invitacion(
            nombre_negocio="Granizados Del Sol",
            url_ingreso="https://granizados.skeilopos.com/login",
            correo="maria@correo.com",
            contrasena_temporal="Temporal123!",
            nombre_persona="María Pérez",
        )
    return _CUERPOS_PARA_MAQUETA["invitacion"]


def _restablecimiento():
    if "restablecimiento" not in _CUERPOS_PARA_MAQUETA:
        _CUERPOS_PARA_MAQUETA["restablecimiento"] = construir_correo_restablecimiento(
            url_restablecimiento="https://granizados.skeilopos.com/reset-password?token=abc123",
            minutos_de_vigencia=30,
        )
    return _CUERPOS_PARA_MAQUETA["restablecimiento"]


def _contrasena_cambiada():
    if "contrasena_cambiada" not in _CUERPOS_PARA_MAQUETA:
        _CUERPOS_PARA_MAQUETA["contrasena_cambiada"] = construir_correo_contrasena_cambiada(
            correo="ana@granizados.com",
            momento=datetime(2026, 10, 2, 21, 6),
            url_ingreso="https://granizados.skeilopos.com/login",
            es_super_admin=False,
            nombre_negocio="Granizados Del Sol",
        )
    return _CUERPOS_PARA_MAQUETA["contrasena_cambiada"]


class MaquetaYMarcaInvitacionTests(unittest.TestCase):
    """FR-002: cada valor de la tabla de marca, uno a uno, sobre el cuerpo
    de invitación."""

    def test_fondo_de_pagina(self):
        self.assertIn("#EEF1F6", _invitacion().html_body)

    def test_tarjeta_fondo_borde_y_radio(self):
        html_body = _invitacion().html_body
        self.assertIn("#FFFFFF", html_body)
        self.assertIn("#E2E8F0", html_body)
        self.assertIn("16px", html_body)

    def test_barra_de_acento(self):
        html_body = _invitacion().html_body
        self.assertIn("#7C3AED", html_body)
        self.assertIn("4px", html_body)

    def test_pildora(self):
        html_body = _invitacion().html_body
        self.assertIn("#EDE9FE", html_body)
        self.assertIn("#5B21B6", html_body)
        self.assertIn("999px", html_body)
        self.assertIn("letter-spacing:0.08em", html_body)

    def test_titulo(self):
        html_body = _invitacion().html_body
        self.assertIn("font-size:28px;line-height:34px", html_body)
        self.assertIn("#090D16", html_body)
        self.assertIn("letter-spacing:-0.02em", html_body)

    def test_parrafo(self):
        html_body = _invitacion().html_body
        self.assertIn("font-size:16px;line-height:24px", html_body)
        self.assertIn("#475569", html_body)

    def test_tarjeta_de_datos(self):
        html_body = _invitacion().html_body
        self.assertIn("#F8FAFC", html_body)

    def test_etiqueta_y_valor_de_dato(self):
        html_body = _invitacion().html_body
        self.assertIn("font-size:11px;line-height:16px", html_body)
        self.assertIn("#64748B", html_body)
        self.assertIn("font-size:15px;line-height:22px", html_body)

    def test_boton(self):
        html_body = _invitacion().html_body
        self.assertIn("#7C3AED", html_body)
        self.assertIn("padding:14px 28px", html_body)

    def test_aviso_de_atencion(self):
        html_body = _invitacion().html_body
        self.assertIn("#FFF1F2", html_body)
        self.assertIn("#FECDD3", html_body)
        self.assertIn("#9F1239", html_body)

    def test_color_de_enlaces(self):
        self.assertIn("#6D28D9", _invitacion().html_body)

    def test_pie(self):
        html_body = _invitacion().html_body
        self.assertIn("font-size:13px;line-height:20px", html_body)
        self.assertIn("#64748B", html_body)

    # ---------------------------------------------------------- FR-003

    def test_las_dos_familias_tipograficas(self):
        html_body = _invitacion().html_body
        self.assertIn("Sora, Arial, Helvetica, sans-serif", html_body)
        self.assertIn("Figtree, Arial, Helvetica, sans-serif", html_body)

    # ---------------------------------------------------------- FR-006/FR-007

    def test_url_logo_literal_con_alt_y_ancho(self):
        html_body = _invitacion().html_body
        self.assertIn(
            'src="https://assets.skeilopos.com/skeiloPosResourses/logo-horizontal-violeta-fondo-claro-1200px.png"', html_body
        )
        self.assertIn('alt="SkeiloPOS"', html_body)
        self.assertIn('width="150"', html_body)

    # ---------------------------------------------------------- FR-008/FR-009

    def test_vista_previa_oculta(self):
        html_body = _invitacion().html_body
        self.assertIn("display:none", html_body)
        self.assertIn("max-height:0", html_body)

    # ---------------------------------------------------------- FR-013

    def test_pie_de_diseno_con_contacto(self):
        self.assertIn("skeilo.tech@gmail.com", _invitacion().html_body)

    # ---------------------------------------------------------- FR-049/FR-051

    def test_una_sola_imagen_y_ningun_recurso_externo_ajeno_al_logo(self):
        html_body = _invitacion().html_body
        self.assertEqual(html_body.count("<img"), 1)
        # Ninguna URL externa distinta del dominio de recursos del logo.
        urls = re.findall(r'(?:src|href)="(https?://[^"]+)"', html_body)
        for url in urls:
            self.assertTrue(
                url.startswith("https://assets.skeilopos.com/")
                or url.startswith("https://granizados.skeilopos.com/"),
                f"recurso externo inesperado: {url}",
            )

    # ---------------------------------------------------------- FR-050

    def test_ningun_href_de_redireccion(self):
        html_body = _invitacion().html_body
        for dominio_prohibido in ("bit.ly", "click.", "track.", "r.mail", "redirect"):
            self.assertNotIn(dominio_prohibido, html_body.lower())


class MaquetaYMarcaRestablecimientoTests(unittest.TestCase):
    """FR-001 a FR-013, FR-049 a FR-051 sobre el cuerpo de restablecimiento."""

    def test_valores_de_marca_compartidos(self):
        # Este correo usa el aviso informativo (FR-029), no el de atención
        # (#FFF1F2/#FECDD3/#9F1239 son exclusivos de invitación y
        # contraseña cambiada) — se afirman aquí los tokens que sí aplican.
        html_body = _restablecimiento().html_body
        for valor in (
            "#EEF1F6", "#FFFFFF", "#E2E8F0", "#7C3AED", "#EDE9FE", "#5B21B6",
            "#090D16", "#475569", "#F8FAFC", "#64748B", "#6D28D9",
        ):
            with self.subTest(valor=valor):
                self.assertIn(valor, html_body)

    def test_las_dos_familias_tipograficas(self):
        html_body = _restablecimiento().html_body
        self.assertIn("Sora, Arial, Helvetica, sans-serif", html_body)
        self.assertIn("Figtree, Arial, Helvetica, sans-serif", html_body)

    def test_url_logo_literal_con_alt_y_ancho(self):
        html_body = _restablecimiento().html_body
        self.assertIn(
            'src="https://assets.skeilopos.com/skeiloPosResourses/logo-horizontal-violeta-fondo-claro-1200px.png"', html_body
        )
        self.assertIn('alt="SkeiloPOS"', html_body)
        self.assertIn('width="150"', html_body)

    def test_vista_previa_oculta(self):
        html_body = _restablecimiento().html_body
        self.assertIn("display:none", html_body)
        self.assertIn("max-height:0", html_body)

    def test_pie_con_contacto(self):
        self.assertIn("skeilo.tech@gmail.com", _restablecimiento().html_body)

    def test_una_sola_imagen_y_ningun_recurso_externo_ajeno_al_logo(self):
        html_body = _restablecimiento().html_body
        self.assertEqual(html_body.count("<img"), 1)
        urls = re.findall(r'(?:src|href)="(https?://[^"]+)"', html_body)
        for url in urls:
            self.assertTrue(
                url.startswith("https://assets.skeilopos.com/")
                or url.startswith("https://granizados.skeilopos.com/"),
                f"recurso externo inesperado: {url}",
            )


class MaquetaYMarcaContrasenaCambiadaTests(unittest.TestCase):
    """FR-001 a FR-013, FR-049 a FR-051 sobre el cuerpo de contraseña
    cambiada."""

    def test_valores_de_marca_compartidos(self):
        # Este correo no lleva ningún enlace de texto (solo el botón, en
        # blanco): #6D28D9 (color de enlaces) es exclusivo de invitación y
        # restablecimiento, que sí lo llevan.
        html_body = _contrasena_cambiada().html_body
        for valor in (
            "#EEF1F6", "#FFFFFF", "#E2E8F0", "#7C3AED", "#EDE9FE", "#5B21B6",
            "#090D16", "#475569", "#F8FAFC", "#64748B", "#FFF1F2", "#FECDD3",
            "#9F1239",
        ):
            with self.subTest(valor=valor):
                self.assertIn(valor, html_body)

    def test_las_dos_familias_tipograficas(self):
        html_body = _contrasena_cambiada().html_body
        self.assertIn("Sora, Arial, Helvetica, sans-serif", html_body)
        self.assertIn("Figtree, Arial, Helvetica, sans-serif", html_body)

    def test_url_logo_literal_con_alt_y_ancho(self):
        html_body = _contrasena_cambiada().html_body
        self.assertIn(
            'src="https://assets.skeilopos.com/skeiloPosResourses/logo-horizontal-violeta-fondo-claro-1200px.png"', html_body
        )
        self.assertIn('alt="SkeiloPOS"', html_body)
        self.assertIn('width="150"', html_body)

    def test_vista_previa_oculta(self):
        html_body = _contrasena_cambiada().html_body
        self.assertIn("display:none", html_body)
        self.assertIn("max-height:0", html_body)

    def test_pie_sin_mencion_de_negocio(self):
        html_body = _contrasena_cambiada().html_body
        self.assertIn("skeilo.tech@gmail.com", html_body)
        pie_inicio = html_body.rindex('align="center"')
        self.assertNotIn("Granizados Del Sol", html_body[pie_inicio:])

    def test_una_sola_imagen_y_ningun_recurso_externo_ajeno_al_logo(self):
        html_body = _contrasena_cambiada().html_body
        self.assertEqual(html_body.count("<img"), 1)
        urls = re.findall(r'(?:src|href)="(https?://[^"]+)"', html_body)
        for url in urls:
            self.assertTrue(
                url.startswith("https://assets.skeilopos.com/")
                or url.startswith("https://granizados.skeilopos.com/"),
                f"recurso externo inesperado: {url}",
            )


class ConformidadDeConstruccionTests(unittest.TestCase):
    """US4 (T047): conformidad de construcción sobre los tres cuerpos a la
    vez (FR-005, FR-049, FR-053, FR-054) — la parte que la suite puede
    verificar sin un cliente de correo real. Los clientes de verificación
    diferida (D14) se sostienen por esta técnica, no por un envío."""

    def _cuerpos(self):
        return (_invitacion().html_body, _restablecimiento().html_body, _contrasena_cambiada().html_body)

    def test_ningun_style_con_selectores(self):
        for html_body in self._cuerpos():
            self.assertNotIn("<style", html_body.lower())

    def test_sin_flexbox_ni_grid_ni_consulta_de_medios(self):
        for html_body in self._cuerpos():
            self.assertNotIn("display:flex", html_body)
            self.assertNotIn("display: flex", html_body)
            self.assertNotIn("display:grid", html_body)
            self.assertNotIn("display: grid", html_body)
            self.assertNotIn("@media", html_body)

    def test_cada_elemento_con_fondo_lleva_bgcolor_y_background(self):
        for html_body in self._cuerpos():
            for match in re.finditer(r'bgcolor="(#[0-9A-Fa-f]{6})"', html_body):
                color = match.group(1)
                self.assertIn(f"background:{color}", html_body)
                self.assertIn(f"background-color:{color}", html_body)

    def test_ancho_600_con_max_width_fluido(self):
        for html_body in self._cuerpos():
            self.assertIn('width="600"', html_body)
            self.assertIn("max-width:600px", html_body)

    def test_una_sola_imagen_por_correo_y_ninguna_de_1px(self):
        for html_body in self._cuerpos():
            self.assertEqual(html_body.count("<img"), 1)
            self.assertNotIn('width="1"', html_body)
            self.assertNotIn('height="1"', html_body)

    def test_todo_href_apunta_a_su_destino_final(self):
        # Ningún enlace pasa por un parámetro de seguimiento o redirección
        # ajeno al que el propio constructor generó.
        for html_body in self._cuerpos():
            for href in re.findall(r'href="([^"]+)"', html_body):
                self.assertTrue(href.startswith("https://") or href.startswith("http://"))
                for marcador in ("utm_", "redirect", "click.", "track."):
                    self.assertNotIn(marcador, href.lower())


if __name__ == "__main__":
    unittest.main()
