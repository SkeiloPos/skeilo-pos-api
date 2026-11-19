"""Tests de la nueva funcionalidad — spec 161-suscripcion-pago-wompi, Historia de Usuario 1
(FR-001, FR-002, FR-003, SC-001): el estado de la suscripción de un tenant se deriva de
`plan_vence_en` con tramos EXACTOS en UTC (48 h antes del vencimiento y 120 h de gracia
después), y sin vencimiento (`NULL`) siempre está `activa`.

No son characterization tests — comportamiento enteramente nuevo. `compute_subscription_status`
es una función pura con `now` inyectable, así que cada frontera se prueba sin tocar el reloj.

    python -m unittest app.characterization_tests.test_subscription_state -v
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.characterization_tests import plan_fixtures  # noqa: F401  - variables de entorno inertes antes de importar app.*
from app.core.subscription import (
    ALERT_WINDOW,
    GRACE_PERIOD,
    SubscriptionStatus,
    compute_subscription_status,
)

#: Vencimiento de referencia (UTC naive, como `tenants.plan_vence_en`).
VENCE = datetime(2026, 12, 1, 5, 0, 0)
LIMITE = VENCE + timedelta(hours=120)


def _status(delta: timedelta) -> SubscriptionStatus:
    """Estado con el reloj a `VENCE + delta` (delta negativo = antes del vencimiento)."""
    return compute_subscription_status(VENCE, VENCE + delta)


class TestConstantes(unittest.TestCase):
    def test_los_tramos_son_48_y_120_horas_exactas(self):
        self.assertEqual(ALERT_WINDOW, timedelta(hours=48))
        self.assertEqual(GRACE_PERIOD, timedelta(hours=120))


class TestSinVencimiento(unittest.TestCase):
    """FR-003: `plan_vence_en` NULL = nunca vence."""

    def test_siempre_activa_sin_dias_ni_fecha_limite(self):
        for now in (VENCE - timedelta(days=400), VENCE, VENCE + timedelta(days=400)):
            with self.subTest(now=now):
                s = compute_subscription_status(None, now)
                self.assertEqual(s.state, "activa")
                self.assertIsNone(s.dias_restantes)
                self.assertIsNone(s.fecha_limite_pago)


class TestFronterasExactas(unittest.TestCase):
    """SC-001: cada frontera del cálculo, un segundo antes y en el instante exacto."""

    def test_mas_de_48_horas_antes_es_activa(self):
        s = _status(-(ALERT_WINDOW + timedelta(seconds=1)))
        self.assertEqual(s.state, "activa")

    def test_justo_a_48_horas_antes_es_por_vencer(self):
        self.assertEqual(_status(-ALERT_WINDOW).state, "por_vencer")

    def test_un_segundo_antes_del_vencimiento_sigue_por_vencer(self):
        self.assertEqual(_status(-timedelta(seconds=1)).state, "por_vencer")

    def test_en_el_instante_del_vencimiento_empieza_la_gracia(self):
        self.assertEqual(_status(timedelta(0)).state, "en_gracia")

    def test_a_119h_59m_59s_sigue_en_gracia(self):
        self.assertEqual(_status(GRACE_PERIOD - timedelta(seconds=1)).state, "en_gracia")

    def test_a_120_horas_exactas_queda_bloqueada(self):
        self.assertEqual(_status(GRACE_PERIOD).state, "bloqueada")

    def test_pasado_mucho_tiempo_sigue_bloqueada(self):
        self.assertEqual(_status(timedelta(days=45)).state, "bloqueada")


class TestFechaLimiteDePago(unittest.TestCase):
    """FR-002: `fecha_limite_pago` = vencimiento + 120 h, en todo estado con vencimiento."""

    def test_es_vencimiento_mas_120_horas_en_cada_tramo(self):
        for delta in (-timedelta(days=40), -ALERT_WINDOW, timedelta(0), GRACE_PERIOD - timedelta(seconds=1), GRACE_PERIOD):
            with self.subTest(delta=delta):
                self.assertEqual(_status(delta).fecha_limite_pago, LIMITE)


class TestDiasRestantes(unittest.TestCase):
    """FR-002: días enteros redondeados hacia arriba; hasta el vencimiento mientras no ha vencido,
    hasta la fecha límite durante la gracia, y 0 cuando está bloqueada."""

    def test_activa_cuenta_hasta_el_vencimiento(self):
        self.assertEqual(_status(-timedelta(days=40)).dias_restantes, 40)

    def test_activa_redondea_hacia_arriba(self):
        # 48 h + 1 s antes: faltan 2 días y 1 segundo -> 3
        self.assertEqual(_status(-(ALERT_WINDOW + timedelta(seconds=1))).dias_restantes, 3)

    def test_por_vencer_en_el_borde_de_48_horas_son_2_dias(self):
        self.assertEqual(_status(-ALERT_WINDOW).dias_restantes, 2)

    def test_por_vencer_a_un_segundo_del_vencimiento_es_1_dia(self):
        self.assertEqual(_status(-timedelta(seconds=1)).dias_restantes, 1)

    def test_en_gracia_cuenta_hasta_la_fecha_limite(self):
        self.assertEqual(_status(timedelta(0)).dias_restantes, 5)
        self.assertEqual(_status(timedelta(days=2)).dias_restantes, 3)
        self.assertEqual(_status(GRACE_PERIOD - timedelta(seconds=1)).dias_restantes, 1)

    def test_bloqueada_es_cero(self):
        self.assertEqual(_status(GRACE_PERIOD).dias_restantes, 0)
        self.assertEqual(_status(timedelta(days=45)).dias_restantes, 0)


class TestEntradas(unittest.TestCase):
    def test_acepta_datetimes_con_zona_y_los_normaliza_a_utc(self):
        # 05:00 UTC expresado en UTC-5 (00:00 local) debe dar el mismo resultado que el naive.
        bogota = timezone(timedelta(hours=-5))
        vence_aware = datetime(2026, 12, 1, 0, 0, 0, tzinfo=bogota)
        now_aware = datetime(2026, 12, 1, 0, 0, 0, tzinfo=bogota) + timedelta(hours=1)
        s = compute_subscription_status(vence_aware, now_aware)
        self.assertEqual(s.state, "en_gracia")
        self.assertEqual(s.fecha_limite_pago, LIMITE)

    def test_sin_now_usa_el_reloj_utc_naive_del_sistema(self):
        with mock.patch("app.core.subscription.utc_now_naive", return_value=VENCE + timedelta(hours=1)):
            self.assertEqual(compute_subscription_status(VENCE).state, "en_gracia")

    def test_el_resultado_es_inmutable(self):
        s = _status(timedelta(0))
        with self.assertRaises(Exception):
            s.state = "activa"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
