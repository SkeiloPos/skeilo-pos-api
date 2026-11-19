"""Tests de la nueva funcionalidad — spec 161-suscripcion-pago-wompi, Historia de Usuario 7 (FR-038, SC-011,
D8; `research.md` Decisión 13; `data-model.md § 5`): el procedimiento de UNA SOLA VEZ que da a los tenants ya
vencidos la gracia completa de 5 días contada desde el despliegue, para que el guard global no los bloquee de
golpe al activarse.

    python -m app.scripts.grant_subscription_grace --cutoff <ISO-UTC> [--apply]

No son characterization tests — script enteramente nuevo.

    python -m unittest app.characterization_tests.test_subscription_grace_script -v
"""
from __future__ import annotations

import io
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta
from unittest import mock

from app.characterization_tests import plan_fixtures as fx
from app.core.subscription import compute_subscription_status
from app.core.timezone import utc_now_naive
from app.scripts import grant_subscription_grace as script

CUTOFF = datetime(2026, 12, 1, 15, 0, 0)


def _tenant(db, vence, *, ciclo="mensual", plan=None):
    plan = plan or fx.make_plan(db)
    tenant = fx.make_tenant(db, plan=plan, ciclo_facturacion=ciclo if vence is not None else None)
    if vence is not None:
        tenant.plan_iniciado_en = vence - timedelta(days=30)
        tenant.plan_vence_en = vence
    db.commit()
    return tenant


def _estado(db, tenant, instante=CUTOFF + timedelta(minutes=1)):
    db.expire_all()
    return compute_subscription_status(db.get(type(tenant), tenant.id).plan_vence_en, instante).state


class ConGraciaTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.hace_40_dias = _tenant(self.db, CUTOFF - timedelta(days=40))
        self.hace_3_horas = _tenant(self.db, CUTOFF - timedelta(hours=3))
        self.vigente = _tenant(self.db, CUTOFF + timedelta(days=12))
        self.sin_vencimiento = _tenant(self.db, None)

    # ---------------------------------------------------------- sin --apply solo informa

    def test_sin_apply_informa_pero_no_escribe_nada(self):
        antes = {t.id: (t.plan_iniciado_en, t.plan_vence_en) for t in (self.hace_40_dias, self.hace_3_horas, self.vigente)}

        afectados = script.grant_grace(self.db, CUTOFF, apply=False)
        self.db.expire_all()

        self.assertEqual({a.tenant_id for a in afectados}, {self.hace_40_dias.id, self.hace_3_horas.id})
        for t in (self.hace_40_dias, self.hace_3_horas, self.vigente):
            actual = self.db.get(type(t), t.id)
            self.assertEqual((actual.plan_iniciado_en, actual.plan_vence_en), antes[t.id])

    # ---------------------------------------------------------- con --apply

    def test_apply_mueve_el_vencimiento_de_los_ya_vencidos_al_corte(self):
        script.grant_grace(self.db, CUTOFF, apply=True)
        self.db.expire_all()

        self.assertEqual(self.db.get(type(self.hace_40_dias), self.hace_40_dias.id).plan_vence_en, CUTOFF)
        self.assertEqual(self.db.get(type(self.hace_3_horas), self.hace_3_horas.id).plan_vence_en, CUTOFF)

    def test_los_ya_vencidos_quedan_en_gracia_con_5_dias_completos(self):
        script.grant_grace(self.db, CUTOFF, apply=True)

        for t in (self.hace_40_dias, self.hace_3_horas):
            with self.subTest(t.id):
                self.db.expire_all()
                tenant = self.db.get(type(t), t.id)
                estado = compute_subscription_status(tenant.plan_vence_en, CUTOFF + timedelta(minutes=1))
                self.assertEqual(estado.state, "en_gracia")
                self.assertEqual(estado.dias_restantes, 5)
                self.assertEqual(estado.fecha_limite_pago, CUTOFF + timedelta(hours=120))

    def test_ninguno_queda_bloqueado_en_el_arranque_sc011(self):
        script.grant_grace(self.db, CUTOFF, apply=True)

        for t in (self.hace_40_dias, self.hace_3_horas, self.vigente, self.sin_vencimiento):
            with self.subTest(t.id):
                self.assertNotEqual(_estado(self.db, t), "bloqueada")

    def test_el_bloqueo_llega_exactamente_120_horas_despues_del_corte(self):
        script.grant_grace(self.db, CUTOFF, apply=True)

        self.assertEqual(_estado(self.db, self.hace_40_dias, CUTOFF + timedelta(hours=120) - timedelta(seconds=1)), "en_gracia")
        self.assertEqual(_estado(self.db, self.hace_40_dias, CUTOFF + timedelta(hours=120)), "bloqueada")

    def test_no_toca_a_los_vigentes_ni_a_los_que_nunca_vencen(self):
        vigente_antes = self.vigente.plan_vence_en

        script.grant_grace(self.db, CUTOFF, apply=True)
        self.db.expire_all()

        self.assertEqual(self.db.get(type(self.vigente), self.vigente.id).plan_vence_en, vigente_antes)
        self.assertIsNone(self.db.get(type(self.sin_vencimiento), self.sin_vencimiento.id).plan_vence_en)

    def test_no_cambia_plan_iniciado_en_ciclo_ni_plan(self):
        antes = (self.hace_40_dias.plan_iniciado_en, self.hace_40_dias.ciclo_facturacion, self.hace_40_dias.plan_id)

        script.grant_grace(self.db, CUTOFF, apply=True)
        self.db.expire_all()
        t = self.db.get(type(self.hace_40_dias), self.hace_40_dias.id)

        self.assertEqual((t.plan_iniciado_en, t.ciclo_facturacion, t.plan_id), antes)

    def test_un_tenant_que_vence_justo_en_el_corte_no_se_toca(self):
        justo = _tenant(self.db, CUTOFF)

        afectados = script.grant_grace(self.db, CUTOFF, apply=True)

        self.assertNotIn(justo.id, {a.tenant_id for a in afectados})

    def test_un_ciclo_anual_ya_vencido_tambien_recibe_la_gracia(self):
        anual = _tenant(self.db, CUTOFF - timedelta(days=3), ciclo="anual")

        script.grant_grace(self.db, CUTOFF, apply=True)

        self.assertEqual(_estado(self.db, anual), "en_gracia")

    # ---------------------------------------------------------- idempotencia

    def test_repetirlo_con_el_mismo_corte_no_modifica_nada(self):
        primera = script.grant_grace(self.db, CUTOFF, apply=True)
        segunda = script.grant_grace(self.db, CUTOFF, apply=True)

        self.assertEqual(len(primera), 2)
        self.assertEqual(segunda, [])

    def test_repetirlo_con_el_mismo_corte_deja_las_fechas_exactamente_igual(self):
        script.grant_grace(self.db, CUTOFF, apply=True)
        self.db.expire_all()
        despues_1 = {t.id: self.db.get(type(t), t.id).plan_vence_en for t in (self.hace_40_dias, self.hace_3_horas)}

        script.grant_grace(self.db, CUTOFF, apply=True)
        self.db.expire_all()
        despues_2 = {t.id: self.db.get(type(t), t.id).plan_vence_en for t in (self.hace_40_dias, self.hace_3_horas)}

        self.assertEqual(despues_1, despues_2)

    def test_un_corte_posterior_si_vuelve_a_actuar_sobre_los_que_vencieron_despues(self):
        """Por eso el corte es obligatorio y explícito: no hay un «ahora» implícito que lo vuelva repetible."""
        script.grant_grace(self.db, CUTOFF, apply=True)
        otro_corte = CUTOFF + timedelta(days=30)

        afectados = script.grant_grace(self.db, otro_corte, apply=False)

        self.assertTrue(len(afectados) >= 3)  # los dos de antes ya vencieron respecto al corte nuevo, y el vigente también

    # ---------------------------------------------------------- el informe

    def test_informa_por_tenant_el_valor_anterior_y_el_nuevo(self):
        afectados = {a.tenant_id: a for a in script.grant_grace(self.db, CUTOFF, apply=False)}

        a = afectados[self.hace_40_dias.id]
        self.assertEqual(a.previous, CUTOFF - timedelta(days=40))
        self.assertEqual(a.new, CUTOFF)
        self.assertEqual(a.name, self.hace_40_dias.name)

    def test_el_informe_va_ordenado_por_id(self):
        ids = [a.tenant_id for a in script.grant_grace(self.db, CUTOFF, apply=False)]

        self.assertEqual(ids, sorted(ids))

    def test_sin_nadie_que_atender_el_informe_esta_vacio(self):
        self.assertEqual(script.grant_grace(self.db, CUTOFF - timedelta(days=100), apply=True), [])


class ParseCutoffTests(unittest.TestCase):
    def test_formatos_aceptados(self):
        esperado = datetime(2026, 12, 1, 15, 0, 0)
        for valor in ("2026-12-01T15:00:00", "2026-12-01T15:00:00Z", "2026-12-01T15:00:00+00:00", "2026-12-01 15:00:00"):
            with self.subTest(valor):
                self.assertEqual(script.parse_cutoff(valor), esperado)

    def test_un_valor_con_zona_se_lleva_a_utc_naive(self):
        self.assertEqual(script.parse_cutoff("2026-12-01T10:00:00-05:00"), datetime(2026, 12, 1, 15, 0, 0))

    def test_el_resultado_es_naive_como_las_columnas_del_esquema_compartido(self):
        self.assertIsNone(script.parse_cutoff("2026-12-01T15:00:00Z").tzinfo)

    def test_un_valor_invalido_se_rechaza(self):
        for valor in ("ayer", "", "2026-13-45T00:00:00", "12/01/2026"):
            with self.subTest(valor):
                with self.assertRaises(ValueError):
                    script.parse_cutoff(valor)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.vencido = _tenant(self.db, utc_now_naive() - timedelta(days=40))

        @contextmanager
        def _con_db(_schema):
            yield self.db

        patcher = mock.patch.object(script, "with_db", _con_db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _ejecutar(self, *argv):
        salida = io.StringIO()
        with redirect_stdout(salida):
            codigo = script.main(list(argv))
        return codigo, salida.getvalue()

    def test_el_corte_es_obligatorio(self):
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                script.main([])

        self.assertEqual(ctx.exception.code, 2)

    def test_sin_apply_es_una_simulacion_que_lo_dice_y_no_escribe(self):
        corte = utc_now_naive().replace(microsecond=0)
        antes = self.vencido.plan_vence_en

        codigo, salida = self._ejecutar("--cutoff", corte.isoformat())
        self.db.expire_all()

        self.assertEqual(codigo, 0)
        self.assertIn("SIMULACIÓN", salida)
        self.assertIn(self.vencido.name, salida)
        self.assertEqual(self.db.get(type(self.vencido), self.vencido.id).plan_vence_en, antes)

    def test_con_apply_escribe_e_imprime_valor_anterior_y_nuevo(self):
        corte = utc_now_naive().replace(microsecond=0)
        anterior = self.vencido.plan_vence_en

        codigo, salida = self._ejecutar("--cutoff", corte.isoformat(), "--apply")
        self.db.expire_all()

        self.assertEqual(codigo, 0)
        self.assertNotIn("SIMULACIÓN", salida)
        self.assertIn(anterior.isoformat(), salida)
        self.assertIn(corte.isoformat(), salida)
        self.assertEqual(self.db.get(type(self.vencido), self.vencido.id).plan_vence_en, corte)

    def test_apply_rechaza_un_corte_en_el_futuro_para_no_extender_suscripciones_sin_querer(self):
        futuro = utc_now_naive() + timedelta(days=3)
        antes = self.vencido.plan_vence_en

        codigo, salida = self._ejecutar("--cutoff", futuro.isoformat(), "--apply")
        self.db.expire_all()

        self.assertEqual(codigo, 2)
        self.assertIn("futuro", salida.lower())
        self.assertEqual(self.db.get(type(self.vencido), self.vencido.id).plan_vence_en, antes)

    def test_la_simulacion_si_acepta_un_corte_en_el_futuro(self):
        codigo, _ = self._ejecutar("--cutoff", (utc_now_naive() + timedelta(days=3)).isoformat())

        self.assertEqual(codigo, 0)

    def test_un_corte_ilegible_termina_con_codigo_2_y_sin_escribir(self):
        codigo, salida = self._ejecutar("--cutoff", "mañana", "--apply")

        self.assertEqual(codigo, 2)
        self.assertIn("cutoff", salida.lower())

    def test_sin_tenants_vencidos_lo_dice_claro(self):
        corte = utc_now_naive() - timedelta(days=100)

        codigo, salida = self._ejecutar("--cutoff", corte.isoformat(), "--apply")

        self.assertEqual(codigo, 0)
        self.assertIn("nada que hacer", salida.lower())


if __name__ == "__main__":
    unittest.main()
