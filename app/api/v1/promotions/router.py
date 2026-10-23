"""CRUD de promociones — spec 063 (modelo por conjunto explícito de variantes).

La aplicación automática en la venta vive en el checkout, que llama a
`service.evaluate_variant_sets`. `contracts/administracion-promociones.md` §3.
"""
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db, get_tenant
from app.core.crud import get_or_404, ensure_unique
from app.core.dependencies import get_current_user, require_tenant_admin
from app.core.models import Tenant, User
from app.core.audit import record_audit
from app.core.pagination import Page, paginate
from app.core.plan_limits import require_module_access
from app.models.promotion import Promotion
from app.api.v1.promotions import service
from app.api.v1.promotions.schemas import (
    PromotionCreate, PromotionUpdate, PromotionShapeUpdate, PromotionStatusUpdate,
    PromotionDuplicate, PromotionResponse,
)

router = APIRouter(
    prefix="/promotions", tags=["promotions"],
    dependencies=[Depends(require_module_access("promociones"))],
)


@router.get("", response_model=Page[PromotionResponse], summary="Listar promociones")
def list_promotions(
    response: Response,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(
        None, alias="status", description="draft | active | paused | finished"
    ),
    search: str | None = Query(None, description="Búsqueda por nombre."),
    closed_by_refactor: bool | None = Query(
        None,
        description="true = solo las promociones que la migración de la spec 063 "
                    "pasó a Finalizada (aviso de FR-025).",
    ),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    # A-09: el POS de staff necesita una hora de referencia sincronizada con el
    # servidor (en vez del reloj del dispositivo) para previsualizar vigencia.
    response.headers["X-Server-Time"] = datetime.now(timezone.utc).isoformat()
    result = paginate(
        db,
        service.list_query(
            search=search, status_filter=status_filter,
            closed_by_refactor=closed_by_refactor,
        ),
        page, size,
    )
    result["items"] = [service.serialize_promotion(db, p) for p in result["items"]]
    return result


@router.post("", response_model=PromotionResponse,
             status_code=status.HTTP_201_CREATED, summary="Crear promoción")
def create_promotion(body: PromotionCreate, db: Session = Depends(get_db),
                     user: User = Depends(require_tenant_admin),
                     tenant: Tenant = Depends(get_tenant)):
    ensure_unique(db, Promotion, Promotion.name, body.name, "Ya existe una promoción con ese nombre")
    promo = service.create(db, body, tenant=tenant)
    record_audit(db, action="create", entity="promotion", entity_id=promo.id,
                 user=user, payload={
                     "name": promo.name, "status": promo.status,
                     "rule_count": len(promo.rules),
                     "rule_types": [r.type for r in promo.rules],
                 })
    db.commit()
    db.refresh(promo)
    return service.serialize_promotion(db, promo)


@router.patch("/{promotion_id}", response_model=PromotionResponse,
              summary="Actualizar campos escalares")
def update_promotion(promotion_id: UUID, body: PromotionUpdate, db: Session = Depends(get_db),
                     user: User = Depends(require_tenant_admin),
                     tenant: Tenant = Depends(get_tenant)):
    promo = get_or_404(db, Promotion, promotion_id, "Promoción no encontrada")
    if "name" in body.model_fields_set and body.name is not None:
        ensure_unique(db, Promotion, Promotion.name, body.name,
                      "Ya existe una promoción con ese nombre", exclude_id=promo.id)
    before = {f: getattr(promo, f) for f in body.model_fields_set if hasattr(promo, f)}
    promo = service.update(db, promo, body, tenant=tenant)
    record_audit(db, action="update", entity="promotion", entity_id=promo.id, user=user,
                 payload={"before": {k: str(v) for k, v in before.items()},
                          "after": {k: str(getattr(promo, k)) for k in before}})
    db.commit()
    db.refresh(promo)
    return service.serialize_promotion(db, promo)


@router.patch("/{promotion_id}/shape", response_model=PromotionResponse,
              summary="Cambiar reglas y conjunto de variantes (borrador o pausada)")
def update_promotion_shape(promotion_id: UUID, body: PromotionShapeUpdate,
                           db: Session = Depends(get_db),
                           user: User = Depends(require_tenant_admin)):
    promo = get_or_404(db, Promotion, promotion_id, "Promoción no encontrada")
    promo = service.update_shape(db, promo, body)
    record_audit(db, action="update_shape", entity="promotion", entity_id=promo.id,
                 user=user, payload={
                     "rule_count": len(promo.rules),
                     "rule_types": [r.type for r in promo.rules],
                 })
    db.commit()
    db.refresh(promo)
    return service.serialize_promotion(db, promo)


@router.patch("/{promotion_id}/status", response_model=PromotionResponse,
              summary="Activar, pausar o finalizar")
def change_promotion_status(promotion_id: UUID, body: PromotionStatusUpdate,
                            db: Session = Depends(get_db),
                            user: User = Depends(require_tenant_admin)):
    promo = get_or_404(db, Promotion, promotion_id, "Promoción no encontrada")
    previous = promo.status
    promo = service.change_status(db, promo, body.status.value)
    record_audit(db, action="status", entity="promotion", entity_id=promo.id,
                 user=user, payload={"from": previous, "to": promo.status})
    db.commit()
    db.refresh(promo)
    return service.serialize_promotion(db, promo)


@router.post("/{promotion_id}/duplicate", response_model=PromotionResponse,
             status_code=status.HTTP_201_CREATED, summary="Duplicar promoción")
def duplicate_promotion(promotion_id: UUID, body: PromotionDuplicate,
                        db: Session = Depends(get_db),
                        user: User = Depends(require_tenant_admin)):
    promo = get_or_404(db, Promotion, promotion_id, "Promoción no encontrada")
    existing = db.execute(
        select(Promotion).where(Promotion.name == body.name)
    ).scalar_one_or_none()
    replaced = None
    if existing is None:
        copy = service.duplicate(db, promo, body.name)
    elif not body.replace_existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ya existe una promoción con ese nombre")
    else:
        # spec 084 (A-83): la promoción anterior con ese nombre se elimina y la copia ocupa su lugar.
        replaced = {"id": str(existing.id), "name": existing.name, "status": existing.status}
        copy = service.duplicate_replacing(db, promo, body.name, existing)
        record_audit(db, action="delete", entity="promotion", entity_id=UUID(replaced["id"]),
                     user=user, payload={"name": replaced["name"], "replaced_by": str(copy.id)})
    record_audit(db, action="duplicate", entity="promotion", entity_id=copy.id,
                 user=user, payload={"source_id": str(promo.id), "name": copy.name,
                                     **({"replaced": replaced} if replaced else {})})
    db.commit()
    db.refresh(copy)
    return service.serialize_promotion(db, copy)


@router.delete("/{promotion_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar promoción")
def delete_promotion(promotion_id: UUID, db: Session = Depends(get_db),
                     user: User = Depends(require_tenant_admin)):
    promo = get_or_404(db, Promotion, promotion_id, "Promoción no encontrada")
    record_audit(db, action="delete", entity="promotion", entity_id=promo.id,
                 user=user, payload={"name": promo.name})
    db.delete(promo)
    db.commit()
