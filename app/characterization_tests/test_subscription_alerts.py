"""Tests de la nueva funcionalidad — spec 161-suscripcion-pago-wompi, Historia de Usuario 5 (FR-028 a FR-032,
SC-008, SC-009; research.md Decisiones 8 y 12): tres avisos dentro de la aplicación, SOLO para los ADMIN, una
vez por ciclo de vencimiento, emitidos por un job periódico dentro de la ventana horaria LOCAL del tenant.

No son characterization tests — comportamiento enteramente nuevo. Dos bloques:

- `dispatch.notify_subscription_*`: qué se persiste, a quién va dirigido y la idempotencia por ciclo;
- `scheduler.evaluate_subscription_alerts`: qué aviso corresponde a cada estado, la ventana horaria con dos
  zonas distintas, el reinicio del ciclo tras un pago, y que un tenant roto no detiene a los demás.

La concurrencia entre instancias (lock de Redis e índice único parcial bajo PostgreSQL) se verifica con
`quickstart.md § 6`.

    python -m unittest app.characterization_tests.test_subscription_alerts -v
"""
from __future__ import annotations

import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy import func, select

from app.characterization_tests import plan_fixtures as fx
from app.core import events, scheduler
from app.core.config import settings
from app.core.notifications import dispatch
from app.core.subscription import compute_subscription_status
from app.models.notification_event import NotificationEvent
from app.models.subscription_payment import SubscriptionPayment

#: 15:00 UTC = 10:00 en Bogotá (UTC-5, dentro de 8–20) y 00:00 del día siguiente en Tokio (UTC+9, fuera).
UTC_DIA_BOGOTA = datetime(2026, 12, 6, 15, 0, 0)
#: 03:00 UTC = 22:00 en Bogotá (fuera) y 12:00 en Tokio (dentro).
UTC_DIA_TOKIO = datetime(2026, 12, 6, 3, 0, 0)


def _eventos(db, event_type=None):
    stmt = select(NotificationEvent).order_by(NotificationEvent.created_at)
    if event_type:
        stmt = stmt.where(NotificationEvent.event_type == event_type)
    return db.execute(stmt).scalars().all()


def _contar(db, *where) -> int:
    stmt = select(func.count()).select_from(NotificationEvent)
    if where:
        stmt = stmt.where(*where)
    return db.execute(stmt).scalar_one()


def _tenant(db, vence, *, tz="America/Bogota", plan=None):
    plan = plan or fx.make_plan(db)
    tenant = fx.make_tenant(db, plan=plan, ciclo_facturacion="mensual" if vence is not None else None, timezone=tz)
    if vence is not None:
        tenant.plan_iniciado_en = vence - timedelta(days=30)
        tenant.plan_vence_en = vence
    db.commit()
    return tenant


def _sin_red(testcase):
    """Evita que `InAppChannel` hable con Redis: la publicación en tiempo real no es lo que se prueba aquí."""
    patcher = mock.patch.object(events, "publish")
    publish = patcher.start()
    testcase.addCleanup(patcher.stop)
    return publish


def _ventana(testcase, start=8, end=20):
    patcher = mock.patch.multiple(
        settings, SUBSCRIPTION_ALERT_LOCAL_START_HOUR=start, SUBSCRIPTION_ALERT_LOCAL_END_HOUR=end
    )
    patcher.start()
    testcase.addCleanup(patcher.stop)


# ======================================================================
# dispatch.notify_subscription_*
# ======================================================================


class NotificacionesDeSuscripcionTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.publish = _sin_red(self)
        self.vence = datetime(2026, 12, 1, 5, 0, 0)

    def _tenant(self, vence=None, **kw):
        return _tenant(self.db, vence or self.vence, **kw)

    def test_cada_funcion_emite_su_tipo_dirigido_solo_a_admin(self):
        tenant = self._tenant()
        status = compute_subscription_status(self.vence, self.vence - timedelta(hours=10))
        for funcion, tipo in (
            (dispatch.notify_subscription_expiring, "subscription.expiring"),
            (dispatch.notify_subscription_grace_started, "subscription.grace_started"),
            (dispatch.notify_subscription_blocked, "subscription.blocked"),
        ):
            with self.subTest(tipo):
                notif = funcion(self.db, tenant, status)

                self.assertEqual(notif.event_type, tipo)
                self.assertEqual(notif.audience_role, "ADMIN")
                self.assertEqual(notif.related_entity_type, "subscription")
                self.assertIsNone(notif.attended_at)

    def test_se_publican_solo_en_el_canal_del_admin_nunca_en_el_comun(self):
        tenant = self._tenant()
        status = compute_subscription_status(self.vence, self.vence - timedelta(hours=10))

        dispatch.notify_subscription_expiring(self.db, tenant, status)

        self.assertEqual(self.publish.call_count, 1)
        self.assertEqual(list(self.publish.call_args.kwargs["channels"]), ["staff:admin"])

    def test_el_payload_lleva_las_fechas_los_dias_y_la_fecha_limite_en_la_zona_del_tenant(self):
        tenant = self._tenant()  # Bogotá
        status = compute_subscription_status(self.vence, self.vence + timedelta(hours=1))

        notif = dispatch.notify_subscription_grace_started(self.db, tenant, status)

        self.assertEqual(notif.payload["plan_vence_en"], "2026-12-01T05:00:00")
        self.assertEqual(notif.payload["fecha_limite_pago"], "2026-12-06T05:00:00")
        self.assertEqual(notif.payload["dias_restantes"], status.dias_restantes)
        # Límite 06-dic 05:00 UTC = 06-dic 00:00 en Bogotá (UTC-5).
        self.assertEqual(notif.payload["fecha_limite_pago_local"], "6 de diciembre")

    def test_la_fecha_limite_local_depende_de_la_zona_horaria_del_tenant(self):
        vence = datetime(2026, 12, 1, 2, 0, 0)  # límite = 06-dic 02:00 UTC
        status = compute_subscription_status(vence, vence + timedelta(hours=1))
        db_tokio = fx.new_session()  # cada tenant vive en su propio esquema; aquí, en su propia sesión
        bogota = self._tenant(vence)  # 05-dic 21:00 local
        tokio = _tenant(db_tokio, vence, tz="Asia/Tokyo")  # 06-dic 11:00 local

        local_bogota = dispatch.notify_subscription_grace_started(self.db, bogota, status).payload["fecha_limite_pago_local"]
        local_tokio = dispatch.notify_subscription_grace_started(db_tokio, tokio, status).payload["fecha_limite_pago_local"]

        self.assertEqual((local_bogota, local_tokio), ("5 de diciembre", "6 de diciembre"))

    def test_un_aviso_por_tipo_y_ciclo_el_segundo_intento_no_inserta_nada(self):
        tenant = self._tenant()
        status = compute_subscription_status(self.vence, self.vence - timedelta(hours=10))

        primero = dispatch.notify_subscription_expiring(self.db, tenant, status)
        segundo = dispatch.notify_subscription_expiring(self.db, tenant, status)

        self.assertIsNotNone(primero)
        self.assertIsNone(segundo)
        self.assertEqual(_contar(self.db), 1)
        self.assertEqual(self.publish.call_count, 1)  # y tampoco se vuelve a publicar

    def test_los_tres_tipos_del_mismo_ciclo_son_avisos_distintos(self):
        tenant = self._tenant()
        status = compute_subscription_status(self.vence, self.vence + timedelta(hours=1))

        for funcion in (dispatch.notify_subscription_expiring, dispatch.notify_subscription_grace_started, dispatch.notify_subscription_blocked):
            self.assertIsNotNone(funcion(self.db, tenant, status))

        self.assertEqual(_contar(self.db), 3)

    def test_un_nuevo_ciclo_tras_un_pago_permite_otra_vez_los_tres_avisos(self):
        tenant = self._tenant()
        status = compute_subscription_status(self.vence, self.vence - timedelta(hours=10))
        dispatch.notify_subscription_expiring(self.db, tenant, status)

        tenant.plan_vence_en = self.vence + timedelta(days=31)  # lo que hace el webhook al aprobar un pago
        self.db.commit()
        nuevo_status = compute_subscription_status(tenant.plan_vence_en, tenant.plan_vence_en - timedelta(hours=10))

        nuevo = dispatch.notify_subscription_expiring(self.db, tenant, nuevo_status)

        self.assertIsNotNone(nuevo)
        self.assertEqual(_contar(self.db, NotificationEvent.event_type == "subscription.expiring"), 2)

    def test_dos_tenants_con_el_mismo_vencimiento_no_se_estorban(self):
        """Cada tenant vive en su propio esquema; aquí, en su propia sesión."""
        otra_db = fx.new_session()
        a = self._tenant()
        b = _tenant(otra_db, self.vence)
        status = compute_subscription_status(self.vence, self.vence - timedelta(hours=10))

        self.assertIsNotNone(dispatch.notify_subscription_expiring(self.db, a, status))
        self.assertIsNotNone(dispatch.notify_subscription_expiring(otra_db, b, status))

    def test_un_tenant_sin_ningun_usuario_admin_igual_registra_el_aviso(self):
        tenant = self._tenant()  # no hay ningún usuario
        status = compute_subscription_status(self.vence, self.vence - timedelta(hours=10))

        self.assertIsNotNone(dispatch.notify_subscription_expiring(self.db, tenant, status))

    def test_la_retencion_del_tenant_aplica_igual_que_a_los_demas_eventos(self):
        tenant = self._tenant()
        tenant.notification_retention_days = 30
        self.db.commit()
        status = compute_subscription_status(self.vence, self.vence - timedelta(hours=10))

        notif = dispatch.notify_subscription_expiring(self.db, tenant, status)

        self.assertLessEqual(notif.purge_at - notif.created_at.replace(tzinfo=None), timedelta(days=30, seconds=5))

    def test_la_misma_entidad_relacionada_es_estable_para_un_ciclo_y_cambia_con_el_vencimiento(self):
        a = dispatch._subscription_entity_id("subscription.expiring", self.vence)
        b = dispatch._subscription_entity_id("subscription.expiring", self.vence)
        otro_ciclo = dispatch._subscription_entity_id("subscription.expiring", self.vence + timedelta(days=31))
        otro_tipo = dispatch._subscription_entity_id("subscription.blocked", self.vence)

        self.assertEqual(a, b)
        self.assertEqual(len({a, otro_ciclo, otro_tipo}), 3)
        self.assertIsInstance(a, uuid.UUID)


# ======================================================================
# El job: scheduler.evaluate_subscription_alerts
# ======================================================================


class JobDeAvisosTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.publish = _sin_red(self)
        _ventana(self)

        @contextmanager
        def _con_db(_schema):
            yield self.db

        patcher = mock.patch.object(scheduler, "with_db", _con_db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _evaluar(self, now=UTC_DIA_BOGOTA):
        return scheduler.evaluate_subscription_alerts(now)

    # ---------------------------------------------------------- qué aviso corresponde a cada estado

    def test_cada_estado_emite_solo_su_aviso(self):
        for estado, vence_rel, esperado in (
            ("por_vencer", timedelta(hours=10), "subscription.expiring"),
            ("en_gracia", -timedelta(hours=2), "subscription.grace_started"),
            ("bloqueada", -timedelta(days=10), "subscription.blocked"),
        ):
            with self.subTest(estado):
                db_antes = {e.id for e in _eventos(self.db)}
                _tenant(self.db, UTC_DIA_BOGOTA + vence_rel)

                emitidos = self._evaluar()
                nuevos = [e for e in _eventos(self.db) if e.id not in db_antes]

                self.assertEqual(emitidos, 1)
                self.assertEqual([e.event_type for e in nuevos], [esperado])

    def test_activa_y_sin_vencimiento_no_emiten_nada(self):
        _tenant(self.db, UTC_DIA_BOGOTA + timedelta(days=20))
        _tenant(self.db, None)

        self.assertEqual(self._evaluar(), 0)
        self.assertEqual(_contar(self.db), 0)

    def test_un_tenant_bloqueado_sin_avisos_previos_recibe_solo_el_de_bloqueo_fr032(self):
        """Tras una caída larga del job no se avisa «vence en 2 días» a quien ya está bloqueado."""
        _tenant(self.db, UTC_DIA_BOGOTA - timedelta(days=10))

        self._evaluar()

        self.assertEqual([e.event_type for e in _eventos(self.db)], ["subscription.blocked"])

    def test_los_bordes_exactos_de_48h_0h_y_120h(self):
        for vence_rel, esperado in (
            (timedelta(hours=48), "subscription.expiring"),          # justo a 48 h: ya es por_vencer
            (timedelta(hours=48, seconds=1), None),                  # un segundo antes: aún activa
            (timedelta(0), "subscription.grace_started"),            # en el instante del vencimiento
            (-timedelta(hours=120) + timedelta(seconds=1), "subscription.grace_started"),
            (-timedelta(hours=120), "subscription.blocked"),         # a 120 h exactas: bloqueada
        ):
            with self.subTest(vence_rel=vence_rel):
                db = fx.new_session()
                _tenant(db, UTC_DIA_BOGOTA + vence_rel)
                with mock.patch.object(scheduler, "with_db", lambda _s, db=db: _cm(db)):
                    scheduler.evaluate_subscription_alerts(UTC_DIA_BOGOTA)

                self.assertEqual([e.event_type for e in _eventos(db)], [esperado] if esperado else [])

    # ---------------------------------------------------------- idempotencia

    def test_correr_el_job_diez_veces_con_el_mismo_estado_emite_un_solo_aviso(self):
        _tenant(self.db, UTC_DIA_BOGOTA + timedelta(hours=10))

        totales = [self._evaluar() for _ in range(10)]

        self.assertEqual(totales, [1] + [0] * 9)
        self.assertEqual(_contar(self.db), 1)

    def test_pasar_de_un_estado_al_siguiente_emite_el_siguiente_aviso_una_vez(self):
        vence = UTC_DIA_BOGOTA + timedelta(hours=10)
        _tenant(self.db, vence)

        self._evaluar(UTC_DIA_BOGOTA)                         # por_vencer
        self._evaluar(vence + timedelta(hours=1))             # en_gracia (10:00 + 11 h = 21:00 local → fuera de ventana)
        self._evaluar(vence + timedelta(hours=1))
        self.assertEqual([e.event_type for e in _eventos(self.db)], ["subscription.expiring"])  # la ventana lo frenó

        with mock.patch.multiple(settings, SUBSCRIPTION_ALERT_LOCAL_START_HOUR=0, SUBSCRIPTION_ALERT_LOCAL_END_HOUR=24):
            self._evaluar(vence + timedelta(hours=1))
            self._evaluar(vence + timedelta(hours=130))       # bloqueada
            self._evaluar(vence + timedelta(hours=131))

        self.assertEqual(
            [e.event_type for e in _eventos(self.db)],
            ["subscription.expiring", "subscription.grace_started", "subscription.blocked"],
        )

    def test_un_pago_aprobado_reinicia_el_ciclo_y_vuelven_a_salir_los_avisos(self):
        tenant = _tenant(self.db, UTC_DIA_BOGOTA + timedelta(hours=10))
        self._evaluar()
        tenant.plan_vence_en = UTC_DIA_BOGOTA + timedelta(days=31, hours=10)  # el webhook movió el vencimiento
        self.db.commit()

        siguiente_por_vencer = tenant.plan_vence_en - timedelta(hours=10)  # 15:00 UTC → 10:00 Bogotá
        self.assertEqual(self._evaluar(siguiente_por_vencer), 1)

        self.assertEqual(_contar(self.db, NotificationEvent.event_type == "subscription.expiring"), 2)

    # ---------------------------------------------------------- la ventana horaria local (FR-030)

    def test_la_misma_hora_utc_cae_dentro_para_un_tenant_y_fuera_para_otro(self):
        bogota_db, tokio_db = fx.new_session(), fx.new_session()
        _tenant(bogota_db, UTC_DIA_BOGOTA + timedelta(hours=10), tz="America/Bogota")
        _tenant(tokio_db, UTC_DIA_BOGOTA + timedelta(hours=10), tz="Asia/Tokyo")

        for db, esperado in ((bogota_db, 1), (tokio_db, 0)):  # a las 15:00 UTC: 10:00 en Bogotá, 00:00 en Tokio
            with mock.patch.object(scheduler, "with_db", lambda _s, db=db: _cm(db)):
                scheduler.evaluate_subscription_alerts(UTC_DIA_BOGOTA)
            self.assertEqual(_contar(db), esperado)

    def test_a_la_hora_utc_de_tokio_ocurre_lo_contrario(self):
        bogota_db, tokio_db = fx.new_session(), fx.new_session()
        _tenant(bogota_db, UTC_DIA_TOKIO + timedelta(hours=10), tz="America/Bogota")
        _tenant(tokio_db, UTC_DIA_TOKIO + timedelta(hours=10), tz="Asia/Tokyo")

        for db, esperado in ((bogota_db, 0), (tokio_db, 1)):  # a las 03:00 UTC: 22:00 en Bogotá, 12:00 en Tokio
            with mock.patch.object(scheduler, "with_db", lambda _s, db=db: _cm(db)):
                scheduler.evaluate_subscription_alerts(UTC_DIA_TOKIO)
            self.assertEqual(_contar(db), esperado)

    def test_los_limites_de_la_ventana_son_inicio_inclusivo_y_fin_exclusivo(self):
        for hora_local_bogota, esperado in ((7, 0), (8, 1), (19, 1), (20, 0)):
            with self.subTest(hora=hora_local_bogota):
                db = fx.new_session()
                ahora = datetime(2026, 12, 6) + timedelta(hours=hora_local_bogota + 5, minutes=30)  # UTC = local + 5
                _tenant(db, ahora + timedelta(hours=10))
                with mock.patch.object(scheduler, "with_db", lambda _s, db=db: _cm(db)):
                    scheduler.evaluate_subscription_alerts(ahora)

                self.assertEqual(_contar(db), esperado)

    def test_con_la_ventana_0_a_24_emite_a_cualquier_hora(self):
        _ventana(self, 0, 24)
        _tenant(self.db, UTC_DIA_TOKIO + timedelta(hours=10))  # 22:00 en Bogotá

        self.assertEqual(self._evaluar(UTC_DIA_TOKIO), 1)

    def test_los_tramos_de_estado_no_cambian_con_la_zona_solo_cuando_se_emite(self):
        """FR-030: la zona decide CUÁNDO se emite el aviso, nunca el estado. Fuera de la ventana el tenant sigue en
        `por_vencer` (el aviso se emitirá en el siguiente tick dentro de la ventana)."""
        tenant = _tenant(self.db, UTC_DIA_TOKIO + timedelta(hours=10))  # 22:00 en Bogotá: fuera de ventana

        self.assertEqual(self._evaluar(UTC_DIA_TOKIO), 0)
        self.assertEqual(compute_subscription_status(tenant.plan_vence_en, UTC_DIA_TOKIO).state, "por_vencer")
        self.assertEqual(self._evaluar(UTC_DIA_TOKIO + timedelta(hours=12)), 1)  # 10:00 en Bogotá del día siguiente

    # ---------------------------------------------------------- robustez

    def test_un_tenant_que_falla_no_detiene_a_los_demas(self):
        malo = _tenant(self.db, UTC_DIA_BOGOTA + timedelta(hours=10))
        bueno = _tenant(self.db, UTC_DIA_BOGOTA + timedelta(hours=11))
        original = dispatch.notify_subscription_expiring

        def _explota_con_el_malo(db, tenant, status):
            if tenant.id == malo.id:
                raise RuntimeError("tenant roto")
            return original(db, tenant, status)

        with mock.patch.object(dispatch, "notify_subscription_expiring", _explota_con_el_malo):
            with self.assertLogs("app.core.scheduler", level="ERROR"):
                emitidos = self._evaluar()

        self.assertEqual(emitidos, 1)
        self.assertEqual({e.related_entity_id for e in _eventos(self.db)}, {dispatch._subscription_entity_id("subscription.expiring", bueno.plan_vence_en)})

    def test_el_job_no_levanta_aunque_todo_falle(self):
        _tenant(self.db, UTC_DIA_BOGOTA + timedelta(hours=10))

        with mock.patch.object(dispatch, "notify_subscription_expiring", side_effect=RuntimeError("x")):
            with self.assertLogs("app.core.scheduler", level="ERROR"):
                self.assertEqual(self._evaluar(), 0)

    def test_un_tenant_con_zona_invalida_cae_a_la_zona_por_defecto_en_vez_de_romper(self):
        tenant = _tenant(self.db, UTC_DIA_BOGOTA + timedelta(hours=10))
        with mock.patch.object(scheduler, "_alert_window_open", wraps=scheduler._alert_window_open):
            self.assertTrue(scheduler._alert_window_open(UTC_DIA_BOGOTA, "Zona/Inexistente"))  # Bogotá por defecto = 10:00
        self.assertIsNotNone(tenant)

    # ---------------------------------------------------------- pagos pendientes vencidos

    def test_marca_expired_los_pagos_pending_cuyo_link_ya_vencio(self):
        plan = fx.make_plan(self.db, precio_mensual=89900)
        tenant = _tenant(self.db, UTC_DIA_BOGOTA + timedelta(days=20), plan=plan)
        vencido = self._pago(tenant, plan, "pending", UTC_DIA_BOGOTA - timedelta(minutes=1))
        en_el_limite = self._pago(tenant, plan, "approved", UTC_DIA_BOGOTA - timedelta(days=3))
        vigente_tenant = _tenant(self.db, UTC_DIA_BOGOTA + timedelta(days=20), plan=plan)
        vigente = self._pago(vigente_tenant, plan, "pending", UTC_DIA_BOGOTA + timedelta(hours=1))

        self.assertEqual(scheduler.expire_stale_subscription_payments(UTC_DIA_BOGOTA), 1)
        self.db.expire_all()

        self.assertEqual(self.db.get(SubscriptionPayment, vencido.id).status, "expired")
        self.assertEqual(self.db.get(SubscriptionPayment, vigente.id).status, "pending")
        self.assertEqual(self.db.get(SubscriptionPayment, en_el_limite.id).status, "approved")  # solo toca los pending

    def test_un_link_que_vence_exactamente_ahora_ya_cuenta_como_vencido(self):
        plan = fx.make_plan(self.db, precio_mensual=89900)
        tenant = _tenant(self.db, UTC_DIA_BOGOTA + timedelta(days=20), plan=plan)
        pago = self._pago(tenant, plan, "pending", UTC_DIA_BOGOTA)  # coherente con `payment_link_expires_at > now` del servicio

        scheduler.expire_stale_subscription_payments(UTC_DIA_BOGOTA)
        self.db.expire_all()

        self.assertEqual(self.db.get(SubscriptionPayment, pago.id).status, "expired")

    def test_evaluar_los_avisos_tambien_expira_los_links_vencidos(self):
        plan = fx.make_plan(self.db, precio_mensual=89900)
        tenant = _tenant(self.db, UTC_DIA_BOGOTA + timedelta(hours=10), plan=plan)
        pago = self._pago(tenant, plan, "pending", UTC_DIA_BOGOTA - timedelta(hours=1))

        self._evaluar()
        self.db.expire_all()

        self.assertEqual(self.db.get(SubscriptionPayment, pago.id).status, "expired")

    def _pago(self, tenant, plan, status, expires_at):
        pid = uuid.uuid4()
        pago = SubscriptionPayment(
            id=pid, tenant_id=tenant.id, plan_id=plan.id, amount=89900, status=status,
            gateway_reference=f"SKP-{pid.hex}", payment_link_url="https://x", payment_link_expires_at=expires_at,
        )
        self.db.add(pago)
        self.db.commit()
        return pago


@contextmanager
def _cm(db):
    yield db


# ======================================================================
# El planificador
# ======================================================================


class RegistroDelJobTests(unittest.TestCase):
    def test_el_job_se_registra_con_su_intervalo_configurable_y_un_solo_hilo(self):
        class _Planificador:
            def __init__(self):
                self.jobs = {}

            def add_job(self, func, trigger, id=None, **kw):
                self.jobs[id] = (func, trigger, kw)

            def start(self):
                pass

        falso = _Planificador()
        with mock.patch("apscheduler.schedulers.asyncio.AsyncIOScheduler", return_value=falso):
            with mock.patch.multiple(settings, SUBSCRIPTION_ALERT_INTERVAL_MINUTES=45):
                scheduler.start_scheduler()

        func, trigger, kw = falso.jobs["subscription_alerts"]
        self.assertEqual(func, scheduler._run_subscription_alerts_with_lock)
        self.assertEqual(trigger.interval, timedelta(minutes=45))
        self.assertEqual(kw["max_instances"], 1)
        # Los jobs que ya existían siguen registrados.
        self.assertTrue({"sweep_orphan_table_sessions", "expire_promotions", "purge_notifications"} <= set(falso.jobs))


if __name__ == "__main__":
    unittest.main()
