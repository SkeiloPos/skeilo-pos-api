"""Barrido de sesiones de mesa huérfanas.

Una mesa se marca `ocupada` cuando el primer comensal escanea el QR. La expiración
del comensal es **perezosa** —solo se evalúa cuando vuelve a llamar con su token—
así que quien cierra la pestaña no expira nunca y la mesa se quedaría ocupada
indefinidamente, sin poder recibir clientes. Este job es la red que lo evita.

Cierra por **dos criterios distintos**, no por un umbral con dos valores:

- **Abandonada sin pedir** (`EMPTY_SESSION_TTL_MINUTES`, 30 min): alguien escaneó
  y se fue sin pedir nada. No hay nada que perder, así que se suelta pronto.
- **Olvidada con consumo** (`TABLE_SESSION_MAX_HOURS`, 6 h): tope duro. Aquí sí
  puede haber pedidos, y bajar el umbral echaría a gente que sigue comiendo.

La distinción importa: el riesgo de soltar demasiado pronto una mesa **con** pedidos
es real (se cierra la sesión de alguien que está esperando su comida); el de soltar
una mesa que nadie usó, ninguno.

El camino normal no depende de este job: el comensal que pulsa "Salir" libera la
mesa en el acto (`POST /cart/leave`). Esto cubre a quien no pulsa nada.

Dos detalles que no son opcionales:

- **Itera todos los schemas de tenant.** El job no vive dentro de un request, así
  que no hay `x-tenant-host` ni token del que sacar el tenant: hay que recorrer
  `shared.tenants` y abrir una sesión por cada uno.
- **Toma un lock en Redis.** Con varios workers de uvicorn el scheduler arranca en
  cada proceso; sin el lock el barrido correría N veces en paralelo sobre las
  mismas filas.
"""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from app.core import events
from app.core.config import settings
from app.core.db import with_db
from app.core.redis import token_blocklist as redis

logger = logging.getLogger(__name__)

_LOCK_KEY = "lock:sweep_table_sessions"
_PROMO_LOCK_KEY = "lock:expire_promotions"
_NOTIFICATIONS_PURGE_LOCK_KEY = "lock:purge_notifications"


def _abandonadas_sin_pedir(db, now: datetime) -> list:
    """Sesiones que hay que soltar ya: **sin ningún pedido** y sin nadie activo
    desde hace más de `EMPTY_SESSION_TTL_MINUTES`.

    Es el caso de "alguien escaneó por curiosidad y se fue". No espera al tope de
    6 h porque no hay nada que perder: no se pidió nada.

    El detalle que hace que esto funcione: `expires_at` **no** es el instante de la
    última actividad, sino ése más `SESSION_TTL_MINUTES` (la ventana deslizante de
    4 h). Compararlo contra `now` a secas no dispararía nunca dentro de esas 4 h,
    que es justo el agujero que se quiere cerrar. Hay que revertir el desplazamiento
    para recuperar la última actividad real.
    """
    from app.models.session_participant import SessionParticipant
    from app.models.table_session import TableSession
    from app.api.v1.table_sessions.service import has_billable_orders

    limite = now - timedelta(minutes=settings.EMPTY_SESSION_TTL_MINUTES)
    # `expires_at - SESSION_TTL_MINUTES` = última actividad del comensal.
    ultima_actividad = SessionParticipant.expires_at - timedelta(
        minutes=settings.SESSION_TTL_MINUTES
    )

    candidatas = db.execute(
        select(TableSession).where(
            TableSession.status == "active",
            # Ningún comensal sigue dentro de su ventana de inactividad.
            ~select(SessionParticipant.id)
            .where(
                SessionParticipant.table_session_id == TableSession.id,
                SessionParticipant.status == "open",
                SessionParticipant.expires_at.is_not(None),
                ultima_actividad > limite,
            )
            .exists(),
        )
    ).scalars().all()

    return [ts for ts in candidatas if not has_billable_orders(db, ts.id)]


def _sweep_schema(schema: str, corte: datetime, tenant_id: int | None = None) -> int:
    """Suelta las sesiones vencidas de un tenant. Devuelve cuántas tocó."""
    from app.models.customer_order import CustomerOrder
    from app.models.dining_table import DiningTable
    from app.models.table_session import TableSession
    from app.api.v1.orders.checkout import (
        TERMINAL, close_participants, close_table_sessions, delete_orphan_carts,
    )
    from app.api.v1.table_sessions.service import has_billable_orders

    tocadas = 0
    #: (table_session_id, dining_table_id, número de mesa, ¿quedó libre?) de las
    #: sesiones realmente cerradas, para publicar los eventos tras el COMMIT.
    cerradas: list[tuple] = []
    with with_db(schema) as db:
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        # Dos criterios distintos, no uno con dos umbrales:
        #  - mesas abandonadas sin pedir → ventana corta (30 min);
        #  - sesiones olvidadas con consumo → tope duro de 6 h.
        vencidas = list(_abandonadas_sin_pedir(db, now))
        ids = {ts.id for ts in vencidas}

        vencidas += [
            ts for ts in db.execute(
                select(TableSession).where(
                    TableSession.status == "active",
                    TableSession.opened_at < corte,
                )
            ).scalars().all()
            if ts.id not in ids
        ]

        for ts in vencidas:
            table_id = ts.dining_table_id

            # Con consumo sin cobrar la sesión **no se cierra**: cerrarla dejaba la
            # mesa sin cuenta que cargar —`GET /table-sessions` solo lista las
            # activas— y por tanto imposible de cobrar desde la terminal. Se echa a
            # los comensales, que es lo que interesa del vencimiento, y la mesa
            # queda esperando al cajero.
            if has_billable_orders(db, ts.id):
                echados = close_participants(db, ts)
                logger.info(
                    "Sesión %s (schema %s) vencida con pedidos sin cobrar: se "
                    "cierran %d comensal(es) y la mesa queda pendiente de cobro",
                    ts.id, schema, echados,
                )
                tocadas += 1
                continue

            # `closed_by=None`: no la cerró un humano, la cerró el barrido.
            sessions = close_table_sessions(db, table_id, closed_by=None)

            # Segunda red: la mesa solo vuelve a 'libre' si tampoco le quedan
            # pedidos vivos **fuera** de esta sesión (los que quedaron sin
            # `table_session_id` en su día). Liberarla los volvería incobrables.
            pendientes = db.execute(
                select(CustomerOrder.id).where(
                    CustomerOrder.dining_table_id == table_id,
                    CustomerOrder.status.notin_(TERMINAL),
                ).limit(1)
            ).scalar()
            table = db.get(DiningTable, table_id)
            quedo_libre = table is not None and pendientes is None
            cerradas.append(
                (ts.id, table_id, table.number if table is not None else None, quedo_libre)
            )
            if quedo_libre:
                table.status = "libre"
                delete_orphan_carts(db, sessions)
            elif pendientes is not None:
                logger.warning(
                    "Mesa %s (schema %s) sigue ocupada: tiene pedidos sin cobrar "
                    "que no cuelgan de la sesión cerrada",
                    table_id, schema,
                )
            tocadas += 1

        if tocadas:
            db.commit()
            # Tras el COMMIT: el cajero ve el tablero moverse solo, y el comensal
            # cuya mesa se barrió recibe el aviso en vez de quedarse mirando una
            # pantalla congelada.
            if tenant_id is not None:
                for ts_id, table_id, table_num, libre in cerradas:
                    events.session_closed(
                        tenant_id, table_session_id=ts_id,
                        dining_table_id=table_id, reason="swept",
                    )
                    if libre:
                        events.table_status_changed(
                            tenant_id, dining_table_id=table_id,
                            table_number=table_num, status="libre",
                        )
    return tocadas


def sweep_orphan_sessions() -> int:
    """Recorre todos los tenants cerrando sesiones de mesa vencidas."""
    corte = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        hours=settings.TABLE_SESSION_MAX_HOURS
    )
    total = 0
    with with_db(None) as db:
        # También el `id`: hace falta para publicar los eventos de tiempo real,
        # que van a un stream por tenant.
        tenants = [(r[0], r[1]) for r in db.execute(
            text("SELECT id, schema FROM shared.tenants")
        ).fetchall()]

    for tenant_id, schema in tenants:
        try:
            total += _sweep_schema(schema, corte, tenant_id)
        except Exception:
            # Un tenant roto no debe impedir el barrido de los demás.
            logger.exception("Error barriendo sesiones del schema %s", schema)

    if total:
        logger.info("Barrido de sesiones: %d sesión(es) de mesa cerradas", total)
    return total


def expire_promotions() -> int:
    """Pasa a `finished` las promociones cuyo `ends_at` ya venció, en todos los
    tenants. `ends_at` se guarda en hora **local del tenant** (A-08, igual que
    `_valid_now`), así que el corte también tiene que calcularse en esa misma
    zona horaria por tenant -- comparar contra un único `now` en UTC corre el
    job ~5h (o lo que sea el offset del tenant) antes de tiempo, y SÍ afecta la
    vigencia real: `active_variant_set_rules` filtra `status == "active"` en
    SQL antes de llamar a `_valid_now()`, así que una promoción marcada
    `finished` de más deja de evaluarse aunque su `ends_at` siga vigente.
    `finished` es terminal: para revivir una promoción se duplica."""
    from sqlalchemy import update
    from app.models.promotion import Promotion

    total = 0
    with with_db(None) as db:
        tenants = db.execute(
            text("SELECT schema, timezone FROM shared.tenants")
        ).fetchall()

    for schema, tz_name in tenants:
        try:
            tz = ZoneInfo(tz_name) if tz_name else ZoneInfo(settings.TENANT_TIMEZONE)
            now_local = datetime.now(tz).replace(tzinfo=None)
            with with_db(schema) as db:
                # `update()` sobre el modelo (no SQL crudo): el `schema_translate_map`
                # de `with_db` solo traduce sentencias ORM, no texto plano con nombres
                # de tabla sin calificar.
                result = db.execute(
                    update(Promotion)
                    .where(Promotion.status.in_(("active", "paused", "draft")),
                           Promotion.ends_at.is_not(None), Promotion.ends_at < now_local)
                    .values(status="finished")
                )
                db.commit()
                total += result.rowcount
        except Exception:
            logger.exception("Error expirando promociones del schema %s", schema)

    if total:
        logger.info("Expiración de promociones: %d promoción(es) finalizada(s)", total)
    return total


async def _run_expire_promotions_with_lock() -> None:
    """Igual patrón que `_run_with_lock`: un solo worker corre el job por ciclo."""
    try:
        got = await redis.set(_PROMO_LOCK_KEY, "1", ex=3600, nx=True)
    except Exception:
        logger.warning("Redis no disponible; se omite la expiración de promociones", exc_info=True)
        return
    if not got:
        return  # otro worker lo está haciendo

    import anyio
    await anyio.to_thread.run_sync(expire_promotions)


async def _run_purge_notifications_with_lock() -> None:
    """Igual patrón que `_run_expire_promotions_with_lock` (spec 077, RNF-005)."""
    try:
        got = await redis.set(_NOTIFICATIONS_PURGE_LOCK_KEY, "1", ex=3600, nx=True)
    except Exception:
        logger.warning("Redis no disponible; se omite la purga de notificaciones", exc_info=True)
        return
    if not got:
        return  # otro worker lo está haciendo

    import anyio
    from app.core.notifications.purge import purge_expired_notifications

    await anyio.to_thread.run_sync(purge_expired_notifications)


async def _run_with_lock() -> None:
    """Un solo worker ejecuta el barrido por ciclo. El TTL del lock es la mitad
    del intervalo: si el proceso muere a medias, el siguiente ciclo lo retoma."""
    ttl = max(int(settings.SESSION_SWEEP_INTERVAL_MINUTES * 60 / 2), 30)
    try:
        got = await redis.set(_LOCK_KEY, "1", ex=ttl, nx=True)
    except Exception:
        logger.warning("Redis no disponible; se omite el barrido de sesiones", exc_info=True)
        return
    if not got:
        return  # otro worker lo está haciendo

    import anyio
    await anyio.to_thread.run_sync(sweep_orphan_sessions)


def start_scheduler():
    """Arranca el barrido periódico. Devuelve el scheduler (o None si APScheduler
    no está instalado, para no tumbar la app por una dependencia opcional)."""
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.interval import IntervalTrigger
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning(
            "APScheduler no está instalado: las sesiones de mesa huérfanas no se "
            "cerrarán solas. Instala 'apscheduler' o corre "
            "`python -m app.scripts.sweep_sessions` por cron."
        )
        return None

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _run_with_lock,
        IntervalTrigger(minutes=settings.SESSION_SWEEP_INTERVAL_MINUTES),
        id="sweep_orphan_table_sessions",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        _run_expire_promotions_with_lock,
        CronTrigger(hour=0, minute=0),
        id="expire_promotions",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        _run_purge_notifications_with_lock,
        CronTrigger(hour=0, minute=30),
        id="purge_notifications",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info(
        "Barrido de sesiones activo: cada %d min, cierra las de más de %d h",
        settings.SESSION_SWEEP_INTERVAL_MINUTES, settings.TABLE_SESSION_MAX_HOURS,
    )
    logger.info("Expiración de promociones activa: a medianoche")
    logger.info("Purga de notificaciones activa: a medianoche y media")
    return scheduler
