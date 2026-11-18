"""Esquemas del panel de tenant para perfil legal y documento legal propio
(spec 162, US3, contracts/tenant-panel-api.md)."""
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.core.timezone import UtcDatetime


class TenantLegalProfileOut(BaseModel):
    razon_social: Optional[str] = None
    identificacion_legal: Optional[str] = None
    direccion_legal: Optional[str] = None
    ciudad: Optional[str] = None
    correo_contacto: Optional[str] = None
    telefono_contacto: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class TenantLegalProfileIn(BaseModel):
    """Reemplaza el perfil (FR-012). Parcial permitido: solo lo enviado se
    guarda, la completitud se exige en preview/publish, no aquí."""

    razon_social: Optional[str] = None
    identificacion_legal: Optional[str] = None
    direccion_legal: Optional[str] = None
    ciudad: Optional[str] = None
    correo_contacto: Optional[str] = None
    telefono_contacto: Optional[str] = None


class TenantLegalDocumentOut(BaseModel):
    id: UUID
    version: int
    master_template_version: int
    contenido_renderizado: str
    content_hash: str
    estado: str
    published_at: UtcDatetime

    model_config = ConfigDict(from_attributes=True)


class TenantLegalDocumentPreviewOut(BaseModel):
    contenido_renderizado: str
    master_template_version: int


class TemplateUpdateStatusOut(BaseModel):
    actualizacion_pendiente: bool
    version_actual: Optional[int] = None
    version_disponible: Optional[int] = None
