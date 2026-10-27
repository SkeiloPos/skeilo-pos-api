from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.api.v1.users.schemas import RoleName
from app.core.person_name import normalize_full_name
from app.core.timezone import UtcDatetime


class InvitationCreate(BaseModel):
    # spec 091 (A-100): obligatorio. `default=None` + `validate_default=True` hace
    # que omitir la clave, enviarla vacía o solo con espacios den el mismo 422
    # "El nombre es obligatorio" en vez de un genérico "Field required".
    name: Optional[str] = Field(
        default=None,
        validate_default=True,
        description=(
            "Nombre completo de la persona invitada (obligatorio): 2 a 100 caracteres, letras, "
            "espacios, apóstrofes y guiones, con al menos dos letras."
        ),
        examples=["María Pérez"],
    )
    email: EmailStr = Field(
        ...,
        description="Correo de la persona invitada. Único (por invitación pendiente o cuenta) dentro del tenant.",
        examples=["cajero1@acme.com"],
    )
    role: RoleName = Field(
        ...,
        description="Rol que tendrá la cuenta al consumir la invitación: ADMIN, CASHIER o MESERO.",
        examples=["CASHIER"],
    )

    @field_validator("name", mode="before")
    @classmethod
    def _validate_name(cls, value):
        return normalize_full_name(value)


class InvitationResponse(BaseModel):
    id: UUID = Field(..., description="Identificador único de la invitación.")
    email: EmailStr = Field(..., description="Correo de la persona invitada.")
    name: Optional[str] = Field(
        None, description="Nombre completo de la persona invitada; nulo en invitaciones anteriores."
    )
    role_name: str = Field(..., description="Nombre del rol asignado.")
    sent_at: UtcDatetime = Field(..., description="Fecha del último envío (creación o reenvío).")

    model_config = ConfigDict(from_attributes=True)
