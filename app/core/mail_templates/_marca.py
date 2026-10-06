"""Constantes de marca compartidas por las tres plantillas (spec 096,
FR-002, FR-003, FR-006, FR-011, FR-013). Ningún valor de este módulo varía
por entorno: es la identidad visual y de contacto del producto, no un
ajuste de configuración — en particular `URL_LOGO`, que FR-006 exige igual
en todos los entornos (research.md Decisión 4)."""

# Fondo de página
COLOR_FONDO_PAGINA = "#EEF1F6"

# Tarjeta
COLOR_FONDO_TARJETA = "#FFFFFF"
COLOR_BORDE_TARJETA = "#E2E8F0"
RADIO_TARJETA = "16px"

# Barra de acento superior
COLOR_ACENTO = "#7C3AED"
ALTO_BARRA_ACENTO = "4px"

# Píldora (etiqueta)
COLOR_FONDO_PILDORA = "#EDE9FE"
COLOR_TEXTO_PILDORA = "#5B21B6"

# Título
COLOR_TITULO = "#090D16"

# Párrafo
COLOR_PARRAFO = "#475569"

# Tarjeta de datos
COLOR_FONDO_TARJETA_DATOS = "#F8FAFC"
COLOR_BORDE_TARJETA_DATOS = "#E2E8F0"
COLOR_ETIQUETA_DATO = "#64748B"
COLOR_VALOR_DATO = "#090D16"

# Botón
COLOR_BOTON = "#7C3AED"

# Aviso de atención
COLOR_FONDO_AVISO_ATENCION = "#FFF1F2"
COLOR_BORDE_AVISO_ATENCION = "#FECDD3"
COLOR_TEXTO_AVISO_ATENCION = "#9F1239"

# Aviso informativo
COLOR_FONDO_AVISO_INFORMATIVO = "#F8FAFC"
COLOR_BORDE_AVISO_INFORMATIVO = "#E2E8F0"
COLOR_TEXTO_AVISO_INFORMATIVO = "#475569"

# Enlaces
COLOR_ENLACE = "#6D28D9"

# Pie
COLOR_PIE = "#64748B"

# Tipografías (FR-003/FR-004): cadena de reserva completa — Arial/Helvetica
# sostienen tamaño, peso, color y espaciado donde Sora/Figtree no carguen.
FUENTE_TITULOS = "Sora, Arial, Helvetica, sans-serif"
FUENTE_TEXTO = "Figtree, Arial, Helvetica, sans-serif"
FUENTE_CONTRASENA = "'Courier New', Courier, monospace"

# Ancho y relleno del contenido (FR-002)
ANCHO_CONTENIDO = 600
RELLENO_VERTICAL = "40px"
RELLENO_HORIZONTAL = "16px"

# Logo (D12, FR-006): literal del módulo. Nunca se deriva de `settings` ni
# de `ASSETS_BASE_URL` — esa URL sí varía por entorno y FR-006 exige que la
# del logo no varíe. Clave real del objeto subido al bucket de producción
# (T003, 2026-10-06): no es la clave `brand/...` que plan.md anticipaba,
# sino la ruta bajo la que el archivo quedó publicado en el bucket
# `resources` (prefijo `skeiloPosResourses/`, nombre de archivo original
# del sistema de diseño) — verificado con `curl -I`, responde 200
# image/png.
URL_LOGO = "https://assets.skeilopos.com/skeiloPosResourses/logo-horizontal-violeta-fondo-claro-1200px.png"
ANCHO_LOGO = 150
ALT_LOGO = "SkeiloPOS"

# Contacto y remitente (FR-011, FR-013, FR-038, A-114)
CONTACTO_PIE = "skeilo.tech@gmail.com"
NOMBRE_REMITENTE = "SkeiloPOS"
