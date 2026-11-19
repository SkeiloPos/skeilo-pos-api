"""Correo de aviso de nueva versión de la plantilla legal maestra (spec 162,
FR-015, research.md Decisión 10). Se dispara una sola vez, al publicar la
nueva versión de la plantilla, hacia cada tenant cuyo documento legal
publicado quedó en una versión de plantilla anterior — no en cada carga de
página (el banner del panel se deriva, no se persiste)."""
from . import _layout as layout
from . import _marca as marca
from ._campos import escapar_valores, validar_campos_obligatorios

_ASUNTO = "Hay una nueva versión de la plantilla legal disponible"


def construir_correo_plantilla_actualizada(
    *, nombre_negocio: str, version_nueva: int, url_panel_legal: str,
):
    from . import EmailContent

    validar_campos_obligatorios(
        nombre_negocio=nombre_negocio, url_panel_legal=url_panel_legal,
    )

    e = escapar_valores(
        nombre_negocio=nombre_negocio, url_panel_legal=url_panel_legal,
    )

    cuerpo_html = (
        layout.pildora("DOCUMENTOS LEGALES")
        + layout.titulo("Hay una nueva versión de la plantilla legal disponible.")
        + layout.parrafo(
            f"Hola, {e['nombre_negocio']}. La plataforma publicó la versión "
            f"{version_nueva} de la plantilla legal maestra. Tu documento legal "
            "publicado sigue vigente tal como está, pero puedes revisarlo y "
            "volver a publicarlo con el contenido actualizado cuando quieras."
        )
        + layout.boton("Revisar mi documento legal", e["url_panel_legal"])
        + layout.aviso_informativo(
            "Esto no cambia tu documento ya publicado: solo se actualiza si "
            "tú decides volver a publicarlo."
        )
    )

    pie_html = layout.pie(
        f"Este correo fue enviado por {marca.NOMBRE_REMITENTE}. ¿Dudas? Escríbenos a "
        f"{marca.CONTACTO_PIE}."
    )

    preview_html = layout.texto_vista_previa(
        f"Nueva versión ({version_nueva}) de la plantilla legal maestra disponible."
    )

    html_body = layout.documento(
        texto_vista_previa_html=preview_html, cuerpo_html=cuerpo_html, pie_html=pie_html
    )

    text_body = "\n".join(
        [
            "Hay una nueva versión de la plantilla legal disponible.",
            "",
            f"Hola, {nombre_negocio}. La plataforma publicó la versión {version_nueva} de "
            "la plantilla legal maestra. Tu documento legal publicado sigue vigente tal "
            "como está, pero puedes revisarlo y volver a publicarlo con el contenido "
            "actualizado cuando quieras.",
            "",
            url_panel_legal,
            "",
            f"Este correo fue enviado por {marca.NOMBRE_REMITENTE}. ¿Dudas? Escríbenos a "
            f"{marca.CONTACTO_PIE}.",
        ]
    )

    return EmailContent(subject=_ASUNTO, html_body=html_body, text_body=text_body)
