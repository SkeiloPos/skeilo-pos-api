"""Endpoints tenant-facing para la información del negocio (incluye logo).

El `Tenant` vive en el schema `shared`, por eso las escrituras usan
`get_shared_db` (sesión sobre shared) en vez de `get_db` (sesión del schema del
tenant). El logo se sube a R2 vía POST /uploads/presign (folder="logo"); spec
080: en base de datos vive solo la `key` del objeto (el esquema normaliza a key
cualquier URL absoluta del bucket gestionado que llegue, FR-004) y la URL de
visualización se arma al responder contra `ASSETS_BASE_URL`."""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.asset_refs import ImageDecision, delete_if_unreferenced, resolve_image_change
from app.core.db import get_tenant, with_db
from app.core.dependencies import get_current_user, require_tenant_admin, get_shared_db
from app.core.models import Tenant, User
from app.core.storage import FOLDER_LOGO, deletable_key, delete_object, normalize_asset_ref
from app.api.v1.tenant.schemas import TenantInfoResponse, TenantUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenant", tags=["tenant"])


@router.get(
    "",
    response_model=TenantInfoResponse,
    summary="Información del negocio del tenant actual",
    responses={
        401: {"description": "No autenticado o token inválido."},
        404: {"description": "Tenant no encontrado para el host."},
    },
)
def get_tenant_info(
    tenant: Tenant = Depends(get_tenant),
    _: User = Depends(get_current_user),
):
    return tenant


@router.patch(
    "",
    response_model=TenantInfoResponse,
    summary="Actualizar la información del negocio (logo, recibo, prefijo de factura)",
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "El usuario no es administrador del tenant."},
    },
)
def update_tenant(
    body: TenantUpdate,
    tenant: Tenant = Depends(get_tenant),
    _: User = Depends(require_tenant_admin),
    db: Session = Depends(get_shared_db),
):
    row = db.query(Tenant).filter(Tenant.id == tenant.id).one()

    # spec 088 (research D6): el logo se resuelve ANTES de tocar `row`, de modo que un
    # 422/503 deja todo intacto. Un logo enviado solo cuenta como cambio si `logo_url_base`
    # coincide con el vigente (FR-002); si el formulario está desactualizado el logo no
    # cambia, pero `receipt_message` e `invoice_prefix` se guardan igualmente.
    # spec 080: `logo_url`/`logo_url_base` ya vienen normalizados a key (AssetRefIn).
    logo_decision = resolve_image_change(
        db, tenant_schema=row.schema, folder=FOLDER_LOGO,
        sent=body.logo_url,
        base_provided="logo_url_base" in body.model_fields_set,
        base=body.logo_url_base,
        current=normalize_asset_ref(row.logo_url),
        is_creation=False,
    )

    old_logo_key = None
    if logo_decision is ImageDecision.APPLY:
        # None si la referencia anterior es de otro origen o está fuera de convención
        # (FR-004/FR-005): no se borra.
        old_logo_key = deletable_key(row.logo_url, row.schema, FOLDER_LOGO)
        row.logo_url = body.logo_url
    elif body.logo_url is not None and normalize_asset_ref(row.logo_url) == body.logo_url:
        # Mismo logo (KEEP) en otra representación: una fila histórica con URL absoluta
        # pasa a guardarse como key (spec 080, FR-004). No borra ni verifica nada.
        row.logo_url = body.logo_url

    if body.receipt_message is not None:
        # Vaciar el campo es la forma de quitar el mensaje del recibo.
        row.receipt_message = body.receipt_message.strip() or None

    if body.invoice_prefix is not None:
        # Cambiarlo arranca una numeración nueva: cada prefijo lleva su propio
        # consecutivo, así que el nuevo empieza en 1 y el anterior se conserva.
        row.invoice_prefix = body.invoice_prefix.strip().upper() or None

    if body.unit_conversion_enabled is not None:
        row.unit_conversion_enabled = body.unit_conversion_enabled

    db.commit()
    # A-44 / spec 021 (intacto): primero confirmar, después borrar best-effort. spec 088
    # (FR-006): y solo si ninguna otra fila usa el archivo anterior. La sesión `shared` no
    # alcanza las tablas del negocio (productos, métodos de pago, comprobantes): el chequeo
    # abre una sesión corta del negocio (research D9).
    if old_logo_key:
        with with_db(row.schema) as tenant_db:
            delete_if_unreferenced(tenant_db, old_logo_key, delete_object)
    db.refresh(row)
    return row
