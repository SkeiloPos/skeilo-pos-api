from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import with_db
from app.core.models import Tenant, User
from app.models.invoice import Invoice
from app.models.customer_order import CustomerOrder
from app.models.tenant_membership import TenantMembership


class RetentionTenant(TypedDict):
    tenant_id: int
    tenant_name: str


def find_retention_tenants(db: Session, user: User) -> list[RetentionTenant]:
    """Spec 159, research.md Decisión 7 / FR-024: antes de eliminar
    permanentemente a un usuario, revisa -- en el esquema de CADA negocio al
    que pertenece (sus `TenantMembership`) -- si existe algún registro
    fiscal/transaccional (`facturas`/`órdenes`) que referencie su `user_id`.
    Nunca modifica esas filas, solo verifica su existencia. Referenciado por
    `DELETE /super-admin/users/{user_id}` para bloquear el borrado con 409 y
    ofrecer anonimizar como alternativa."""
    tenant_rows = db.execute(
        select(Tenant.id, Tenant.name, Tenant.schema)
        .join(TenantMembership, TenantMembership.tenant_id == Tenant.id)
        .where(TenantMembership.user_id == user.id)
    ).all()

    retention_tenants: list[RetentionTenant] = []
    for tenant_id, tenant_name, tenant_schema in tenant_rows:
        with with_db(tenant_schema) as tenant_db:
            has_invoice = tenant_db.execute(
                select(Invoice.id).where(Invoice.user_id == user.id).limit(1)
            ).first()
            has_order = tenant_db.execute(
                select(CustomerOrder.id).where(CustomerOrder.user_id == user.id).limit(1)
            ).first()
        if has_invoice or has_order:
            retention_tenants.append({"tenant_id": tenant_id, "tenant_name": tenant_name})

    return retention_tenants
