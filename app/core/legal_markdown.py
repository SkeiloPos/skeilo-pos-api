"""Render de Markdown → HTML para los documentos legales de plataforma y de
tenant (spec 162, research.md Decisión 3). El Markdown crudo es lo único que
se persiste; el HTML se genera en el momento de responder, así un cambio
futuro de reglas de render no exige reprocesar filas existentes.

`markdown_it.MarkdownIt` se instancia a nivel de módulo (no por request) y
sin `html=True`: el contenido que llega aquí ya pasó por la sustitución
literal de variables de `legal_template.py` (FR-011), así que no hay ninguna
razón para permitir HTML embebido sin escapar.
"""
from markdown_it import MarkdownIt

_md = MarkdownIt("commonmark").enable("table")


def render_markdown_to_html(markdown_text: str) -> str:
    """Markdown → HTML, usando siempre la misma instancia compilada."""
    return _md.render(markdown_text)
