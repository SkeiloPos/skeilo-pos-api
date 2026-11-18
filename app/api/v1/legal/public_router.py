"""API pública de documentos legales (spec 162, contracts/public-api.md):
landing (documentos de plataforma) y menú QR (documento del tenant, en una
fase posterior). Sin autenticación: `with_db(None)` directo sobre `shared`,
igual que `find_tenant_by_host` — no hay tenant que resolver para
`/legal/platform-documents/{tipo}`."""
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, TypeAdapter
from sqlalchemy import select

from app.core.db import with_db
from app.core.http_cache import json_or_304_with_hash
from app.core.legal_markdown import render_markdown_to_html
from app.core.models import PlatformLegalDocument, Tenant
from app.core.rate_limit import enforce as rate_limit
from app.models import TenantLegalDocument

router = APIRouter(prefix="/legal", tags=["legal"])


class PlatformLegalDocumentPublicOut(BaseModel):
    tipo: str
    version: int
    contenido_html: str
    fecha_vigencia: Optional[datetime] = None
    content_hash: str


# A nivel de módulo: compilar el esquema por request anularía el ahorro de `json_or_304_with_hash`.
_PLATFORM_DOCUMENT_ADAPTER = TypeAdapter(PlatformLegalDocumentPublicOut)


@router.get(
    "/platform-documents/{tipo}",
    summary="Documento legal vigente de la plataforma (landing, sin auth)",
    responses={404: {"description": "Nunca se publicó ninguna versión de ese tipo."}},
)
async def get_platform_document(tipo: Literal["privacidad", "terminos"], request: Request):
    await rate_limit(request, "legal_public")

    with with_db(None) as db:
        doc = db.execute(
            select(PlatformLegalDocument).where(
                PlatformLegalDocument.tipo == tipo,
                PlatformLegalDocument.estado == "publicado",
            )
        ).scalar_one_or_none()

    if doc is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Todavía no hay un documento vigente de ese tipo",
        )

    data = PlatformLegalDocumentPublicOut(
        tipo=doc.tipo,
        version=doc.version,
        contenido_html=render_markdown_to_html(doc.contenido_markdown),
        fecha_vigencia=doc.fecha_vigencia,
        content_hash=doc.content_hash,
    )
    return json_or_304_with_hash(request, _PLATFORM_DOCUMENT_ADAPTER, data, doc.content_hash)


class TenantLegalDocumentPublicOut(BaseModel):
    id: UUID
    version: int
    contenido_html: str
    published_at: datetime
    content_hash: str


_TENANT_DOCUMENT_ADAPTER = TypeAdapter(TenantLegalDocumentPublicOut)


@router.get(
    "/tenant-documents/{document_id}",
    summary="Snapshot publicado de un documento legal de tenant (menú QR, sin auth)",
    responses={404: {"description": "No existe ese snapshot."}},
)
async def get_tenant_document(document_id: UUID, request: Request):
    await rate_limit(request, "legal_public")

    doc = None
    with with_db(None) as shared_db:
        tenant_schemas = list(shared_db.execute(select(Tenant.schema)).scalars())

    # El id del snapshot no identifica por sí solo el schema físico que lo
    # contiene (research.md Decisión 8: "no necesita resolver tenant por
    # host", pero sí necesita resolver tenant por *schema* para una consulta
    # cross-schema por UUID) — se busca por PK en cada schema de tenant.
    # Barato en la práctica: es un lookup indexado por PK, repetido N veces
    # (N = tenants activos), no un scan.
    for schema in tenant_schemas:
        with with_db(schema) as tenant_db:
            doc = tenant_db.execute(
                select(TenantLegalDocument).where(TenantLegalDocument.id == document_id)
            ).scalar_one_or_none()
        if doc is not None:
            break

    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese documento legal")

    data = TenantLegalDocumentPublicOut(
        id=doc.id,
        version=doc.version,
        contenido_html=render_markdown_to_html(doc.contenido_renderizado),
        published_at=doc.published_at,
        content_hash=doc.content_hash,
    )
    return json_or_304_with_hash(request, _TENANT_DOCUMENT_ADAPTER, data, doc.content_hash)
