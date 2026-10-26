from pydantic import BaseModel, ConfigDict, Field

from app.core.schema_types import AssetRefIn, AssetUrl


class TenantInfoResponse(BaseModel):
    id: int
    name: str
    host: str
    # `plan` (texto libre heredado) se elimina en spec 033 — el plan de
    # suscripción real vive en `GET /plan` (Historia de Usuario 6), no aquí.
    # spec 080: la columna guarda la key; se ensambla la URL contra
    # ASSETS_BASE_URL al serializar (FR-005/FR-006).
    logo_url: AssetUrl = None
    receipt_message: str | None = None
    invoice_prefix: str | None = None
    # Zona horaria IANA del negocio (spec 030). Solo lectura — no se agrega a
    # TenantUpdate, ver contracts/tenant-info-endpoint.md.
    timezone: str
    # Permite capturar una línea de compra en una unidad distinta de la base del insumo
    # (spec 134, FR-016).
    unit_conversion_enabled: bool

    model_config = ConfigDict(from_attributes=True)


class TenantUpdate(BaseModel):
    # Referencia al logo ya subido a R2 (vía POST /uploads/presign folder="logo").
    # spec 080: en base de datos vive solo la key; si el cliente reenvía una URL
    # absoluta del bucket gestionado se normaliza a key antes de persistir (FR-004).
    logo_url: AssetRefIn = Field(None, max_length=500)
    # spec 088 (FR-002, research D5): logo que el formulario mostraba al abrirse. "No
    # enviado" (ausente) != `null` explícito; se distinguen con `model_fields_set`.
    logo_url_base: AssetRefIn = Field(None, max_length=500)
    # Mensaje que cierra la factura impresa. Cadena vacía = borrarlo (queda NULL);
    # omitirlo = dejarlo como está.
    receipt_message: str | None = Field(None, max_length=255)
    # Prefijo del consecutivo de facturación. Vaciarlo = numeración sin prefijo.
    invoice_prefix: str | None = Field(None, max_length=20)
    # spec 134, FR-016: editable únicamente por ADMIN (mismo criterio que el resto de
    # este endpoint, ya detrás de require_tenant_admin).
    unit_conversion_enabled: bool | None = None
