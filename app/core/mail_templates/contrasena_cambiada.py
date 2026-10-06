"""Correo de aviso de contraseña cambiada (spec 096, US3): FR-033 a FR-039.

Única de las tres plantillas con dos variantes: usuario de tenant (tres
filas, aviso de contactar a un administrador) y Super Admin (dos filas,
sin negocio, aviso de escribir a soporte). `es_super_admin == True` ⟺
`nombre_negocio is None`: las dos combinaciones cruzadas son un error de
programación y se rechazan (data-model.md → Consistencia obligatoria).
"""
from datetime import datetime

from . import _layout as layout
from . import _marca as marca
from ._campos import ErrorDePlantillaDeCorreo, escapar_valores, formatear_fecha_hora_colombia, validar_campos_obligatorios

_ASUNTO = "Tu contraseña de SkeiloPOS fue cambiada"

_AVISO_TENANT = "<strong>Si no fuiste tú.</strong> Contacta de inmediato a un administrador de tu negocio."


def _aviso_super_admin() -> str:
    return f"<strong>Si no fuiste tú.</strong> Escribe a {marca.CONTACTO_PIE}."


def construir_correo_contrasena_cambiada(
    *,
    correo: str,
    momento: datetime,
    url_ingreso: str,
    es_super_admin: bool,
    nombre_negocio: str | None = None,
):
    from . import EmailContent

    if momento is None:
        raise ErrorDePlantillaDeCorreo("Faltan campos obligatorios: momento")

    validar_campos_obligatorios(correo=correo, url_ingreso=url_ingreso)

    nombre_negocio_normalizado = (
        nombre_negocio.strip() if nombre_negocio and nombre_negocio.strip() else None
    )

    if es_super_admin and nombre_negocio_normalizado is not None:
        raise ErrorDePlantillaDeCorreo(
            "Combinación inválida: es_super_admin=True con nombre_negocio presente"
        )
    if not es_super_admin and nombre_negocio_normalizado is None:
        raise ErrorDePlantillaDeCorreo("Faltan campos obligatorios: nombre_negocio")

    crudos = {"correo": correo, "url_ingreso": url_ingreso}
    if nombre_negocio_normalizado:
        crudos["nombre_negocio"] = nombre_negocio_normalizado
    e = escapar_valores(**crudos)

    fecha_hora = formatear_fecha_hora_colombia(momento)

    filas_html = layout.fila_dato("CUENTA", e["correo"], primera=True)
    if not es_super_admin:
        filas_html += layout.fila_dato("NEGOCIO", e["nombre_negocio"])
    filas_html += layout.fila_dato("FECHA Y HORA", fecha_hora)

    aviso_html = _aviso_super_admin() if es_super_admin else _AVISO_TENANT

    cuerpo_html = (
        layout.pildora("CONTRASEÑA ACTUALIZADA")
        + layout.titulo("Tu contraseña fue cambiada.")
        + layout.parrafo(
            "Tu contraseña se actualizó correctamente. Estos son los detalles del cambio:"
        )
        + layout.tarjeta_datos(filas_html)
        + layout.boton("Iniciar sesión", e["url_ingreso"])
        + layout.aviso_atencion(aviso_html)
    )

    pie_html = layout.pie(
        f"Este correo fue enviado por {marca.NOMBRE_REMITENTE}. ¿Dudas? Escríbenos a "
        f"{marca.CONTACTO_PIE}."
    )

    preview_html = layout.texto_vista_previa(f"Tu contraseña de SkeiloPOS fue cambiada el {fecha_hora}.")

    html_body = layout.documento(
        texto_vista_previa_html=preview_html, cuerpo_html=cuerpo_html, pie_html=pie_html
    )

    lineas = [
        "Tu contraseña fue cambiada.",
        "",
        "Tu contraseña se actualizó correctamente. Estos son los detalles del cambio:",
        "",
        f"CUENTA: {correo}",
    ]
    if not es_super_admin:
        lineas.append(f"NEGOCIO: {nombre_negocio_normalizado}")
    lineas.append(f"FECHA Y HORA: {fecha_hora}")
    lineas += ["", url_ingreso, ""]
    lineas.append(
        f"Si no fuiste tú, escribe a {marca.CONTACTO_PIE}."
        if es_super_admin
        else "Si no fuiste tú, contacta de inmediato a un administrador de tu negocio."
    )
    lineas += [
        "",
        f"Este correo fue enviado por {marca.NOMBRE_REMITENTE}. ¿Dudas? Escríbenos a "
        f"{marca.CONTACTO_PIE}.",
    ]
    text_body = "\n".join(lineas)

    return EmailContent(subject=_ASUNTO, html_body=html_body, text_body=text_body)
