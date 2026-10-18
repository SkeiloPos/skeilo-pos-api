from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import select, func

from app.core import events
from app.core.db import get_db, get_tenant
from app.core.dependencies import AccessTokenBearer, get_current_user
from app.core.crud import get_or_404, ensure_unique
from app.core.models import Tenant, User
from app.core.pagination import Page, paginate
from app.models.category import Category
from app.models.category_presentation import CategoryPresentation
from app.models.presentation import Presentation
from app.api.v1.categories.schemas import CategoryCreate, CategoryUpdate, CategoryResponse


def _replace_category_presentations(
    db: Session, category: Category, presentation_ids: list[UUID]
) -> None:
    """Reemplazo total de las presentaciones asociadas a `category` (spec 083,
    research.md D5) -- mismo patrón que `_replace_option_groups`
    (`app/api/v1/catalog/service.py:178`). 404 si algún id no existe en
    `presentations` (no se exige `active=true`, FR-002)."""
    db.execute(
        CategoryPresentation.__table__.delete().where(
            CategoryPresentation.category_id == category.id
        )
    )
    for presentation_id in presentation_ids:
        if db.get(Presentation, presentation_id) is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail=f"Presentation {presentation_id} not found",
            )
        db.add(CategoryPresentation(category_id=category.id, presentation_id=presentation_id))

router = APIRouter(prefix="/categories", tags=["categories"])
acccess_token_bearer = AccessTokenBearer()

@router.get(
    "",
    response_model=Page[CategoryResponse],
    summary="Listar categorías",
    description="Devuelve las categorías de forma paginada. Permite filtrar por estado activo/inactivo y buscar por nombre.",
    response_description="Página de categorías.",
    responses={
        401: {"description": "No autenticado o token inválido."},
    },
)
def list_categories(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    active: bool | None = Query(
        None, description="Filtra por estado activo (true) o inactivo (false)."
    ),
    visible_en_qr: bool | None = Query(
        None,
        description="Filtra por visibilidad en el menú QR (true) u ocultas en QR (false). spec 128, FR-011.",
    ),
    search: str | None = Query(None, description="Búsqueda por nombre (contiene, sin distinguir mayúsculas)."),
    db: Session = Depends(get_db),
    _: dict = Depends(acccess_token_bearer),
    user: User = Depends(get_current_user),

):
    query = select(Category).order_by(Category.name)
    if active is not None:
        query = query.where(Category.active == active)
    if visible_en_qr is not None:
        query = query.where(Category.visible_en_qr == visible_en_qr)
    if search:
        query = query.where(Category.name.ilike(f"%{search.strip()}%"))
    return paginate(db, query, page, size)


@router.get(
    "/{id}",
    response_model=CategoryResponse,
    summary="Obtener una categoría",
    description="Devuelve una categoría por su identificador único (UUID).",
    response_description="La categoría encontrada.",
    responses={
        401: {"description": "No autenticado o token inválido."},
        404: {"description": "La categoría no existe."},
    },
)
def get_category(
    id: UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return get_or_404(db, Category, id, "Category not found")


@router.post(
    "",
    response_model=CategoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear una categoría",
    description="Crea una nueva categoría. El nombre debe ser único.",
    response_description="La categoría creada.",
    responses={
        401: {"description": "No autenticado o token inválido."},
        409: {"description": "Ya existe una categoría con ese nombre."},
        422: {"description": "Datos de entrada inválidos."},
    },
)
def create_category(
    body: CategoryCreate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    ensure_unique(db, Category, Category.name, body.name, "Category name already exists")

    if body.display_order is not None:
        display_order = body.display_order
    else:
        current_max = db.execute(select(func.max(Category.display_order))).scalar()
        display_order = (current_max or 0) + 1

    category = Category(name=body.name, description=body.description, display_order=display_order)
    db.add(category)
    db.flush()
    if body.presentation_ids is not None:
        _replace_category_presentations(db, category, body.presentation_ids)
    db.commit()
    db.refresh(category)
    return category


@router.patch(
    "/{id}",
    response_model=CategoryResponse,
    summary="Actualizar una categoría",
    description="Actualiza parcialmente una categoría. Solo se modifican los campos enviados.",
    response_description="La categoría actualizada.",
    responses={
        401: {"description": "No autenticado o token inválido."},
        404: {"description": "La categoría no existe."},
        409: {"description": "Ya existe una categoría con ese nombre."},
        422: {"description": "Datos de entrada inválidos."},
    },
)
def update_category(
    id: UUID,
    body: CategoryUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    category = get_or_404(db, Category, id, "Category not found")

    if body.name is not None and body.name != category.name:
        ensure_unique(db, Category, Category.name, body.name, "Category name already exists")
        category.name = body.name

    if body.description is not None:
        category.description = body.description

    if body.active is not None:
        category.active = body.active

    if body.visible_en_qr is not None:
        # spec 128 (FR-005/FR-006): restricción de rol acotada SOLO a este
        # campo -- el resto del endpoint sigue sin exigir ningún rol
        # (research.md, Decisión 3; no agregar require_tenant_admin a todo
        # el endpoint). Se evalúa `category.active` DESPUÉS de aplicar
        # `body.active` arriba, para permitir activar y mostrar/ocultar en
        # QR en la misma llamada.
        if not user.role or user.role.name != "ADMIN":
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Solo un Administrador puede cambiar la visibilidad en el menú QR",
            )
        if not category.active:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Activa la categoría para controlar su visibilidad en QR",
            )
        category.visible_en_qr = body.visible_en_qr

    if body.display_order is not None:
        category.display_order = body.display_order

    if body.presentation_ids is not None:
        _replace_category_presentations(db, category, body.presentation_ids)

    db.commit()
    db.refresh(category)
    # spec 128 (US3, FR-009): publicar DESPUÉS del commit -- si la
    # transacción hubiera fallado no puede haber salido un evento
    # anunciando un cambio que no ocurrió (mismo principio que todo el
    # catálogo de app/core/events.py). Solo cuando el campo venía en el
    # payload: evitar un evento de "nada cambió" en cada PATCH de otro campo.
    if body.visible_en_qr is not None:
        events.menu_updated(tenant.id)
    return category


@router.delete(
    "/{id}",
    response_model=CategoryResponse,
    summary="Desactivar una categoría",
    description="Realiza un borrado lógico (soft-delete): marca la categoría como inactiva (active=False) sin eliminarla físicamente.",
    response_description="La categoría desactivada.",
    responses={
        401: {"description": "No autenticado o token inválido."},
        404: {"description": "La categoría no existe."},
    },
)
def delete_category(
    id: UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    category = get_or_404(db, Category, id, "Category not found")
    category.active = False
    db.commit()
    db.refresh(category)
    return category
