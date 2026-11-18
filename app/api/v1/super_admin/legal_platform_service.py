"""Servicio de super admin para documentos legales de plataforma (spec 162,
US1) y plantilla legal maestra (US2). Misma máquina de estados
borrador/publicado/superado para ambas entidades, aplicada aquí a nivel de
servicio (FR-003): ningún `UPDATE` de contenido se permite fuera de
`estado='borrador'`, y el índice único parcial de cada tabla es la barrera
real contra dos publicaciones concurrentes (FR-004) — este servicio solo
traduce ese choque en un 409 legible."""
import logging
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import mail
from app.core.config import settings
from app.core.db import with_db
from app.core.legal_markdown import render_markdown_to_html
from app.core.legal_template import content_hash, validate_variables
from app.core.mail_templates import EmailContent
from app.core.mail_templates.plantilla_actualizada import construir_correo_plantilla_actualizada
from app.core.models import MasterLegalTemplate, PlatformLegalDocument, Tenant
from app.core.timezone import utc_now_naive
from app.models import TenantLegalDocument, TenantLegalProfile

# --------------------------------------------------------------- Documentos de plataforma


def create_platform_document_draft(
    db: Session, *, tipo: str, contenido_markdown: str,
) -> PlatformLegalDocument:
    """Crea un borrador nuevo (FR-001). `version` = última versión de ese
    `tipo` + 1 (1 si no existe ninguna)."""
    last_version = db.execute(
        select(PlatformLegalDocument.version)
        .where(PlatformLegalDocument.tipo == tipo)
        .order_by(PlatformLegalDocument.version.desc())
        .limit(1)
    ).scalar_one_or_none()

    doc = PlatformLegalDocument(
        tipo=tipo,
        version=(last_version or 0) + 1,
        contenido_markdown=contenido_markdown,
        estado="borrador",
        content_hash=content_hash(contenido_markdown),
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def _get_platform_document_or_404(db: Session, document_id: UUID) -> PlatformLegalDocument:
    doc = db.get(PlatformLegalDocument, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese documento legal")
    return doc


def render_platform_document(db: Session, document_id: UUID) -> str:
    """Vista previa admin: renderiza cualquier versión (borrador, publicada o
    superada) a HTML, sin exigir que esté publicada (eso es lo que ya hace el
    endpoint público). Reutiliza el mismo renderer que `public_router.py`."""
    doc = _get_platform_document_or_404(db, document_id)
    return render_markdown_to_html(doc.contenido_markdown)


def edit_platform_document_draft(
    db: Session, document_id: UUID, *, contenido_markdown: str
) -> PlatformLegalDocument:
    """Edita el `contenido_markdown` de un borrador (FR-001). 409 si ya no es
    un borrador (FR-003)."""
    doc = _get_platform_document_or_404(db, document_id)
    if doc.estado != "borrador":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Solo se puede editar un documento en estado borrador",
        )
    doc.contenido_markdown = contenido_markdown
    doc.content_hash = content_hash(contenido_markdown)
    db.commit()
    db.refresh(doc)
    return doc


def publish_platform_document(
    db: Session, document_id: UUID, *, fecha_vigencia: datetime, published_by_user_id: UUID
) -> PlatformLegalDocument:
    """Publica la versión (FR-002/FR-003/FR-004): dentro de la misma
    transacción, la versión `publicado` previa del mismo `tipo` (si existía)
    pasa a `superado` y esta pasa a `publicado`. Una carrera de publicación
    concurrente del mismo `tipo` choca contra el índice único parcial y se
    traduce en 409."""
    doc = _get_platform_document_or_404(db, document_id)
    if doc.estado != "borrador":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Solo se puede publicar un documento en estado borrador",
        )

    try:
        previous = db.execute(
            select(PlatformLegalDocument).where(
                PlatformLegalDocument.tipo == doc.tipo,
                PlatformLegalDocument.estado == "publicado",
            )
        ).scalar_one_or_none()
        if previous is not None:
            previous.estado = "superado"
            db.flush()

        doc.estado = "publicado"
        doc.fecha_vigencia = fecha_vigencia
        doc.published_by_user_id = published_by_user_id
        doc.published_at = utc_now_naive()
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Otra publicación de este tipo de documento ganó la carrera; recarga e inténtalo de nuevo",
        )

    db.refresh(doc)
    return doc


def get_platform_document_history(db: Session, tipo: str) -> list[PlatformLegalDocument]:
    """Historial completo, incluyendo versiones superadas (FR-007)."""
    return list(
        db.execute(
            select(PlatformLegalDocument)
            .where(PlatformLegalDocument.tipo == tipo)
            .order_by(PlatformLegalDocument.version.desc())
        ).scalars()
    )


def delete_platform_document_draft(db: Session, document_id: UUID) -> None:
    """Elimina un borrador. 409 si la fila no está en `estado='borrador'` —
    una versión publicada o superada nunca se puede borrar, rompería el
    rastro de auditoría que FR-003/FR-004/FR-007 dan por sentado."""
    doc = _get_platform_document_or_404(db, document_id)
    if doc.estado != "borrador":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Solo se puede eliminar un documento en estado borrador",
        )
    db.delete(doc)
    db.commit()


# --------------------------------------------------------------------- Plantilla legal maestra


def create_master_template_draft(db: Session, *, contenido_markdown: str) -> MasterLegalTemplate:
    """Crea un borrador nuevo de la plantilla maestra (FR-008). `version` =
    última versión + 1 (1 si no existe ninguna).

    No valida la lista cerrada de variables aquí a propósito: el frontend
    pre-llena un borrador nuevo con el contenido de la versión anterior, que
    puede incluir una variable que en su momento era válida y luego se quitó
    de `ALLOWED_TEMPLATE_VARIABLES` — si create también validara, ese
    contenido histórico dejaría el borrador imposible de crear (ni siquiera
    se podría abrir el editor para corregirlo). FR-009 (lista cerrada) se
    sigue aplicando igual en `edit_master_template_draft` (al guardar un
    cambio intencional) y en `publish_master_template` (antes de que el
    contenido quede visible a un tenant), que es donde de verdad importa."""
    last_version = db.execute(
        select(MasterLegalTemplate.version).order_by(MasterLegalTemplate.version.desc()).limit(1)
    ).scalar_one_or_none()

    template = MasterLegalTemplate(
        version=(last_version or 0) + 1,
        contenido_markdown=contenido_markdown,
        estado="borrador",
        content_hash=content_hash(contenido_markdown),
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


def _get_master_template_or_404(db: Session, template_id: UUID) -> MasterLegalTemplate:
    template = db.get(MasterLegalTemplate, template_id)
    if template is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe esa plantilla legal")
    return template


def render_master_template(db: Session, template_id: UUID) -> str:
    """Vista previa admin de cualquier versión de la plantilla, con los
    placeholders `{{variable}}` tal cual (sin datos de un tenant real que
    sustituir aquí — eso solo existe en `legal/service.py::preview`)."""
    template = _get_master_template_or_404(db, template_id)
    return render_markdown_to_html(template.contenido_markdown)


def edit_master_template_draft(
    db: Session, template_id: UUID, *, contenido_markdown: str
) -> MasterLegalTemplate:
    """Edita el `contenido_markdown` de un borrador (FR-008). 409 si ya no es
    un borrador; 422 si hay variables no permitidas (FR-009)."""
    template = _get_master_template_or_404(db, template_id)
    if template.estado != "borrador":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Solo se puede editar una plantilla en estado borrador",
        )

    variables_no_permitidas = validate_variables(contenido_markdown)
    if variables_no_permitidas:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {
                "detail": "Variables no permitidas en la plantilla",
                "variables_no_permitidas": variables_no_permitidas,
            },
        )

    template.contenido_markdown = contenido_markdown
    template.content_hash = content_hash(contenido_markdown)
    db.commit()
    db.refresh(template)
    return template


def _build_login_url_for_tenant_legal_panel(tenant: Tenant) -> str:
    path = "/settings/legal"
    if settings.ENVIRONMENT == "prod":
        return f"https://{tenant.host}.skeilopos.com{path}"
    return f"http://{tenant.host}.localhost:4200{path}"


def _notify_tenants_of_template_update(db: Session, *, version_nueva: int) -> None:
    """FR-015/Decisión 10: para cada tenant cuyo `TenantLegalDocument`
    publicado quedó en una versión de plantilla anterior a `version_nueva`,
    envía el correo una sola vez (en este momento de publicar), sin tocar
    ninguna fila de `tenant_legal_documents`. No bloquea la publicación si
    un envío individual falla: se registra y se sigue con el resto."""
    tenants = list(db.execute(select(Tenant)).scalars())
    for tenant in tenants:
        try:
            with with_db(tenant.schema) as tenant_db:
                outdated_doc = tenant_db.execute(
                    select(TenantLegalDocument).where(
                        TenantLegalDocument.estado == "publicado",
                        TenantLegalDocument.master_template_version < version_nueva,
                    )
                ).scalar_one_or_none()
                if outdated_doc is None:
                    continue

                profile = tenant_db.execute(select(TenantLegalProfile)).scalar_one_or_none()
                destinatario = profile.correo_contacto if profile else None
            if not destinatario:
                continue

            correo: EmailContent = construir_correo_plantilla_actualizada(
                nombre_negocio=tenant.name,
                version_nueva=version_nueva,
                url_panel_legal=_build_login_url_for_tenant_legal_panel(tenant),
            )
            mail.send_email(
                mail.create_message(
                    [destinatario], correo.subject, correo.html_body, text_body=correo.text_body,
                )
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "No se pudo enviar el aviso de plantilla actualizada al tenant %s", tenant.id,
            )


def publish_master_template(
    db: Session, template_id: UUID, *, fecha_vigencia: datetime, published_by_user_id: UUID
) -> MasterLegalTemplate:
    """Publica la versión (FR-008/FR-009/FR-010): dentro de la misma
    transacción, la versión `publicado` previa (si existía) pasa a
    `superado` y esta pasa a `publicado`. No recalcula ningún
    `TenantLegalDocument` existente (FR-010: inmutabilidad del snapshot del
    tenant) — solo encola el aviso por correo de FR-015."""
    template = _get_master_template_or_404(db, template_id)
    if template.estado != "borrador":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Solo se puede publicar una plantilla en estado borrador",
        )

    variables_no_permitidas = validate_variables(template.contenido_markdown)
    if variables_no_permitidas:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {
                "detail": "Variables no permitidas en la plantilla",
                "variables_no_permitidas": variables_no_permitidas,
            },
        )

    try:
        previous = db.execute(
            select(MasterLegalTemplate).where(MasterLegalTemplate.estado == "publicado")
        ).scalar_one_or_none()
        if previous is not None:
            previous.estado = "superado"
            db.flush()

        template.estado = "publicado"
        template.fecha_vigencia = fecha_vigencia
        template.published_by_user_id = published_by_user_id
        template.published_at = utc_now_naive()
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Otra publicación de la plantilla ganó la carrera; recarga e inténtalo de nuevo",
        )

    db.refresh(template)
    _notify_tenants_of_template_update(db, version_nueva=template.version)
    return template


def get_master_template_history(db: Session) -> list[MasterLegalTemplate]:
    """Historial completo de la plantilla maestra (mismo patrón que
    documentos de plataforma)."""
    return list(
        db.execute(select(MasterLegalTemplate).order_by(MasterLegalTemplate.version.desc())).scalars()
    )


def delete_master_template_draft(db: Session, template_id: UUID) -> None:
    """Elimina un borrador de la plantilla. 409 si no está en
    `estado='borrador'` (mismo criterio que `delete_platform_document_draft`)."""
    template = _get_master_template_or_404(db, template_id)
    if template.estado != "borrador":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Solo se puede eliminar una plantilla en estado borrador",
        )
    db.delete(template)
    db.commit()
