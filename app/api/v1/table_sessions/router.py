"""Sesiones de mesa (staff). El comensal anónimo no entra aquí: su vía es
`/cart`, autenticada por el token de sesión firmado.

La apertura no tiene endpoint propio: una sesión de mesa nace cuando el primer
comensal escanea el QR (`POST /cart/sessions`). Aquí se consulta, se cobra y se
cierra.
"""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.db import get_db, get_tenant
from app.core.dependencies import get_current_user
from app.core.models import Tenant, User
from app.api.v1.table_sessions import service
from app.api.v1.table_sessions.schemas import (
    AssignmentsIn, CloseSessionIn, CloseSessionResponse, ParticipantCreateIn,
    ParticipantResponse, ReleaseSessionResponse, SessionBillResponse,
    TableSessionResponse,
)

router = APIRouter(prefix="/table-sessions", tags=["table-sessions"])


@router.get(
    "",
    response_model=list[TableSessionResponse],
    summary="Listar sesiones de mesa",
)
def list_sessions(
    only_active: bool = Query(True, description="Solo las sesiones abiertas."),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return service.list_sessions(db, only_active=only_active)


@router.get(
    "/{table_session_id}",
    response_model=TableSessionResponse,
    summary="Detalle de una sesión de mesa (con sus comensales)",
)
def get_session(
    table_session_id: UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return service.get_session(db, table_session_id)


# ------------------------------------------- Reparto de la cuenta (staff)

@router.post(
    "/{table_session_id}/participants",
    response_model=ParticipantResponse,
    status_code=201,
    summary="Agregar un comensal desde el POS (sin QR), para dividir la cuenta",
)
def add_participant(
    table_session_id: UUID,
    body: ParticipantCreateIn,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_tenant),
    _: User = Depends(get_current_user),
):
    """Para el caso en que una sola persona pidió por todos: el staff crea a los demás
    y luego les reparte los productos. No emite token: no puede pedir por el QR."""
    return service.add_participant(db, table_session_id, body.display_name,
                                   tenant_id=tenant.id)


@router.delete(
    "/{table_session_id}/participants/{participant_id}",
    status_code=204,
    summary="Quitar un comensal (solo si no tiene productos asignados)",
)
def remove_participant(
    table_session_id: UUID,
    participant_id: UUID,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_tenant),
    _: User = Depends(get_current_user),
):
    service.remove_participant(db, table_session_id, participant_id, tenant_id=tenant.id)


@router.put(
    "/{table_session_id}/assignments",
    response_model=SessionBillResponse,
    summary="Repartir productos entre comensales (en lote)",
)
def set_assignments(
    table_session_id: UUID,
    body: AssignmentsIn,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_tenant),
    _: User = Depends(get_current_user),
):
    """Devuelve la cuenta ya recalculada: el POS necesita el desglose nuevo justo
    después de repartir, y pedirlo aparte abriría una ventana de inconsistencia."""
    return service.set_assignments(db, table_session_id, body.assignments,
                                   tenant_id=tenant.id, tenant=tenant)


@router.get(
    "/{table_session_id}/bill",
    response_model=SessionBillResponse,
    summary="Cuenta de la sesión, con el desglose por comensal",
)
def session_bill(
    table_session_id: UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    # `Annotated` + `None` (no `= Depends(...)`): `test_session_bill_endpoint_delega_en_compute_bill`
    # (CONGELA) invoca esta función directo, sin `tenant`; por HTTP FastAPI resuelve la dependencia.
    tenant: Annotated[Tenant, Depends(get_tenant)] = None,
):
    """El desglose sale de `order_items.participant_id`, que es asignación por
    ítem: el reparto es exacto aunque un pedido mezcle comensales."""
    return service.compute_bill(db, table_session_id, tenant)


@router.post(
    "/{table_session_id}/close",
    response_model=CloseSessionResponse,
    summary="Cobrar y cerrar la sesión (billing_mode unified o split)",
)
def close_session(
    table_session_id: UUID,
    body: CloseSessionIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    """Cierra en cascada: cobra, marca los pedidos `pagada`, cierra a los
    comensales, abandona sus carritos y libera la mesa. Todo en una transacción."""
    return service.close_session(
        db, table_session_id, body, user,
        invoice_prefix=tenant.invoice_prefix or "",
        tenant_id=tenant.id,
        tenant=tenant,
    )


@router.post(
    "/{table_session_id}/release",
    response_model=ReleaseSessionResponse,
    summary="Liberar una sesión ya completamente pagada (sin nada por cobrar)",
)
def release_paid_session(
    table_session_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_tenant),
):
    """Para el modo híbrido (spec 028, T027/T028): cuando cada comanda se cobró
    por separado (`POST /orders/{id}/checkout-and-send`) y no queda ninguna
    venta pendiente, `close_session` no aplica —exige algo billable—; este
    endpoint solo cierra la sesión y libera la mesa."""
    return service.release_paid_session(db, table_session_id, user, tenant_id=tenant.id)
