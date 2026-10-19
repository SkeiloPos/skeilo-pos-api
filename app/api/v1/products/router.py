from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.db import get_db, get_tenant
from app.core.dependencies import (
    get_current_user,
    require_tenant_admin,
    require_tenant_admin_or_cashier,
)
from app.core.pagination import Page, paginate
from app.core.models import Tenant, User
from app.core.plan_limits import enforce_plan_limit
from app.api.v1.products.service import ProductService
from app.api.v1.products.schemas import (
    ProductCreate,
    ProductUpdate,
    ProductAvailabilityUpdate,
    ProductResponse,
    ProductListResponse,
    ProductDetailResponse,
    ProductSaveResponse,
)

router = APIRouter(prefix="/products", tags=["products"])

service = ProductService()


@router.get(
    "",
    response_model=Page[ProductListResponse],
    summary="Listar productos",
    description="Devuelve los productos de forma paginada (más recientes primero). Filtra por estado activo y/o disponibilidad.",
    responses={401: {"description": "No autenticado o token inválido."}},
)
def list_products(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    active: bool | None = Query(None, description="Filtra por estado activo/inactivo."),
    available: bool | None = Query(
        None, description="Filtra por disponibilidad (spec 093: Disponibles/Agotados)."
    ),
    search: str | None = Query(None, description="Búsqueda por nombre."),
    is_topping: bool | None = Query(
        None,
        description=(
            "spec 129: filtra por si el producto está marcado como adición "
            "(topping) -- usado por el selector de 'Adiciones disponibles'."
        ),
    ),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return paginate(db, service.list_query(active, search, available, is_topping), page, size)


@router.get(
    "/{id}",
    response_model=ProductDetailResponse,
    summary="Obtener un producto",
    responses={
        401: {"description": "No autenticado o token inválido."},
        404: {"description": "El producto no existe."},
    },
)
def get_product(
    id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Spec 103-detalle-producto-consolidado: `get_detail_or_404` precarga receta/grupos
    # de opciones por variante (research.md D2); `to_detail_response` necesita el `user`
    # para ocultarle la receta al rol Cajero (D3, FR-003).
    return service.to_detail_response(service.get_detail_or_404(db, id), user)


@router.patch(
    "/{id}/availability",
    response_model=ProductResponse,
    summary="Marcar o desmarcar un producto como agotado",
    description=(
        "Único endpoint por el que Cajero y Administrador tocan `available` (spec 093). "
        "A diferencia de `PATCH /products/{id}`, no acepta ningún otro campo."
    ),
    responses={
        401: {"description": "No autenticado o token inválido."},
        403: {"description": "El usuario no es Administrador ni Cajero."},
        404: {"description": "El producto no existe."},
        409: {"description": "El producto está inactivo: no se puede cambiar su disponibilidad."},
    },
)
def set_product_availability(
    id: UUID,
    body: ProductAvailabilityUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_tenant_admin_or_cashier),
):
    return service.set_availability(db, id, body.available, user)


@router.post(
    "",
    response_model=ProductSaveResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear un producto",
    description=(
        "Crea un producto de catálogo junto con sus presentaciones, receta y grupos de opciones "
        "en una sola operación atómica (spec 043). Sin `variants`, recibe una variante default."
    ),
    responses={
        401: {"description": "No autenticado o token inválido."},
        404: {"description": "La categoría, un insumo o un grupo de opciones no existen."},
        409: {"description": "Nombre de presentación o SKU ya en uso."},
        422: {"description": "Datos de entrada inválidos."},
    },
)
def create_product(
    body: ProductCreate,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_tenant),
    _: User = Depends(require_tenant_admin),
):
    enforce_plan_limit(db, tenant, "productos")  # spec 033, FR-005/FR-006
    product = service.create_product(db, tenant, body)
    return service.to_save_response(product)


@router.patch(
    "/{id}",
    response_model=ProductSaveResponse,
    summary="Actualizar un producto",
    description=(
        "Actualiza un producto y, opcionalmente, el árbol completo de sus presentaciones "
        "(crear/editar/desactivar, receta, grupos de opciones y orden) en una sola operación "
        "atómica (spec 043)."
    ),
    responses={
        401: {"description": "No autenticado o token inválido."},
        404: {"description": "El producto, la categoría, un insumo o un grupo de opciones no existen."},
        409: {"description": "Nombre de presentación o SKU ya en uso."},
        422: {"description": "Datos de entrada inválidos."},
    },
)
def update_product(
    id: UUID,
    body: ProductUpdate,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_tenant),
    _: User = Depends(require_tenant_admin),
):
    product = service.update_product(db, tenant, id, body)
    return service.to_save_response(product)


@router.put(
    "/{id}",
    response_model=ProductSaveResponse,
    summary="Actualizar un producto (alias de PATCH)",
    responses={
        401: {"description": "No autenticado o token inválido."},
        404: {"description": "El producto, la categoría, un insumo o un grupo de opciones no existen."},
        409: {"description": "Nombre de presentación o SKU ya en uso."},
        422: {"description": "Datos de entrada inválidos."},
    },
)
def replace_product(
    id: UUID,
    body: ProductUpdate,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_tenant),
    _: User = Depends(require_tenant_admin),
):
    product = service.update_product(db, tenant, id, body)
    return service.to_save_response(product)


@router.delete(
    "/{id}",
    response_model=ProductResponse,
    summary="Desactivar un producto",
    description="Borrado lógico: marca el producto como inactivo (active=False).",
    responses={
        401: {"description": "No autenticado o token inválido."},
        404: {"description": "El producto no existe."},
    },
)
def delete_product(
    id: UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_tenant_admin),
):
    return service.soft_delete(db, id)
