from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.db import get_db, get_tenant
from app.core.crud import get_or_404, ensure_unique
from app.core.dependencies import get_current_user, require_tenant_admin
from app.core.models import Tenant, User
from app.core.pagination import Page, paginate
from app.core.plan_limits import enforce_plan_limit
from app.core.storage import asset_display_url
from app.models.payment import PaymentMethod
from app.models.payment_method_catalog import PaymentMethodCatalog
from app.models.sale import Sale
from app.api.v1.sales import service
from app.api.v1.sales.schemas import (
    CatalogPaymentMethodOption, PaymentMethodCreate, PaymentMethodCheckoutOption,
    PaymentMethodResponse, PaymentMethodUpdate, SaleCreate, SaleResponse,
)

router = APIRouter(prefix="/sales", tags=["sales"])


def payment_method_response(method: PaymentMethod) -> PaymentMethodResponse:
    """spec 080 (FR-005/FR-006/FR-007): respuesta admin de un método de pago
    con las claves de `payment_info` marcadas `format:"image"` (según
    `method.fields`, propiedad que lee del catálogo) entregadas como URL de
    visualización contra ASSETS_BASE_URL. La columna `payment_info` no cambia
    al serializar — la key sigue viviendo en base de datos.

    `PaymentMethodResponse` no expone `fields` (decisión spec 032), por eso el
    ensamblado se hace aquí en el router y no con un tipo `Annotated` ciego."""
    resp = PaymentMethodResponse.model_validate(method)
    if resp.payment_info:
        image_keys = {
            f.get("key") for f in method.fields if f.get("format") == "image"
        }
        if image_keys:
            resp.payment_info = {
                k: (asset_display_url(v) if k in image_keys else v)
                for k, v in resp.payment_info.items()
            }
    return resp


# ============================ Métodos de pago ============================
@router.get(
    "/payment-methods/catalog",
    response_model=list[CatalogPaymentMethodOption],
    summary="Ver el catálogo de métodos de pago disponible para activar (spec 032)",
)
def list_payment_methods_catalog(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return service.list_catalog_for_tenant(db)


@router.get(
    "/payment-methods",
    response_model=list[PaymentMethodResponse] | list[PaymentMethodCheckoutOption],
    summary="Listar métodos de pago",
    description=(
        "Sin `available`: listado administrativo completo (todos los estados). Con "
        "`available=true`: solo los disponibles para cobrar (activos, completos y con "
        "el catálogo activo), sin datos de integración — spec 032, FR-012/FR-012a."
    ),
)
def list_payment_methods(
    available: bool = Query(False, description="Filtra a los disponibles para cobrar en caja."),
    db: Session = Depends(get_db), _: User = Depends(get_current_user),
):
    if available:
        return service.list_available_payment_methods(db)
    # spec 080: selectinload(catalog) para que `payment_method_response` lea
    # `method.fields` sin disparar un N+1 al ensamblar las claves de imagen.
    methods = db.execute(
        select(PaymentMethod)
        .options(selectinload(PaymentMethod.catalog))
        .order_by(PaymentMethod.name)
    ).scalars().all()
    return [payment_method_response(m) for m in methods]


@router.post(
    "/payment-methods", response_model=PaymentMethodResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Activar un método de pago del catálogo para este tenant (spec 032)",
)
def create_payment_method(
    body: PaymentMethodCreate,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_tenant),
    _: User = Depends(require_tenant_admin),
):
    enforce_plan_limit(db, tenant, "metodos_pago_activos")  # spec 033, FR-005/FR-006
    return payment_method_response(
        service.create_payment_method(db, body, tenant_schema=tenant.schema)
    )


@router.patch(
    "/payment-methods/{payment_method_id}",
    response_model=PaymentMethodResponse,
    summary="Editar/activar/desactivar/reactivar un método de pago (spec 024 / spec 032)",
)
def update_payment_method(
    payment_method_id: UUID, body: PaymentMethodUpdate,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_tenant),
    _: User = Depends(require_tenant_admin),
):
    return payment_method_response(
        service.update_payment_method(db, payment_method_id, body, tenant_schema=tenant.schema)
    )


# ============================ Ventas ============================
@router.post("", response_model=SaleResponse, status_code=status.HTTP_201_CREATED, summary="Checkout: emitir y cobrar una venta")
def create_sale(
    body: SaleCreate,
    db: Session = Depends(get_db),
    cashier: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    sale = service.checkout(db, body, cashier, invoice_prefix=tenant.invoice_prefix or "", tenant=tenant)
    return _load_sale(db, sale.id)


@router.get("", response_model=Page[SaleResponse], summary="Listar ventas")
def list_sales(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None, pattern="^(issued|paid|void)$", description="Filtra por estado de la venta."),
    date_from: date | None = Query(None, description="Fecha inicial (inclusive) sobre sold_at."),
    date_to: date | None = Query(None, description="Fecha final (inclusive) sobre sold_at."),
    invoice_reference: str | None = Query(None, description="Búsqueda por referencia de factura (prefijo+número)."),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    stmt = service.list_sales_query(
        tenant=tenant,
        status=status,
        date_from=date_from,
        date_to=date_to,
        invoice_reference=invoice_reference,
    )
    return paginate(db, stmt, page, size)


@router.get("/{sale_id}", response_model=SaleResponse, summary="Obtener una venta")
def get_sale(sale_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return _load_sale(db, sale_id)


def _load_sale(db: Session, sale_id: UUID) -> Sale:
    sale = db.execute(
        select(Sale)
        .options(
            selectinload(Sale.items),
            selectinload(Sale.payments),
            selectinload(Sale.invoice),
            selectinload(Sale.dining_table),
        )
        .where(Sale.id == sale_id)
    ).scalar_one_or_none()
    if sale is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sale not found")
    return sale
