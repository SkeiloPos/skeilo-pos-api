"""Esquemas de super admin para documentos legales de plataforma y plantilla
legal maestra (spec 162, contracts/super-admin-api.md)."""
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.core.timezone import UtcDatetime


def _non_blank_markdown(valor: str) -> str:
    if not valor.strip():
        raise ValueError("El contenido no puede estar vacío")
    return valor


class PlatformLegalDocumentIn(BaseModel):
    """Body de `POST /platform-documents`. `contenido_markdown` puede llegar
    vacío aquí a propósito: crea la fila borrador que el editor llena
    después (no hay ningún borrador previo del que partir la primera vez).
    Lo que no debe quedar vacío es lo que se guarda al editar (`Patch`) ni,
    sobre todo, lo que se publica — ver `_non_blank_markdown` ahí y el check
    en `publish_platform_document`."""

    tipo: Literal["privacidad", "terminos"]
    contenido_markdown: str


class PlatformLegalDocumentPatch(BaseModel):
    """Body de `PATCH /platform-documents/{id}` — solo edita un borrador."""

    contenido_markdown: str

    _validar_contenido = field_validator("contenido_markdown")(_non_blank_markdown)


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
    """Body de `POST /master-template`. `contenido_markdown` puede llegar
    vacío aquí a propósito (ver `PlatformLegalDocumentIn`)."""

    contenido_markdown: str


class MasterLegalTemplatePatch(BaseModel):
    """Body de `PATCH /master-template/{id}` — solo edita un borrador."""

    contenido_markdown: str

    _validar_contenido = field_validator("contenido_markdown")(_non_blank_markdown)


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
