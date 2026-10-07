import logging
import uuid
from datetime import datetime, timezone

from contextlib import contextmanager
from fastapi import Depends,Request,HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import create_engine,MetaData,schema as sch,inspect,text

from typing import Optional

from alembic import command
from alembic import script
from alembic.config import Config
from alembic.runtime.migration import MigrationContext

import bcrypt

from app.core.config import settings
from app.core.models import Tenant, User, convention, Base
import app.models  # registra modelos tenant en Base.metadata
from app.core.timezone import utc_now
from app.models.plan import Plan
from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError

# Import diferido (dentro de tenant_create) de app.core.plan_limits: ese
# módulo importa get_db/get_tenant de este mismo fichero (app.core.db) para
# construir require_module_access — importarlo aquí arriba crearía un ciclo
# db.py -> plan_limits.py -> db.py. calculate_plan_vencimiento/
# validate_billing_cycle_price no dependen de nada de este módulo, así que
# el import diferido es seguro y no reevalúa nada costoso por request.

alembic_config = Config("alembic.ini")
# En prod el echo se apaga: serializar cada SELECT a texto y escribirlo era el
# mayor coste por request. `SQL_ECHO` en el .env fuerza cualquiera de los dos.

engine = create_engine(settings.DATABASE_URL,echo=False,future=True)
logger = logging.getLogger(__name__)

@contextmanager
def with_db(tenant_schema: Optional[str]):
    if tenant_schema:
        schema_translate_map = dict(tenant=tenant_schema)
    else:
        schema_translate_map = None
    connectable = engine.execution_options(schema_translate_map=schema_translate_map)

    try:
        db = Session(autocommit=False, autoflush=False, bind=connectable)
        yield db
    finally:
        db.close()
        
        
def tenant_create(
    name: str,
    schema: str,
    host: str,
    admin_name: str,
    admin_email: str,
    admin_password: str,
    plan_id: uuid.UUID,
    ciclo_facturacion: Optional[str],
) -> None:
    from app.core.plan_limits import calculate_plan_vencimiento, validate_billing_cycle_price

    with with_db(schema) as db:
        try:
            context = MigrationContext.configure(db.connection())
            SCRIPT = script.ScriptDirectory.from_config(alembic_config)
            if context.get_current_revision() != SCRIPT.get_current_head():
                raise RuntimeError(
                    "Database is not up-to-date. Execute migrations before adding new tenants."
                )

            # spec 033, FR-004/FR-017/FR-018: un tenant nunca se persiste sin
            # un plan_id válido ni con un ciclo de facturación sin precio.
            plan = db.get(Plan, plan_id)
            if plan is None:
                raise HTTPException(status_code=404, detail=f"El plan '{plan_id}' no existe")
            validate_billing_cycle_price(plan, ciclo_facturacion)

            plan_iniciado_en = utc_now().replace(tzinfo=None)
            plan_vence_en = calculate_plan_vencimiento(plan_iniciado_en, ciclo_facturacion)

            tenant = Tenant(
                name=name,
                host=host,
                schema=schema,
                plan_id=plan_id,
                ciclo_facturacion=ciclo_facturacion,
                plan_iniciado_en=plan_iniciado_en if ciclo_facturacion is not None else None,
                plan_vence_en=plan_vence_en,
            )
            db.add(tenant)
            db.flush()

            db.execute(sch.CreateSchema(schema))
            get_tenant_specific_metadata().create_all(bind=db.connection())

            admin_role = db.execute(
                text("SELECT id FROM shared.roles WHERE name = 'ADMIN' LIMIT 1")
            ).fetchone()

            if admin_role is None:
                raise RuntimeError("ADMIN role not found. Run migrations first.")

            password_hash = bcrypt.hashpw(admin_password.encode(), bcrypt.gensalt()).decode()
            user = User(
                name=admin_name,
                email=admin_email,
                password_hash=password_hash,
                role_id=admin_role[0],
                tenant_id=tenant.id,
                must_change_password=True,
            )
            db.add(user)

            db.commit()
       
        except IntegrityError as e:
            db.rollback()
            if isinstance(e.orig, UniqueViolation):
                logger.warning("Intento de crear tenant duplicado: schema='%s'", schema)
                raise HTTPException(status_code=409, detail=f"El tenant '{schema}' ya existe")
            logger.exception("IntegrityError inesperado durante tenant_create")
            raise HTTPException(status_code=500, detail="Internal server error")

        except HTTPException:
            # spec 033: deja pasar los 404/409 intencionales de la validación
            # de plan_id/ciclo_facturacion sin convertirlos en un 500 genérico.
            db.rollback()
            raise

        except Exception as e:
            db.rollback()
            logger.exception("Unexpected error creating tenant"+str(e))
            raise HTTPException(
                status_code=500,
                detail="Internal server error"
            )
    


def get_shared_metadata():
    """identify all shared tables we need to create
    Returns:
        Metadata: the information for specific table 
    """
    meta = MetaData(
        naming_convention=convention
    )
    for table in Base.metadata.tables.values():
        if table.schema != "tenant":
            table.to_metadata(meta)

    return meta


def get_tenant_specific_metadata():
    """identify all the tables we need to create
    Returns:
        Metadata: the information for specific tenant
    """
    meta = MetaData(schema="tenant")

    tenant_tables = [t for t in Base.metadata.tables.values() if t.schema == "tenant"]
    for table in tenant_tables:
        table.to_metadata(meta)

    # spec 070: una tabla de tenant puede referenciar, por FK, una tabla de
    # otro schema (hoy: payment_methods.catalog_id -> shared.payment_method_catalog,
    # spec 032). `to_metadata()` no puede resolver esa FK dentro de `meta` si
    # la tabla referenciada no está también copiada aquí — sin esto,
    # `create_all()` revienta con NoReferencedTableError antes de crear
    # ninguna tabla nueva del tenant. `checkfirst=True` (default de
    # `create_all`) evita que se intente recrear una tabla compartida que ya
    # existe.
    for table in tenant_tables:
        for fk in table.foreign_keys:
            target = fk.column.table
            if target.schema != "tenant" and target.key not in meta.tables:
                target.to_metadata(meta)

    return meta



def find_tenant_by_host(host_without_port: str) -> Optional[Tenant]:
    """Gemelo de `resolve_tenant_by_id`, pero por host en vez de por id.
    Extraído de `get_tenant()` (spec 100, research.md Decisión 2) para que
    `get_optional_tenant` pueda reutilizar la misma consulta sin pasar por el
    `404` que `get_tenant` sigue levantando sin cambios."""
    with with_db(None) as db:
        return db.query(Tenant).filter(Tenant.host == host_without_port).one_or_none()


def get_tenant(req: Request) -> Tenant:
    tenant_host = req.headers.get("x-tenant-host")
    logger.info(f"Obteniendo tenant para x-tenant-host: {tenant_host}")
    if not tenant_host:
        raise HTTPException(
            status_code=400, detail="Falta la cabecera 'x-tenant-host'"
        )
    host_without_port = tenant_host.split(":", 1)[0]

    tenant = find_tenant_by_host(host_without_port)
    logger.info(f"Host recibido: {host_without_port}, Tenant encontrado: {tenant.name if tenant else 'None'}")
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"Tenant not found for host '{host_without_port}'")

    # spec 074 (extensión de logging operativo, research.md § 8): efecto
    # colateral puro para `OperationalLogMiddleware`, que lee `request.state`
    # cuando la petición ya terminó en vez de repetir aquí la resolución del
    # tenant. No cambia qué devuelve esta función, ni cuándo falla, ni su firma.
    req.state.tenant_id = tenant.id

    return tenant

def resolve_tenant_by_id(tenant_id: int) -> Tenant:
    """Gemelo de get_tenant() pero resolviendo por id en vez de host. Lo usa el
    flujo público de QR/sesión, donde el tenant viaja firmado dentro del token
    (claim `t`) y no hay header x-tenant-host."""
    with with_db(None) as db:
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


def get_db(tenant: Tenant = Depends(get_tenant)):
    logger.info(f"Obteniendo DB para tenant: {tenant.name} (schema: {tenant.schema})")
    with with_db(tenant.schema) as db:
        yield db


ROLE_NAMES = ("SUPER_ADMIN", "ADMIN", "CASHIER", "MESERO")


def _seed_shared_data(db):
    """Seed base data (roles + super admin) into the shared schema.

    Runs during first-time initialization, right after the shared tables are
    created and before Alembic is stamped to head. Idempotent by name/email so
    it is safe if the shared tables already hold some rows.
    """
    now = datetime.now(timezone.utc)

    existing_roles = {
        row[0] for row in db.execute(text("SELECT name FROM shared.roles")).fetchall()
    }
    for name in ROLE_NAMES:
        if name in existing_roles:
            continue
        db.execute(
            text(
                "INSERT INTO shared.roles (id, name, active, created_at) "
                "VALUES (:id, :name, true, :created_at)"
            ),
            {"id": uuid.uuid4(), "name": name, "created_at": now},
        )

    super_admin_exists = db.execute(
        text("SELECT 1 FROM shared.users WHERE email = :email"),
        {"email": settings.SUPER_ADMIN_EMAIL},
    ).first()
    if super_admin_exists:
        logger.info("shared base data already present")
        return

    role_id = db.execute(
        text("SELECT id FROM shared.roles WHERE name = 'SUPER_ADMIN' LIMIT 1")
    ).scalar_one()

    password_hash = bcrypt.hashpw(
        settings.SUPER_ADMIN_PASSWORD.encode(), bcrypt.gensalt()
    ).decode()

    db.execute(
        text(
            "INSERT INTO shared.users "
            "(id, name, email, password_hash, active, must_change_password, role_id, tenant_id, created_at) "
            "VALUES (:id, :name, :email, :password_hash, true, true, :role_id, NULL, :created_at)"
        ),
        {
            "id": uuid.uuid4(),
            "name": settings.SUPER_ADMIN_NAME,
            "email": settings.SUPER_ADMIN_EMAIL,
            "password_hash": password_hash,
            "role_id": role_id,
            "created_at": now,
        },
    )
    logger.info("seeded roles and super admin user")


def initialize_database():

    with engine.begin() as db:
        context = MigrationContext.configure(db)
        if context.get_current_revision() is not None:
            logger.info("Database already exists.")
            return
        logger.info("creating schema 'shared'...")
        db.execute(sch.CreateSchema("shared"))

        logger.info("creating shared tables...")
        get_shared_metadata().create_all(bind=db)
        logger.info("seeding shared base data...")
        _seed_shared_data(db)
        logger.info("register versión in Alembic...")
        alembic_config.attributes["connection"] = db
        command.stamp(alembic_config, "head", purge=True)
        logger.info("The database has been initialized successfully.")
        

    