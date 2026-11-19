"""Panel de tenant: aceptación de los documentos legales de plataforma por el
administrador del tenant (spec 162, FR-019, contracts/tenant-panel-api.md).
Mismo patrón de auth que `table_sessions/router.py`: `get_current_user` +
`get_db`/`get_tenant`. `PlatformLegalDocument`/`TenantAdminPlatformAcceptance`
viven en `shared`, pero `get_db` solo traduce el schema `tenant` — las
consultas contra `shared.*` funcionan igual con esta sesión (mismo patrón que
la FK cross-schema de `payment_methods.catalog_id`)."""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.dependencies import get_current_user
from app.core.models import PlatformLegalDocument, TenantAdminPlatformAcceptance, User
from app.core.timezone import UtcDatetime
from app.api.v1.legal import service as legal_service
from app.api.v1.legal.schemas import (
    TemplateUpdateStatusOut,
    TenantLegalDocumentOut,
    TenantLegalDocumentPreviewOut,
    TenantLegalProfileIn,
    TenantLegalProfileOut,
)

router = APIRouter(prefix="/legal", tags=["legal"])


class PendingPlatformAcceptance(BaseModel):
    tipo: str
    platform_document_id: UUID
    version: int
    contenido_markdown: str


class PendingPlatformAcceptanceOut(BaseModel):
    pendientes: list[PendingPlatformAcceptance]


class PlatformAcceptanceIn(BaseModel):
    platform_document_id: UUID


class PlatformAcceptanceOut(BaseModel):
    accepted_at: UtcDatetime


def _current_platform_documents(db: Session) -> list[PlatformLegalDocument]:
    return list(
        db.execute(
            select(PlatformLegalDocument).where(PlatformLegalDocument.estado == "publicado")
        ).scalars()
    )


@router.get(
    "/platform-acceptance/pending",
    response_model=PendingPlatformAcceptanceOut,
    summary="Documentos de plataforma vigentes que el admin del tenant aún no aceptó",
)
def get_pending_platform_acceptance(
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    current_docs = _current_platform_documents(db)
    if not current_docs:
        return PendingPlatformAcceptanceOut(pendientes=[])

    accepted_ids = set(
        db.execute(
            select(TenantAdminPlatformAcceptance.platform_document_id).where(
                TenantAdminPlatformAcceptance.user_id == user.id,
                TenantAdminPlatformAcceptance.platform_document_id.in_(
                    [doc.id for doc in current_docs]
                ),
            )
        ).scalars()
    )

    return PendingPlatformAcceptanceOut(
        pendientes=[
            PendingPlatformAcceptance(
                tipo=doc.tipo,
                platform_document_id=doc.id,
                version=doc.version,
                contenido_markdown=doc.contenido_markdown,
            )
            for doc in current_docs
            if doc.id not in accepted_ids
        ]
    )


@router.post(
    "/platform-acceptance/accept",
    response_model=PlatformAcceptanceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Aceptar la versión vigente de un documento legal de plataforma",
)
def accept_platform_document(
    body: PlatformAcceptanceIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Idempotente (FR-019): `ON CONFLICT DO NOTHING` sobre
    # `uq_tenant_admin_platform_acceptance_user_document` — aceptar dos veces
    # la misma versión no duplica la fila ni falla.
    existing = db.execute(
        select(TenantAdminPlatformAcceptance).where(
            TenantAdminPlatformAcceptance.user_id == user.id,
            TenantAdminPlatformAcceptance.platform_document_id == body.platform_document_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return PlatformAcceptanceOut(accepted_at=existing.accepted_at)

    stmt = (
        pg_insert(TenantAdminPlatformAcceptance)
        .values(
            user_id=user.id,
            tenant_id=user.tenant_id,
            platform_document_id=body.platform_document_id,
        )
        .on_conflict_do_nothing(
            constraint="uq_tenant_admin_platform_acceptance_user_document"
        )
        .returning(TenantAdminPlatformAcceptance.accepted_at)
    )
    accepted_at = db.execute(stmt).scalar_one_or_none()
    db.commit()

    if accepted_at is None:
        accepted_at = db.execute(
            select(TenantAdminPlatformAcceptance.accepted_at).where(
                TenantAdminPlatformAcceptance.user_id == user.id,
                TenantAdminPlatformAcceptance.platform_document_id == body.platform_document_id,
            )
        ).scalar_one()

    return PlatformAcceptanceOut(accepted_at=accepted_at)


# --------------------------------------------------------------------- Perfil legal del tenant (US3)


@router.get(
    "/tenant-profile",
    response_model=TenantLegalProfileOut,
    summary="Perfil legal del tenant (puede venir vacío)",
)
def get_tenant_profile(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    profile = legal_service.get_tenant_legal_profile(db)
    if profile is None:
        return TenantLegalProfileOut()
    return profile


@router.put(
    "/tenant-profile",
    response_model=TenantLegalProfileOut,
    summary="Reemplazar el perfil legal del tenant (escritura parcial permitida)",
)
def put_tenant_profile(
    body: TenantLegalProfileIn, db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    return legal_service.put_tenant_legal_profile(db, **body.model_dump(exclude_unset=True))


# --------------------------------------------------------------------- Documento legal del tenant (US3)


@router.get(
    "/tenant-document/preview",
    response_model=TenantLegalDocumentPreviewOut,
    summary="Vista previa del documento legal del tenant, sin publicar nada",
    responses={422: {"description": "Faltan datos obligatorios del perfil legal."}},
)
def preview_tenant_document(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    contenido_renderizado, master_template_version = legal_service.preview_tenant_legal_document(db)
    return TenantLegalDocumentPreviewOut(
        contenido_renderizado=contenido_renderizado, master_template_version=master_template_version,
    )


@router.post(
    "/tenant-document/publish",
    response_model=TenantLegalDocumentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Publicar el snapshot del documento legal del tenant",
    responses={422: {"description": "Faltan datos obligatorios del perfil legal."}},
)
def publish_tenant_document(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return legal_service.publish_tenant_legal_document(db)


@router.get(
    "/tenant-document/current",
    response_model=Optional[TenantLegalDocumentOut],
    summary="Snapshot publicado vigente del documento legal del tenant",
)
def get_current_tenant_document(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return legal_service.get_current_tenant_legal_document(db)


@router.get(
    "/tenant-document/template-update-status",
    response_model=TemplateUpdateStatusOut,
    summary="Banner de FR-015: hay una versión de plantilla más nueva que la usada en el snapshot vigente",
)
def get_template_update_status(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    pendiente, version_actual, version_disponible = legal_service.get_template_update_status(db)
    return TemplateUpdateStatusOut(
        actualizacion_pendiente=pendiente,
        version_actual=version_actual,
        version_disponible=version_disponible,
    )
