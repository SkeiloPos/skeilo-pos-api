"""Correo de invitación (spec 096, US1 + US5): FR-014 a FR-026.

Sirve a la invitación, al reenvío (FR-025) y al alta de un negocio nuevo
(FR-026, D9) — los tres llaman a `construir_correo_invitacion` con los
mismos campos y obtienen el mismo `EmailContent`.
"""
from . import _layout as layout
from . import _marca as marca
from ._campos import escapar_valores, validar_campos_obligatorios

_ASUNTO = "Te invitaron a unirte a {negocio} en SkeiloPOS"

_AVISO_ATENCION = (
    "<strong>Antes de continuar.</strong> Al iniciar sesión por primera vez deberás "
    "fijar tu propia contraseña."
)


def _texto_plano(
    *, negocio: str, url_ingreso: str, correo: str, contrasena_temporal: str, nombre_persona: str | None
) -> str:
    saludo = f"Hola, {nombre_persona}." if nombre_persona else "Hola."
    lineas = [
        f"Te invitaron a unirte a {negocio}.",
        "",
        f"{saludo} Un administrador de {negocio} te dio acceso al sistema. "
        "Estos son tus datos de acceso:",
        "",
        "URL DE INICIO DE SESIÓN:",
        url_ingreso,
        "",
        f"USUARIO: {correo}",
        f"CONTRASEÑA TEMPORAL: {contrasena_temporal}",
        "",
        "Antes de continuar: al iniciar sesión por primera vez deberás fijar tu propia "
        "contraseña.",
        "",
        f"Este correo fue enviado por {marca.NOMBRE_REMITENTE} en nombre de {negocio}. "
        f"¿Dudas? Escríbenos a {marca.CONTACTO_PIE}.",
    ]
    return "\n".join(lineas)


def construir_correo_invitacion(
    *,
    nombre_negocio: str,
    url_ingreso: str,
    correo: str,
    contrasena_temporal: str,
    nombre_persona: str | None = None,
):
    from . import EmailContent

    validar_campos_obligatorios(
        nombre_negocio=nombre_negocio,
        url_ingreso=url_ingreso,
        correo=correo,
        contrasena_temporal=contrasena_temporal,
    )

    nombre_persona_normalizado = (
        nombre_persona.strip() if nombre_persona and nombre_persona.strip() else None
    )

    crudos = {
        "nombre_negocio": nombre_negocio,
        "nombre_negocio_mayus": nombre_negocio.upper(),
        "url_ingreso": url_ingreso,
        "correo": correo,
        "contrasena_temporal": contrasena_temporal,
    }
    if nombre_persona_normalizado:
        crudos["nombre_persona"] = nombre_persona_normalizado
    e = escapar_valores(**crudos)

    saludo_html = f"Hola, {e['nombre_persona']}." if nombre_persona_normalizado else "Hola."

    enlace_ingreso_html = (
        f'<a href="{e["url_ingreso"]}" style="color:{marca.COLOR_ENLACE};'
        f'text-decoration:underline;">{e["url_ingreso"]}</a>'
    )

    cuerpo_html = (
        layout.pildora(f"INVITACIÓN · {e['nombre_negocio_mayus']}")
        + layout.titulo(f"Te invitaron a unirte a {e['nombre_negocio']}.")
        + layout.parrafo(
            f"{saludo_html} Un administrador de {e['nombre_negocio']} te dio acceso al "
            "sistema. Estos son tus datos de acceso:"
        )
        + layout.tarjeta_datos(
            layout.fila_dato("URL DE INICIO DE SESIÓN", enlace_ingreso_html, primera=True)
            + layout.fila_dato("USUARIO", e["correo"])
            + layout.fila_dato(
                "CONTRASEÑA TEMPORAL", layout.caja_contrasena(e["contrasena_temporal"])
            )
        )
        + layout.boton("Iniciar sesión", e["url_ingreso"])
        + layout.aviso_atencion(_AVISO_ATENCION)
    )

    pie_html = layout.pie(
        f"Este correo fue enviado por {marca.NOMBRE_REMITENTE} en nombre de "
        f"{e['nombre_negocio']}. ¿Dudas? Escríbenos a {marca.CONTACTO_PIE}."
    )

    preview_html = layout.texto_vista_previa(
        f"Te invitaron a unirte a {e['nombre_negocio']} en SkeiloPOS."
    )

    html_body = layout.documento(
        texto_vista_previa_html=preview_html, cuerpo_html=cuerpo_html, pie_html=pie_html
    )

    text_body = _texto_plano(
        negocio=nombre_negocio,
        url_ingreso=url_ingreso,
        correo=correo,
        contrasena_temporal=contrasena_temporal,
        nombre_persona=nombre_persona_normalizado,
    )

    return EmailContent(
        subject=_ASUNTO.format(negocio=nombre_negocio), html_body=html_body, text_body=text_body
    )
