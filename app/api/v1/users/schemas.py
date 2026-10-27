from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field
from app.core.timezone import UtcDatetime


class RoleName(str, Enum):
    """Roles asignables por un admin de tenant (excluye SUPER_ADMIN)."""
    ADMIN = "ADMIN"
    CASHIER = "CASHIER"
    MESERO = "MESERO"


class UserRoleUpdate(BaseModel):
    role: RoleName = Field(
        ...,
        description="Nuevo rol del usuario: ADMIN, CASHIER o MESERO.",
        examples=["ADMIN"],
    )


class UserStatusUpdate(BaseModel):
    active: bool = Field(
        ...,
        description="Nuevo estado del usuario: true=activo, false=inactivo.",
        examples=[False],
    )


class UserResponse(BaseModel):
    id: UUID = Field(..., description="Identificador único del usuario.")
    name: str = Field(..., description="Nombre del usuario.")
    email: EmailStr = Field(..., description="Correo del usuario.")
    phone: str | None = Field(None, description="Teléfono del usuario.")
    active: bool = Field(..., description="Indica si el usuario está activo.")
    role_name: str | None = Field(None, description="Nombre del rol del usuario.")
    tenant_id: int | None = Field(None, description="Identificador del tenant al que pertenece.")
    tenant_name: str | None = Field(None, description="Nombre del tenant al que pertenece.")
    created_at: UtcDatetime = Field(..., description="Fecha de creación del registro.")
    updated_at: UtcDatetime | None = Field(None, description="Fecha de la última actualización.")

    model_config = ConfigDict(from_attributes=True)
