from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional
from uuid import UUID

from pydantic import AliasPath, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.v1.sales.schemas import PaymentMethodType
from app.core.reserved_hosts import is_reserved_subdomain
from app.core.timezone import UtcDatetime, validate_iana_timezone


class PaymentMethodFieldFormat(str, Enum):
    TEXT = "text"
    NUMERIC = "numeric"
    IMAGE = "image"


class PaymentMethodFieldDefinition(BaseModel):
    """Un campo de integración que el catálogo exige/permite a un tenant al
    activar el método (spec 032, FR-004). Validado en Pydantic, no por
    constraint de base de datos (research.md Decisión 2)."""

    key: str = Field(..., min_length=1, max_length=50, examples=["celular", "qr"])
    label: str = Field(..., min_length=1, max_length=150, examples=["Número de celular"])
    required: bool = False
    format: PaymentMethodFieldFormat
    length: int | None = Field(
        None, gt=0,
        description="Longitud exacta esperada; solo aplica a format='text'/'numeric'.",
    )

    @model_validator(mode="after")
    def _length_only_for_text_or_numeric(self):
        if self.length is not None and self.format == PaymentMethodFieldFormat.IMAGE:
            raise ValueError("`length` no aplica a un campo format='image'")
        return self


def _validate_unique_field_keys(fields: list[PaymentMethodFieldDefinition]) -> list[PaymentMethodFieldDefinition]:
    keys = [f.key for f in fields]
    if len(keys) != len(set(keys)):
        raise ValueError("Los `key` de `fields` deben ser únicos dentro del mismo método")
    return fields


class PaymentMethodCatalogCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, examples=["Daviplata"])
    type: PaymentMethodType
    fields: list[PaymentMethodFieldDefinition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_keys(self):
        _validate_unique_field_keys(self.fields)
        return self


class PaymentMethodCatalogUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    type: PaymentMethodType | None = None
    fields: list[PaymentMethodFieldDefinition] | None = None
    active: bool | None = None

    @model_validator(mode="after")
    def _unique_keys(self):
        if self.fields is not None:
            _validate_unique_field_keys(self.fields)
        return self


class PaymentMethodCatalogResponse(BaseModel):
    id: UUID
    name: str
    type: str
    active: bool
    fields: list[PaymentMethodFieldDefinition]
    created_at: datetime
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class TenantResponse(BaseModel):
    id: int = Field(..., description="Identificador único del tenant.")
    name: str = Field(..., description="Nombre del tenant.", examples=["Acme"])
    schema_name: str = Field(
        ..., alias="schema",
        description="Schema de PostgreSQL asignado al tenant.",
        examples=["tenant_acme"],
    )
    host: str = Field(..., description="Host asociado al tenant.", examples=["acme.localhost"])
    plan_id: UUID = Field(..., description="Plan de suscripción vigente del tenant.")
    plan_name: Optional[str] = Field(
        None, description="Nombre del plan vigente (spec 033).", validation_alias=AliasPath("plan", "name")
    )
    ciclo_facturacion: Optional[Literal["mensual", "anual"]] = Field(
        None, description="Ciclo de facturación de la asignación vigente (spec 033)."
    )
    plan_vence_en: Optional[datetime] = Field(
        None, description="Vencimiento de la asignación vigente; `null` = sin vencimiento (spec 033)."
    )
    created_at: datetime = Field(..., description="Fecha de creación del registro.")
    updated_at: datetime | None = Field(None, description="Fecha de la última actualización.")

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class TenantPlanUpdate(BaseModel):
    """Body de `PATCH /super-admin/tenants/{id}` — asigna, cambia O renueva
    el plan de un tenant (spec 033, research.md Decisión 16: las tres son la
    misma operación). `ciclo_facturacion` es obligatorio en el request (sin
    default) y acepta `null` como "sin vencimiento" (research.md Decisión 15
    — FR-004/FR-017/FR-021 a la vez: elección explícita, nunca implícita)."""

    plan_id: UUID
    ciclo_facturacion: Optional[Literal["mensual", "anual"]]


class PlanCreate(BaseModel):
    """Todas las características son opcionales (FR-001): una omitida se
    guarda en su default (0 para límites, false para accesos, bloqueada —
    FR-002); `null` explícito en un límite se guarda como "ilimitado"
    (FR-007). El router siempre pasa un valor explícito para las cinco
    características de límite al construir `Plan(...)` (nunca las omite),
    así que el `default=0` de estos campos es lo único que resuelve la
    ambigüedad "omitido" vs "enviado como null" — ver app/models/plan.py."""

    name: str = Field(..., min_length=1, max_length=100, examples=["Pro"])
    description: Optional[str] = Field(None, max_length=500)

    mesas_limit: Optional[int] = Field(0, ge=0)
    cajas_limit: Optional[int] = Field(0, ge=0)
    usuarios_limit: Optional[int] = Field(0, ge=0)
    productos_limit: Optional[int] = Field(0, ge=0)
    metodos_pago_activos_limit: Optional[int] = Field(0, ge=0)

    inventario_access: bool = False
    compras_access: bool = False
    promociones_access: bool = False

    precio_mensual: Optional[Decimal] = Field(None, ge=0, max_digits=12, decimal_places=2)
    precio_anual: Optional[Decimal] = Field(None, ge=0, max_digits=12, decimal_places=2)


class PlanUpdate(BaseModel):
    """Todos los campos opcionales — el router solo aplica los que el
    request incluyó explícitamente (`model_dump(exclude_unset=True)`), para
    distinguir "no lo toqué" de "lo puse en null" (ilimitado) en los cinco
    límites numéricos."""

    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)

    mesas_limit: Optional[int] = Field(None, ge=0)
    cajas_limit: Optional[int] = Field(None, ge=0)
    usuarios_limit: Optional[int] = Field(None, ge=0)
    productos_limit: Optional[int] = Field(None, ge=0)
    metodos_pago_activos_limit: Optional[int] = Field(None, ge=0)

    inventario_access: Optional[bool] = None
    compras_access: Optional[bool] = None
    promociones_access: Optional[bool] = None

    precio_mensual: Optional[Decimal] = Field(None, ge=0, max_digits=12, decimal_places=2)
    precio_anual: Optional[Decimal] = Field(None, ge=0, max_digits=12, decimal_places=2)


class PlanResponse(BaseModel):
    id: UUID
    name: str
    description: Optional[str] = None

    mesas_limit: Optional[int] = None
    cajas_limit: Optional[int] = None
    usuarios_limit: Optional[int] = None
    productos_limit: Optional[int] = None
    metodos_pago_activos_limit: Optional[int] = None

    inventario_access: bool
    compras_access: bool
    promociones_access: bool

    precio_mensual: Optional[Decimal] = None
    precio_anual: Optional[Decimal] = None

    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class TenantCreateWithUser(BaseModel):

    tenant_name:str = Field(...,min_length=3)
    schema_name:str = Field(...,min_length=3)
    host:str = Field(...,min_length=3)
    name:str = Field(...,min_length=3)
    email:str = Field(...,min_length=5)

    # spec 091, A-101: `admin`, `assets`, `api`, `docs`, `www` y `app` son
    # subdominios de la plataforma. Se compara recortado y en minúsculas, pero
    # el valor recibido se conserva sin transformar.
    @field_validator("host")
    @classmethod
    def _host_not_reserved(cls, value: str) -> str:
        if is_reserved_subdomain(value):
            raise ValueError(
                f"«{value.strip().lower()}» es una palabra reservada y no puede usarse como subdominio"
            )
        return value

    # spec 033, FR-004/FR-017: obligatorios, sin default — la creación de un
    # tenant no se completa sin elegir explícitamente un plan y su ciclo de
    # facturación (`ciclo_facturacion: null` es una elección válida de "sin
    # vencimiento", no un valor implícito, research.md Decisión 15).
    plan_id: UUID
    ciclo_facturacion: Optional[Literal["mensual", "anual"]]

    # spec 138, FR-010: zona horaria IANA del negocio. Opcional a propósito (aditivo): la pantalla siempre la
    # envía, pero la API y los scripts que no la mandan siguen creando el tenant en `America/Bogota`
    # (`server_default`). Si viene, se valida ANTES de crear nada (⇒ 422).
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def _timezone_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_iana_timezone(value)
