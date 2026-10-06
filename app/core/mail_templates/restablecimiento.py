"""Correo de restablecimiento de contraseña (spec 096, US2): FR-027 a
FR-032. No menciona ningún negocio ni enlaza a ningún subdominio ajeno
(FR-043) — es el único de los tres correos sin dato de negocio."""
from . import _layout as layout
from . import _marca as marca
from ._campos import escapar_valores, validar_campos_obligatorios

_ASUNTO = "Restablece tu contraseña de SkeiloPOS"


def construir_correo_restablecimiento(*, url_restablecimiento: str, minutos_de_vigencia: int):
    from . import EmailContent

    validar_campos_obligatorios(
        url_restablecimiento=url_restablecimiento,
        minutos_de_vigencia=minutos_de_vigencia,
    )

    e = escapar_valores(url_restablecimiento=url_restablecimiento)
    url_escapada = e["url_restablecimiento"]

    # FR-060: el botón va antes del enlace visible — el primer `token=` del
    # HTML tiene que quedar dentro de un `href="…"` para que
    # test_auth_forgot_password.py y test_auth_reset_password.py sigan
    # extrayendo el token sin tocar su regex.
    cuerpo_html = (
        layout.pildora("SEGURIDAD DE LA CUENTA")
        + layout.titulo("Restablece tu contraseña.")
        + layout.parrafo(
            "Recibimos una solicitud para restablecer la contraseña de tu cuenta. Usa el "
            "botón para elegir una nueva."
        )
        + layout.boton("Restablecer contraseña", url_escapada)
        + layout.enlace_visible(url_escapada)
        + layout.aviso_informativo(
            f"<strong>Válido por {minutos_de_vigencia} minutos.</strong> El enlace solo "
            "puede usarse una vez."
        )
        + layout.parrafo(
            "Si no solicitaste este cambio, puedes ignorarlo: tu contraseña actual sigue "
            "funcionando."
        )
    )

    pie_html = layout.pie(
        f"Este correo fue enviado por {marca.NOMBRE_REMITENTE}. ¿Dudas? Escríbenos a "
        f"{marca.CONTACTO_PIE}."
    )

    preview_html = layout.texto_vista_previa(
        f"Válido por {minutos_de_vigencia} minutos. Restablece tu contraseña de SkeiloPOS."
    )

    html_body = layout.documento(
        texto_vista_previa_html=preview_html, cuerpo_html=cuerpo_html, pie_html=pie_html
    )

    text_body = "\n".join(
        [
            "Restablece tu contraseña.",
            "",
            "Recibimos una solicitud para restablecer la contraseña de tu cuenta. Usa este "
            "enlace para elegir una nueva:",
            "",
            url_restablecimiento,
            "",
            f"Válido por {minutos_de_vigencia} minutos. El enlace solo puede usarse una vez.",
            "",
            "Si no solicitaste este cambio, puedes ignorarlo: tu contraseña actual sigue "
            "funcionando.",
            "",
            f"Este correo fue enviado por {marca.NOMBRE_REMITENTE}. ¿Dudas? Escríbenos a "
            f"{marca.CONTACTO_PIE}.",
        ]
    )

    return EmailContent(subject=_ASUNTO, html_body=html_body, text_body=text_body)
