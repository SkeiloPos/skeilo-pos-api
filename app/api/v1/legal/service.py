"""Servicio del panel de tenant: perfil legal propio (FR-012) y documento
legal propio (FR-013/FR-014/FR-015) — spec 162, US3. `preview()` no persiste
nada; `publish()` inserta un snapshot inmutable y nunca lo actualiza
después (data-model.md tabla 5)."""
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.legal_template import content_hash, substitute_variables
from app.core.legal_markdown import render_markdown_to_html
from app.core.models import MasterLegalTemplate
from app.models import TenantLegalDocument, TenantLegalProfile

_CAMPOS_PERFIL = (
    "razon_social",
    "identificacion_legal",
    "direccion_legal",
    "ciudad",
    "correo_contacto",
    "telefono_contacto",
)

# `identificacion_legal` (NIT/RUT) es, por ahora, el único dato del perfil
# que no bloquea la generación ni la publicación del documento (spec 162,
# FR-012/FR-012a): el tenant puede publicar sin haberlo ingresado y la línea
# correspondiente se omite del texto renderizado (legal_template.py
# OPTIONAL_TEMPLATE_VARIABLES).
_CAMPOS_OBLIGATORIOS = tuple(
    campo for campo in _CAMPOS_PERFIL if campo != "identificacion_legal"
)


# ------------------------------------------------------------------------- Perfil legal


def get_tenant_legal_profile(db: Session) -> TenantLegalProfile | None:
    return db.execute(select(TenantLegalProfile)).scalar_one_or_none()


def put_tenant_legal_profile(db: Session, **campos) -> TenantLegalProfile:
    """Reemplaza el perfil (FR-012). Escritura parcial: solo los campos
    presentes en `campos` (no `None` por ausencia en el body) se actualizan.
    Crea la fila única si todavía no existe."""
    profile = db.execute(select(TenantLegalProfile)).scalar_one_or_none()
    if profile is None:
        profile = TenantLegalProfile()
        db.add(profile)

    for campo, valor in campos.items():
        setattr(profile, campo, valor)

    db.commit()
    db.refresh(profile)
    return profile


# ------------------------------------------------------------------------- Documento legal del tenant


def _campos_faltantes(profile: TenantLegalProfile | None) -> list[str]:
    if profile is None:
        return list(_CAMPOS_OBLIGATORIOS)
    return [
        campo
        for campo in _CAMPOS_OBLIGATORIOS
        if getattr(profile, campo) in (None, "")
    ]


def _require_complete_profile(db: Session) -> TenantLegalProfile:
    profile = get_tenant_legal_profile(db)
    faltantes = _campos_faltantes(profile)
    if faltantes:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"detail": "Faltan datos legales obligatorios", "campos_faltantes": faltantes},
        )
    return profile


def _get_published_master_template(db: Session) -> MasterLegalTemplate:
    template = db.execute(
        select(MasterLegalTemplate).where(MasterLegalTemplate.estado == "publicado")
    ).scalar_one_or_none()
    if template is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "La plataforma todavía no tiene una plantilla legal maestra publicada",
        )
    return template


# `TenantLegalProfile` guarda el nombre del negocio como `razon_social`
# (data-model.md tabla 4), pero la lista cerrada de variables de la
# plantilla (`legal_template.ALLOWED_TEMPLATE_VARIABLES`, data-model.md
# tabla 2) usa `nombre_negocio` para ese mismo dato — los demás 5 campos
# comparten nombre literal entre perfil y variable.
_CAMPO_A_VARIABLE = {"razon_social": "nombre_negocio"}


def _render_for_profile(template: MasterLegalTemplate, profile: TenantLegalProfile) -> str:
    valores = {
        _CAMPO_A_VARIABLE.get(campo, campo): getattr(profile, campo)
        for campo in _CAMPOS_PERFIL
    }
    return substitute_variables(template.contenido_markdown, valores)


def preview_tenant_legal_document(db: Session) -> tuple[str, int]:
    """Renderiza el texto final con los datos actuales del perfil, sin
    guardar nada (FR-013). 422 con `campos_faltantes` si el perfil no está
    completo."""
    profile = _require_complete_profile(db)
    template = _get_published_master_template(db)
    rendered = _render_for_profile(template, profile)
    return rendered, template.version


def publish_tenant_legal_document(db: Session) -> TenantLegalDocument:
    """Publica el snapshot (FR-014): repite la validación de completitud de
    `preview`, inserta una fila inmutable y transiciona el snapshot
    `publicado` anterior (si existía) a `superado`, en la misma
    transacción."""
    profile = _require_complete_profile(db)
    template = _get_published_master_template(db)
    rendered = _render_for_profile(template, profile)

    previous = db.execute(
        select(TenantLegalDocument).where(TenantLegalDocument.estado == "publicado")
    ).scalar_one_or_none()
    if previous is not None:
        previous.estado = "superado"
        db.flush()

    last_version = db.execute(
        select(TenantLegalDocument.version).order_by(TenantLegalDocument.version.desc()).limit(1)
    ).scalar_one_or_none()

    document = TenantLegalDocument(
        version=(last_version or 0) + 1,
        master_template_id=template.id,
        master_template_version=template.version,
        contenido_renderizado=rendered,
        content_hash=content_hash(rendered),
        estado="publicado",
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    return document


def get_current_tenant_legal_document(db: Session) -> TenantLegalDocument | None:
    return db.execute(
        select(TenantLegalDocument).where(TenantLegalDocument.estado == "publicado")
    ).scalar_one_or_none()


def get_template_update_status(db: Session) -> tuple[bool, int | None, int | None]:
    """FR-015: deriva el banner comparando `master_template_version` del
    snapshot vigente del tenant contra la versión `publicado` vigente de la
    plantilla maestra — no se persiste ningún "aviso pendiente"."""
    current_doc = get_current_tenant_legal_document(db)
    if current_doc is None:
        return False, None, None

    current_template = db.execute(
        select(MasterLegalTemplate).where(MasterLegalTemplate.estado == "publicado")
    ).scalar_one_or_none()
    version_disponible = current_template.version if current_template else None

    pendiente = version_disponible is not None and version_disponible > current_doc.master_template_version
    return pendiente, current_doc.master_template_version, version_disponible


def render_tenant_document_html(document: TenantLegalDocument) -> str:
    return render_markdown_to_html(document.contenido_renderizado)
