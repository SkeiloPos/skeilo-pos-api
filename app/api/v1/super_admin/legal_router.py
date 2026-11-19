"""Documentos legales de plataforma y plantilla legal maestra (spec 162,
contracts/super-admin-api.md). Registrado bajo `super_admin/router.py`, que
ya exige `SUPER_ADMIN` para todo lo que incluye (FR-001, FR-006) — sin
dependencia de auth propia aquí, mismo patrón que `payment_methods_router.py`."""
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_super_admin, get_shared_db
from app.core.models import User
from app.api.v1.super_admin import legal_platform_service as service
from app.api.v1.super_admin.legal_schemas import (
    MasterLegalTemplateIn,
    MasterLegalTemplateOut,
    MasterLegalTemplatePatch,
    MasterLegalTemplatePublishIn,
    PlatformLegalDocumentIn,
    PlatformLegalDocumentOut,
    PlatformLegalDocumentPatch,
    PlatformLegalDocumentPublishIn,
)

router = APIRouter(prefix="/legal", tags=["super-admin"])


@router.post(
    "/platform-documents",
    response_model=PlatformLegalDocumentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Crear un borrador de documento legal de plataforma",
)
def create_platform_document(
    body: PlatformLegalDocumentIn, db: Session = Depends(get_shared_db),
):
    return service.create_platform_document_draft(
        db, tipo=body.tipo, contenido_markdown=body.contenido_markdown,
    )


@router.patch(
    "/platform-documents/{document_id}",
    response_model=PlatformLegalDocumentOut,
    summary="Editar el borrador de un documento legal de plataforma",
    responses={409: {"description": "El documento ya no está en estado borrador."}},
)
def edit_platform_document(
    document_id: UUID, body: PlatformLegalDocumentPatch, db: Session = Depends(get_shared_db),
):
    return service.edit_platform_document_draft(
        db, document_id, contenido_markdown=body.contenido_markdown,
    )


@router.post(
    "/platform-documents/{document_id}/publish",
    response_model=PlatformLegalDocumentOut,
    summary="Publicar un documento legal de plataforma",
    responses={
        409: {"description": "El documento no está en borrador, o perdió una carrera de publicación."},
        422: {"description": "Falta fecha_vigencia."},
    },
)
def publish_platform_document(
    document_id: UUID,
    body: PlatformLegalDocumentPublishIn,
    db: Session = Depends(get_shared_db),
    actor: User = Depends(get_current_super_admin),
):
    return service.publish_platform_document(
        db, document_id, fecha_vigencia=body.fecha_vigencia, published_by_user_id=actor.id,
    )


@router.get(
    "/platform-documents/{tipo}/history",
    response_model=list[PlatformLegalDocumentOut],
    summary="Historial de versiones de un documento legal de plataforma",
)
def get_platform_document_history(tipo: str, db: Session = Depends(get_shared_db)):
    return service.get_platform_document_history(db, tipo)


@router.delete(
    "/platform-documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar un borrador de documento legal de plataforma",
    responses={409: {"description": "El documento no está en estado borrador."}},
)
def delete_platform_document(document_id: UUID, db: Session = Depends(get_shared_db)):
    service.delete_platform_document_draft(db, document_id)


@router.get(
    "/platform-documents/{document_id}/preview",
    summary="Vista previa renderizada de cualquier versión de un documento de plataforma",
)
def preview_platform_document(document_id: UUID, db: Session = Depends(get_shared_db)):
    return {"contenido_html": service.render_platform_document(db, document_id)}


@router.post(
    "/master-template",
    response_model=MasterLegalTemplateOut,
    status_code=status.HTTP_201_CREATED,
    summary="Crear un borrador de la plantilla legal maestra",
    responses={422: {"description": "La plantilla usa variables fuera de la lista permitida."}},
)
def create_master_template(body: MasterLegalTemplateIn, db: Session = Depends(get_shared_db)):
    return service.create_master_template_draft(db, contenido_markdown=body.contenido_markdown)


@router.patch(
    "/master-template/{template_id}",
    response_model=MasterLegalTemplateOut,
    summary="Editar el borrador de la plantilla legal maestra",
    responses={
        409: {"description": "La plantilla ya no está en estado borrador."},
        422: {"description": "La plantilla usa variables fuera de la lista permitida."},
    },
)
def edit_master_template(
    template_id: UUID, body: MasterLegalTemplatePatch, db: Session = Depends(get_shared_db),
):
    return service.edit_master_template_draft(
        db, template_id, contenido_markdown=body.contenido_markdown,
    )


@router.post(
    "/master-template/{template_id}/publish",
    response_model=MasterLegalTemplateOut,
    summary="Publicar una versión de la plantilla legal maestra",
    responses={
        409: {"description": "La plantilla no está en borrador, o perdió una carrera de publicación."},
        422: {"description": "Falta fecha_vigencia."},
    },
)
def publish_master_template(
    template_id: UUID,
    body: MasterLegalTemplatePublishIn,
    db: Session = Depends(get_shared_db),
    actor: User = Depends(get_current_super_admin),
):
    return service.publish_master_template(
        db, template_id, fecha_vigencia=body.fecha_vigencia, published_by_user_id=actor.id,
    )


@router.get(
    "/master-template/history",
    response_model=list[MasterLegalTemplateOut],
    summary="Historial de versiones de la plantilla legal maestra",
)
def get_master_template_history(db: Session = Depends(get_shared_db)):
    return service.get_master_template_history(db)


@router.delete(
    "/master-template/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar un borrador de la plantilla legal maestra",
    responses={409: {"description": "La plantilla no está en estado borrador."}},
)
def delete_master_template(template_id: UUID, db: Session = Depends(get_shared_db)):
    service.delete_master_template_draft(db, template_id)


@router.get(
    "/master-template/{template_id}/preview",
    summary="Vista previa renderizada de cualquier versión de la plantilla maestra",
)
def preview_master_template(template_id: UUID, db: Session = Depends(get_shared_db)):
    return {"contenido_html": service.render_master_template(db, template_id)}
