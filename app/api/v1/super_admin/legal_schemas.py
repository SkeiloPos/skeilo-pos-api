"""Esquemas de super admin para documentos legales de plataforma y plantilla
legal maestra (spec 162, contracts/super-admin-api.md)."""
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.core.timezone import UtcDatetime


class PlatformLegalDocumentIn(BaseModel):
    """Body de `POST /platform-documents`."""

    tipo: Literal["privacidad", "terminos"]
    contenido_markdown: str


class PlatformLegalDocumentPatch(BaseModel):
    """Body de `PATCH /platform-documents/{id}` — solo edita un borrador."""

    contenido_markdown: str


class PlatformLegalDocumentPublishIn(BaseModel):
    """Body de `POST /platform-documents/{id}/publish`."""

    fecha_vigencia: datetime


class PlatformLegalDocumentOut(BaseModel):
    id: UUID
    tipo: str
    version: int
    contenido_markdown: str
    estado: str
    content_hash: str
    fecha_vigencia: Optional[UtcDatetime] = None
    published_by_user_id: Optional[UUID] = None
    published_at: Optional[UtcDatetime] = None

    model_config = ConfigDict(from_attributes=True)


class MasterLegalTemplateIn(BaseModel):
    """Body de `POST /master-template`."""

    contenido_markdown: str


class MasterLegalTemplatePatch(BaseModel):
    """Body de `PATCH /master-template/{id}` — solo edita un borrador."""

    contenido_markdown: str


class MasterLegalTemplatePublishIn(BaseModel):
    """Body de `POST /master-template/{id}/publish`."""

    fecha_vigencia: datetime


class MasterLegalTemplateOut(BaseModel):
    id: UUID
    version: int
    contenido_markdown: str
    estado: str
    content_hash: str
    fecha_vigencia: Optional[UtcDatetime] = None
    published_by_user_id: Optional[UUID] = None
    published_at: Optional[UtcDatetime] = None

    model_config = ConfigDict(from_attributes=True)
