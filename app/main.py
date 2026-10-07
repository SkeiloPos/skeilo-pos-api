
import logging
from contextlib import asynccontextmanager

import sentry_sdk
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.core.error_middleware import (
    OperationalLogMiddleware,
    RequestIdMiddleware,
    register_error_handlers,
)
from app.core.redis import token_blocklist
from app.core.event_bus import event_bus
from app.core.scheduler import start_scheduler
from app.core.db import initialize_database
from app.api.v1.auth.routes import auth_router
from app.api.v1.categories.router import router as categories_router
from app.api.v1.presentations.router import router as presentations_router
from app.api.v1.unit_measures.router import router as unit_measures_router
from app.api.v1.products.router import router as products_router
from app.api.v1.users.router import router as users_router
from app.api.v1.invitations.router import router as invitations_router
from app.api.v1.super_admin.router import router as super_admin_router
from app.api.v1.menu.router import router as menu_router
from app.api.v1.orders.router import router as orders_router
from app.api.v1.catalog.router import router as catalog_router
from app.api.v1.inventory.router import router as inventory_router
from app.api.v1.cash.router import router as cash_router
from app.api.v1.sales.router import router as sales_router
from app.api.v1.uploads.router import router as uploads_router
from app.api.v1.cart.router import router as cart_router
from app.api.v1.table_sessions.router import router as table_sessions_router
from app.api.v1.invoices.router import router as invoices_router
from app.api.v1.health.router import router as health_router
from app.api.v1.tenant.router import router as tenant_router
from app.api.v1.reports.router import router as reports_router
from app.api.v1.promotions.router import router as promotions_router
from app.api.v1.audit.router import router as audit_router
from app.api.v1.plan.router import router as plan_router
from app.api.v1.realtime.router import router as realtime_router
from app.api.v1.notifications.router import router as notifications_router
from app.api.v1.bootstrap.router import router as bootstrap_router
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    force=True 
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 🚀 STARTUP
    scheduler = None
    try:
        initialize_database()  # mejor aquí
        await token_blocklist.ping()
        print("✅ Redis conectado correctamente")
        # Bus de tiempo real: abre su propia conexión Redis (un XREAD BLOCK
        # monopoliza la suya, y el pool del blocklist se usa en cada request).
        await event_bus.start()
        print("✅ Bus de eventos listo")
        # Cierra las sesiones de mesa abandonadas; sin esto una mesa que nadie
        # cobra queda 'ocupada' para siempre (el TTL del comensal es perezoso).
        scheduler = start_scheduler()
    except Exception as e:
        print("❌ Error en startup:", e)
        raise e  # opcional: detiene la app si falla Redis

    yield  # 👉 la app corre aquí

    # 🛑 SHUTDOWN
    if scheduler is not None:
        try:
            scheduler.shutdown(wait=False)
        except Exception as e:
            print("❌ Error deteniendo el scheduler:", e)
    try:
        # Antes que el blocklist: corta los lectores y suelta sus conexiones.
        await event_bus.aclose()
    except Exception as e:
        print("❌ Error cerrando el bus de eventos:", e)
    try:
        await token_blocklist.close()
        print("🔌 Redis cerrado")
    except Exception as e:
        print("❌ Error cerrando Redis:", e)

# Único prefijo con manejo de errores estandarizado por ahora (spec 068).
# Migrar otro módulo a este mismo patrón es agregar su prefijo aquí — sin
# duplicar app/core/domain_errors.py, error_response.py ni error_middleware.py.
SUPER_ADMIN_ERROR_PREFIX = "/api/v1/super-admin"


def create_app()->FastAPI:
    # Sentry solo se inicializa en producción y con DSN configurado
    # (research.md § 6): fuera de "prod", o sin SENTRY_DSN, nunca se llama a
    # sentry_sdk.init() y cualquier capture_exception posterior es un no-op.
    if settings.ENVIRONMENT == "prod" and settings.SENTRY_DSN:
        sentry_sdk.init(dsn=settings.SENTRY_DSN, environment=settings.ENVIRONMENT, enable_logs=True,)

    app = FastAPI(title="Pos", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://([a-z0-9-]+\.)?localhost:4200$|^https://([a-z0-9-]+\.)?skeilopos\.com$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # Con `allow_credentials=True` el comodín no vale aquí: hay que listar cada
        # cabecera que el JS del navegador pueda leer. `ETag` es la de la
        # revalidación condicional; `Retry-After` la que acompaña a los 429;
        # `X-Server-Time` (A-09) es la hora del servidor que el POS de staff usa
        # en vez del reloj del dispositivo para previsualizar vigencia de
        # promociones (GET /promotions).
        #
        # `X-Session-State` (spec 089, A-95): los 401 cuya causa es que la mesa se cerró llevan
        # `closed`; el Menú QR la lee para mostrar "¡Gracias por tu visita!" en vez de pedir el nombre.
        expose_headers=["ETag", "Retry-After", "X-Server-Time", "X-Session-State"],
    )
    app.add_middleware(RequestIdMiddleware, path_prefix=SUPER_ADMIN_ERROR_PREFIX)
    register_error_handlers(app, path_prefix=SUPER_ADMIN_ERROR_PREFIX)
    # spec 074 (FR-015–FR-021): log operativo de toda petición mutativa fuera
    # de super-admin. Sin `path_prefix`: el filtrado de alcance (método y
    # prefijo excluido) vive dentro de la clase. Se agrega de último a
    # propósito — Starlette monta el último agregado como el más externo —, así
    # la duración que mide cubre la petición completa (CORS incluido) y ninguna
    # respuesta se le escapa, la produzca el router o un middleware interno.
    # No colisiona con `RequestIdMiddleware`: sus alcances son mutuamente
    # excluyentes por ruta (ese solo actúa dentro de super-admin, este solo
    # fuera), así que ninguna petición recibe dos `request_id` distintos.
    app.add_middleware(OperationalLogMiddleware)

    initialize_database()

    
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(categories_router, prefix="/api/v1")
    app.include_router(presentations_router, prefix="/api/v1")
    app.include_router(unit_measures_router, prefix="/api/v1")
    app.include_router(products_router, prefix="/api/v1")
    app.include_router(users_router, prefix="/api/v1")
    app.include_router(invitations_router, prefix="/api/v1")
    app.include_router(super_admin_router, prefix="/api/v1")
    app.include_router(menu_router, prefix="/api/v1")
    app.include_router(orders_router, prefix="/api/v1")
    app.include_router(catalog_router, prefix="/api/v1")
    app.include_router(inventory_router, prefix="/api/v1")
    app.include_router(cash_router, prefix="/api/v1")
    app.include_router(sales_router, prefix="/api/v1")
    app.include_router(uploads_router, prefix="/api/v1")
    app.include_router(cart_router, prefix="/api/v1")
    app.include_router(table_sessions_router, prefix="/api/v1")
    app.include_router(invoices_router, prefix="/api/v1")
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(tenant_router, prefix="/api/v1")
    app.include_router(reports_router, prefix="/api/v1")
    app.include_router(promotions_router, prefix="/api/v1")
    app.include_router(audit_router, prefix="/api/v1")
    app.include_router(plan_router, prefix="/api/v1")
    app.include_router(realtime_router, prefix="/api/v1")
    app.include_router(notifications_router, prefix="/api/v1")
    app.include_router(bootstrap_router, prefix="/api/v1")
    return app

app = create_app()
