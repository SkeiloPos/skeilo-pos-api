import uuid
from datetime import datetime

from sqlalchemy import Integer,String,Column,DateTime, UniqueConstraint,func,Boolean,MetaData,ForeignKey,Index,CheckConstraint,text,Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm  import mapped_column,Mapped,DeclarativeBase,relationship,validates

from typing import Optional,List

convention = {
    'all_column_names': lambda constraint, table: '_'.join(
        [column.name for column in constraint.columns.values()]
    ),
    'ix': 'ix__%(table_name)s__%(all_column_names)s',
    'uq': 'uq__%(table_name)s__%(all_column_names)s',
    'ck': 'ck__%(table_name)s__%(constraint_name)s',
    'fk': 'fk__%(table_name)s__%(all_column_names)s__%(referred_table_name)s',
    'pk': 'pk__%(table_name)s',
}

class Base(DeclarativeBase):
     metadata = MetaData(schema="tenant",naming_convention=convention)

class TimestampMixin:
    created_at = Column(
        DateTime,
        server_default=func.now(),
        nullable=False,
        comment="Record creation time"
    )
    updated_at = Column(
        DateTime,
        onupdate=func.now(),
        nullable=True,
        comment="Unique record identifier"
    )
    
class UUIDPrimaryKeyMixin:
    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="Unique record identifier"
    )
class Tenant(Base,TimestampMixin):
    __tablename__ = "tenants"

    id:Mapped[int] = mapped_column("id",Integer, primary_key=True, nullable=False)
    
    name:Mapped[str] = mapped_column("name",String(255), nullable=False, index=True, unique=True)
    
    schema:Mapped[str] = mapped_column("schema", String(255), nullable=False, unique=True)

    # Plan de suscripción vigente (spec 033). FK NOT NULL: garantiza FR-003
    # ("todo tenant tiene, en todo momento, exactamente un plan vigente") a
    # nivel de esquema, sin validación de aplicación adicional. Reemplaza a
    # la columna heredada `plan` (texto libre sin uso real, research.md
    # Decisión 2).
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shared.plans.id"), nullable=False
    )
    plan: Mapped["Plan"] = relationship()

    # Ciclo de facturación de la asignación vigente ("mensual"/"anual") y las
    # fechas que lo respaldan. Los tres son NULL a la vez cuando el tenant
    # nunca vence (plan de transición o elegido explícitamente "sin
    # vencimiento", FR-021) — research.md Decisiones 10 y 12.
    ciclo_facturacion: Mapped[Optional[str]] = mapped_column("ciclo_facturacion", String(10), nullable=True)
    plan_iniciado_en: Mapped[Optional[datetime]] = mapped_column("plan_iniciado_en", DateTime, nullable=True)
    plan_vence_en: Mapped[Optional[datetime]] = mapped_column("plan_vence_en", DateTime, nullable=True)

    host:Mapped[str] = mapped_column("host", String(255), nullable=False, unique=True)

    logo_url:Mapped[Optional[str]] = mapped_column("logo_url", String(500), nullable=True)

    # Mensaje de agradecimiento que cierra la factura impresa. NULL = el front usa
    # su texto por defecto.
    receipt_message:Mapped[Optional[str]] = mapped_column("receipt_message", String(255), nullable=True)

    # Prefijo del consecutivo de facturación (`invoices.prefix`). Cada prefijo
    # lleva su propia numeración, así que a futuro mapea a una resolución DIAN.
    # NULL = sin prefijo, numeración corrida.
    invoice_prefix:Mapped[Optional[str]] = mapped_column("invoice_prefix", String(20), nullable=True)

    # Zona horaria IANA del negocio de este tenant (spec 030, A-46). Nace en el alta
    # (`tenant_create`, spec 138: el Super Admin la elige de una lista curada) y, para un tenant
    # ya creado, solo se cambia vía `app/scripts/set_tenant_timezone.py` (sin pantalla de edición).
    # `America/Bogota` cubre a todo tenant existente y al alta por API/script vía server_default.
    # Un disparador de `shared.tenants` aplica la misma regla de validez en la base de datos.
    timezone:Mapped[str] = mapped_column(
        "timezone", String(255), nullable=False, server_default="America/Bogota"
    )

    # Retención (días) de las notificaciones persistidas del tenant (spec 077,
    # RNF-005). Sin pantalla de edición: mismo mecanismo manual que `timezone`.
    # `purge_at` de cada `NotificationEvent` se calcula con el valor vigente al
    # crearla, no se recalcula si esta columna cambia después.
    notification_retention_days:Mapped[int] = mapped_column(
        "notification_retention_days", Integer, nullable=False, server_default="90"
    )

    # Permite capturar una línea de compra en una unidad distinta de la base del insumo
    # (spec 134, FR-016). `true` preserva el comportamiento de todo tenant existente.
    unit_conversion_enabled:Mapped[bool] = mapped_column(
        "unit_conversion_enabled", Boolean, nullable=False, server_default="true", default=True
    )

    # Estado mínimo de solo lectura (spec 159, research.md Decisión 9) — exclusivamente para
    # reflejar en la ficha de usuario si uno de sus negocios está suspendido. Sin pantalla de
    # administración propia en esta spec (fuera de alcance del módulo de Tenants, spec 033).
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")

    users: Mapped[list["User"]] = relationship(
            back_populates="tenant",
            cascade="all, delete-orphan"
        )

    __table_args__ = ({"schema": "shared"},)

    @validates("timezone")
    def _validate_timezone(self, key, value):
        # Import diferido: `app.core.timezone` importa `Tenant` de este módulo a nivel de módulo.
        from app.core.timezone import validate_iana_timezone

        # Regla común a todos los caminos (spec 138, FR-010): zona reconocida Y forma `Área/Lugar` o `UTC`
        # (rechaza `localtime`, `Factory`…, que `ZoneInfo` aceptaba).
        return validate_iana_timezone(value)

class Role(UUIDPrimaryKeyMixin,TimestampMixin,Base):
    __tablename__ = "roles"    
    
    name:Mapped[str] = mapped_column(String(150),nullable=False)
    
    active:Mapped[bool] = mapped_column(Boolean, default=True)
    
    users:Mapped[List["User"]] = relationship(back_populates="role")
    
    __table_args__ = ({"schema": "shared"},) 


class User(UUIDPrimaryKeyMixin,TimestampMixin,Base):
    __tablename__ = "users"

    name:Mapped[str] = mapped_column(String(150),nullable=False)
    
    email:Mapped[str] = mapped_column(String(255),nullable=False,index=True)
    
    password_hash:Mapped[str] =  mapped_column(String(255),nullable=False)
    
    phone:Mapped[Optional[str]] =  mapped_column(String(20),nullable=True)
    
    active:Mapped[bool] = mapped_column(Boolean, default=True)

    must_change_password:Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Corte de sesiones (spec 031): cualquier JWT con `iat` anterior a este
    # instante deja de aceptarse (ver app/core/dependencies.py). NULL hasta el
    # primer cambio de contraseña bajo esta spec — no afecta cuentas existentes.
    tokens_valid_after:Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=None)

    # Estado enriquecido (spec 159, research.md Decisión 2). `active` (arriba)
    # sigue siendo el único campo que lee `core/dependencies.py` — este módulo
    # lo escribe siempre en conjunto con `status`, nunca lo reemplaza.
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    suspended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    suspended_reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    security_blocked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    anonymized_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    memberships: Mapped[List["TenantMembership"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    sessions: Mapped[List["UserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    role_id: Mapped[UUID] = mapped_column(ForeignKey("shared.roles.id"))

    role:Mapped[Optional["Role"]] = relationship(back_populates="users")
    
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("shared.tenants.id"),
        nullable=True,
        index=True
    )

    tenant: Mapped["Tenant"] = relationship(
        back_populates="users"
    )

    __table_args__ = (UniqueConstraint(
        "tenant_id",
        "email",
        name="uq_user_tenant_email"
    ),{"schema": "shared"},)

    @property
    def role_name(self) -> Optional[str]:
        return self.role.name if self.role else None

    @property
    def tenant_name(self) -> Optional[str]:
        return self.tenant.name if self.tenant else None


class PasswordResetToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Enlace de un solo uso para restablecer contraseña sin sesión (spec 031).

    Solo se persiste el hash SHA-256 del token crudo enviado por correo —
    igual que una contraseña, el valor crudo nunca vive en la base de datos
    (research.md Decisión 2). El estado (vigente/usado/invalidado/caducado) se
    deriva al leer, no es una columna — ver data-model.md.
    """

    __tablename__ = "password_reset_tokens"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("shared.users.id"), nullable=False, index=True
    )

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    # Correo de la cuenta al emitir el enlace; un cambio de correo posterior
    # invalida el enlace en vivo al comparar contra user.email (FR-012).
    email_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)

    issued_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=None)
    invalidated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=None)

    user: Mapped["User"] = relationship()

    __table_args__ = ({"schema": "shared"},)


class UserInvitation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Invitación pendiente de alta de un usuario interno por correo (spec 037).

    Una sola fila por correo+tenant mientras `status='pending'` — reenviar
    (FR-010) sobrescribe `password_hash`/`sent_at` de la misma fila en vez de
    crear una nueva (research.md Decisión 2). Al consumirse desde
    `POST /auth/login` da origen a un `User` (research.md Decisión 7); ver
    data-model.md.
    """

    __tablename__ = "user_invitations"

    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("shared.tenants.id"), nullable=False, index=True
    )

    email: Mapped[str] = mapped_column(String(255), nullable=False)

    # spec 091: nombre completo capturado al invitar. Nulo en las invitaciones
    # anteriores (el correo saluda "Hola:" y la cuenta toma el correo como nombre).
    name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    role_id: Mapped[UUID] = mapped_column(ForeignKey("shared.roles.id"), nullable=False)

    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default="pending")

    sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    tenant: Mapped["Tenant"] = relationship()
    role: Mapped["Role"] = relationship()

    @property
    def role_name(self) -> Optional[str]:
        return self.role.name if self.role else None

    __table_args__ = (
        # A lo sumo una invitación 'pending' por (tenant, correo) a la vez
        # (FR-015, research.md Decisión 3). `sqlite_where` además de
        # `postgresql_where` para que los characterization tests (SQLite en
        # memoria) enfrenten el mismo IntegrityError que produciría Postgres.
        Index(
            "idx_pending_invitation_per_tenant_email",
            "tenant_id",
            "email",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        CheckConstraint(
            "status IN ('pending', 'consumed', 'cancelled')",
            name="ck_user_invitations_status",
        ),
        {"schema": "shared"},
    )


class PlatformLegalDocument(UUIDPrimaryKeyMixin, Base):
    """Una fila por versión de un documento legal de plataforma (Privacidad o
    Términos) — spec 162. Máquina de estados borrador/publicado/superado
    aplicada a nivel de servicio (FR-003): la edición solo se permite con
    `estado='borrador'`, nunca por constraint de BD. `UNIQUE (tipo) WHERE
    estado='publicado'` (FR-004, data-model.md tabla 1) es la barrera real
    contra dos publicaciones concurrentes del mismo `tipo`: la segunda en
    comitear choca contra el índice y recibe 409 del servicio, no corrompe
    el estado."""

    __tablename__ = "platform_legal_documents"

    tipo: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    contenido_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    estado: Mapped[str] = mapped_column(String(12), nullable=False, server_default="borrador")
    # blake2b-128 hex de `contenido_markdown` (research.md Decisión 4, mismo
    # algoritmo que `http_cache.py::weak_etag`).
    content_hash: Mapped[str] = mapped_column(String(32), nullable=False)
    fecha_vigencia: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    published_by_user_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("shared.users.id"), nullable=True
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    published_by: Mapped[Optional["User"]] = relationship()

    __table_args__ = (
        Index(
            "idx_platform_legal_document_vigente_por_tipo",
            "tipo",
            unique=True,
            postgresql_where=text("estado = 'publicado'"),
            sqlite_where=text("estado = 'publicado'"),
        ),
        Index("idx_platform_legal_document_tipo_version", "tipo", "version"),
        CheckConstraint(
            "tipo IN ('privacidad', 'terminos')",
            name="ck_platform_legal_documents_tipo",
        ),
        CheckConstraint(
            "estado IN ('borrador', 'publicado', 'superado')",
            name="ck_platform_legal_documents_estado",
        ),
        {"schema": "shared"},
    )


class MasterLegalTemplate(UUIDPrimaryKeyMixin, Base):
    """Una fila por versión de la plantilla legal maestra que cada tenant
    rellena con sus propios datos — spec 162. Misma máquina de estados que
    `PlatformLegalDocument`, pero a lo sumo una fila `publicado` en total (no
    por `tipo`: solo hay un tipo de plantilla), de ahí el índice único sobre
    `estado` en vez de sobre una columna de tipo (data-model.md tabla 2)."""

    __tablename__ = "master_legal_templates"

    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Markdown con placeholders `{{variable}}` de la lista cerrada
    # (`app/core/legal_template.py::ALLOWED_TEMPLATE_VARIABLES`, FR-008).
    contenido_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    estado: Mapped[str] = mapped_column(String(12), nullable=False, server_default="borrador")
    content_hash: Mapped[str] = mapped_column(String(32), nullable=False)
    fecha_vigencia: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    published_by_user_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("shared.users.id"), nullable=True
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    published_by: Mapped[Optional["User"]] = relationship()

    __table_args__ = (
        Index(
            "idx_master_legal_template_vigente",
            "estado",
            unique=True,
            postgresql_where=text("estado = 'publicado'"),
            sqlite_where=text("estado = 'publicado'"),
        ),
        CheckConstraint(
            "estado IN ('borrador', 'publicado', 'superado')",
            name="ck_master_legal_templates_estado",
        ),
        {"schema": "shared"},
    )


class TenantAdminPlatformAcceptance(UUIDPrimaryKeyMixin, Base):
    """Evidencia de que un administrador de tenant aceptó la versión vigente
    de un documento legal de plataforma (FR-019, spec 162) — separada de
    `ClientConsentAcceptance` (aceptación del cliente final, schema tenant).
    `UNIQUE (user_id, platform_document_id)` hace idempotente reenviar la
    aceptación (data-model.md tabla 3)."""

    __tablename__ = "tenant_admin_platform_acceptances"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("shared.users.id"), nullable=False)
    # Denormalizado desde user.tenant_id para consultas directas por tenant.
    tenant_id: Mapped[int] = mapped_column(ForeignKey("shared.tenants.id"), nullable=False, index=True)
    platform_document_id: Mapped[UUID] = mapped_column(
        ForeignKey("shared.platform_legal_documents.id"), nullable=False
    )
    accepted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    user: Mapped["User"] = relationship()
    tenant: Mapped["Tenant"] = relationship()
    platform_document: Mapped["PlatformLegalDocument"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "user_id", "platform_document_id",
            name="uq_tenant_admin_platform_acceptance_user_document",
        ),
        {"schema": "shared"},
    )
