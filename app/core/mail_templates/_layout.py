"""Piezas de maqueta compartidas por las tres plantillas HTML (spec 096,
FR-001, FR-002, FR-005, FR-049, FR-053, FR-054; research.md Decisión 11).

Tablas de presentación anidadas, estilos en línea, `bgcolor` **y**
`background` en cada elemento con fondo, ancho fijo + `max-width` fluido:
la técnica que ya usa el diseño aprobado y la única que Outlook de
escritorio soporta sin flexbox, grid ni `<style>` con selectores. Ningún
requisito depende de que el `<head>` sobreviva al transporte.
"""
from . import _marca as m


def texto_vista_previa(texto_escapado: str) -> str:
    """FR-008/FR-009: oculto en el cuerpo, visible solo como vista previa
    de la bandeja de entrada en los clientes que la interpretan."""
    return (
        '<div style="display:none;max-height:0;overflow:hidden;opacity:0;'
        f'font-size:1px;line-height:1px;color:{m.COLOR_FONDO_PAGINA};">'
        f"{texto_escapado}&nbsp;"
        "</div>"
    )


def pildora(texto_html: str) -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:collapse;">'
        "<tr><td "
        f'style="background-color:{m.COLOR_FONDO_PILDORA};background:{m.COLOR_FONDO_PILDORA};'
        "border-radius:999px;padding:6px 14px;"
        f'font-family:{m.FUENTE_TEXTO};font-size:12px;font-weight:700;'
        f'color:{m.COLOR_TEXTO_PILDORA};letter-spacing:0.08em;text-transform:uppercase;" '
        f'bgcolor="{m.COLOR_FONDO_PILDORA}">{texto_html}</td></tr></table>'
    )


def titulo(texto_html: str) -> str:
    return (
        '<p style="margin:16px 0 0 0;'
        f'font-family:{m.FUENTE_TITULOS};font-size:28px;line-height:34px;font-weight:700;'
        f'color:{m.COLOR_TITULO};letter-spacing:-0.02em;">{texto_html}</p>'
    )


def parrafo(html_interno: str) -> str:
    return (
        '<p style="margin:16px 0 0 0;'
        f'font-family:{m.FUENTE_TEXTO};font-size:16px;line-height:24px;color:{m.COLOR_PARRAFO};">'
        f"{html_interno}</p>"
    )


def fila_dato(etiqueta: str, valor_html: str, *, primera: bool = False) -> str:
    relleno_superior = "0" if primera else "16px"
    return (
        f'<tr><td style="padding:{relleno_superior} 0 0 0;">'
        f'<div style="font-family:{m.FUENTE_TEXTO};font-size:11px;line-height:16px;font-weight:700;'
        f'color:{m.COLOR_ETIQUETA_DATO};text-transform:uppercase;letter-spacing:0.08em;">{etiqueta}</div>'
        f'<div style="font-family:{m.FUENTE_TEXTO};font-size:15px;line-height:22px;'
        f'color:{m.COLOR_VALOR_DATO};margin-top:4px;word-break:break-word;">{valor_html}</div>'
        "</td></tr>"
    )


def tarjeta_datos(filas_html: str) -> str:
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="border-collapse:collapse;margin-top:24px;background-color:{m.COLOR_FONDO_TARJETA_DATOS};'
        f'background:{m.COLOR_FONDO_TARJETA_DATOS};border:1px solid {m.COLOR_BORDE_TARJETA_DATOS};'
        f'border-radius:12px;" bgcolor="{m.COLOR_FONDO_TARJETA_DATOS}">'
        '<tr><td style="padding:20px 20px 16px 20px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="border-collapse:collapse;">{filas_html}</table>'
        "</td></tr></table>"
    )


def caja_contrasena(valor_escapado: str) -> str:
    """FR-017: borde discontinuo de acento, monoespaciada 18/24px,
    `letter-spacing:0.06em`."""
    return (
        '<div style="margin-top:4px;display:inline-block;'
        f'border:1px dashed {m.COLOR_ACENTO};border-radius:8px;padding:10px 14px;'
        f'font-family:{m.FUENTE_CONTRASENA};font-size:18px;line-height:24px;letter-spacing:0.06em;'
        f'color:{m.COLOR_VALOR_DATO};">{valor_escapado}</div>'
    )


def boton(texto: str, href_escapado: str) -> str:
    """Celda con fondo y un `<a style="display:block">` dentro, tal como
    el diseño (research.md Decisión 11)."""
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:collapse;margin-top:24px;">'
        '<tr><td style="border-radius:8px;'
        f'background-color:{m.COLOR_BOTON};background:{m.COLOR_BOTON};" bgcolor="{m.COLOR_BOTON}">'
        f'<a href="{href_escapado}" target="_blank" '
        f'style="display:block;padding:14px 28px;font-family:{m.FUENTE_TEXTO};font-size:16px;'
        "line-height:20px;font-weight:700;color:#FFFFFF;text-decoration:none;border-radius:8px;\">"
        f"{texto}</a></td></tr></table>"
    )


def enlace_visible(href_escapado: str) -> str:
    """FR-031/A-116: el enlace completo, seleccionable, para quien no
    pueda usar el botón. El texto es idéntico al `href` (mismo valor
    escapado en los dos sitios)."""
    return (
        '<p style="margin:16px 0 0 0;word-break:break-all;'
        f'font-family:{m.FUENTE_TEXTO};font-size:14px;line-height:20px;">'
        f'<a href="{href_escapado}" style="color:{m.COLOR_ENLACE};text-decoration:underline;">'
        f"{href_escapado}</a></p>"
    )


def _aviso(html_interno: str, *, fondo: str, borde: str, texto: str) -> str:
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="border-collapse:collapse;margin-top:24px;background-color:{fondo};background:{fondo};'
        f'border:1px solid {borde};border-radius:12px;" bgcolor="{fondo}">'
        f'<tr><td style="padding:16px 20px;font-family:{m.FUENTE_TEXTO};font-size:14px;line-height:20px;'
        f'color:{texto};">{html_interno}</td></tr></table>'
    )


def aviso_atencion(html_interno: str) -> str:
    return _aviso(
        html_interno,
        fondo=m.COLOR_FONDO_AVISO_ATENCION,
        borde=m.COLOR_BORDE_AVISO_ATENCION,
        texto=m.COLOR_TEXTO_AVISO_ATENCION,
    )


def aviso_informativo(html_interno: str) -> str:
    return _aviso(
        html_interno,
        fondo=m.COLOR_FONDO_AVISO_INFORMATIVO,
        borde=m.COLOR_BORDE_AVISO_INFORMATIVO,
        texto=m.COLOR_TEXTO_AVISO_INFORMATIVO,
    )


def pie(html_interno: str) -> str:
    return (
        '<tr><td align="center" style="padding:24px 16px 0 16px;'
        f'font-family:{m.FUENTE_TEXTO};font-size:13px;line-height:20px;color:{m.COLOR_PIE};">'
        f"{html_interno}</td></tr>"
    )


def documento(*, texto_vista_previa_html: str, cuerpo_html: str, pie_html: str) -> str:
    """Documento completo: `<!DOCTYPE html>`, `<head>` que nada depende de
    que sobreviva, y la tarjeta de 600px centrada con barra de acento,
    logo único y pie fuera de la tarjeta."""
    ancho = m.ANCHO_CONTENIDO
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{m.NOMBRE_REMITENTE}</title>
</head>
<body style="margin:0;padding:0;background-color:{m.COLOR_FONDO_PAGINA};background:{m.COLOR_FONDO_PAGINA};" bgcolor="{m.COLOR_FONDO_PAGINA}">
{texto_vista_previa_html}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;background-color:{m.COLOR_FONDO_PAGINA};background:{m.COLOR_FONDO_PAGINA};" bgcolor="{m.COLOR_FONDO_PAGINA}">
<tr><td align="center" style="padding:{m.RELLENO_VERTICAL} {m.RELLENO_HORIZONTAL};">
<table role="presentation" width="{ancho}" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;width:{ancho}px;max-width:{ancho}px;">
<tr><td style="height:{m.ALTO_BARRA_ACENTO};line-height:{m.ALTO_BARRA_ACENTO};font-size:0;background-color:{m.COLOR_ACENTO};background:{m.COLOR_ACENTO};border-radius:{m.RADIO_TARJETA} {m.RADIO_TARJETA} 0 0;" bgcolor="{m.COLOR_ACENTO}">&nbsp;</td></tr>
<tr>
<td style="background-color:{m.COLOR_FONDO_TARJETA};background:{m.COLOR_FONDO_TARJETA};border:1px solid {m.COLOR_BORDE_TARJETA};border-top:none;border-radius:0 0 {m.RADIO_TARJETA} {m.RADIO_TARJETA};padding:32px;" bgcolor="{m.COLOR_FONDO_TARJETA}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
<tr><td style="padding-bottom:24px;">
<img src="{m.URL_LOGO}" width="{m.ANCHO_LOGO}" alt="{m.ALT_LOGO}" style="display:block;width:{m.ANCHO_LOGO}px;max-width:{m.ANCHO_LOGO}px;height:auto;border:0;outline:none;">
</td></tr>
<tr><td>{cuerpo_html}</td></tr>
</table>
</td>
</tr>
</table>
<table role="presentation" width="{ancho}" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;width:{ancho}px;max-width:{ancho}px;">
{pie_html}
</table>
</td></tr>
</table>
</body>
</html>"""
