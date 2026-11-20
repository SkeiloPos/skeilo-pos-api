"""`app.core.scheduler._sweep_schema` (spec 039, research.md Decisión 8):
primera cobertura `unittest` de esta función — antes solo existía el script
manual `app/scripts/test_table_release.py`, fuera de la suite automatizada.

`_sweep_schema(schema, corte, tenant_id=None)` no acepta ningún parámetro
`db`/`Session` — abre su propia sesión vía `with_db(schema)`. Se parchea
`app.core.scheduler.with_db` para que entregue la sesión SQLite en memoria de
este test, mismo patrón que ya usan `test_invitations_resend_cancel.py`,
`test_orders_payment_gate.py` y `fixtures.py` en este repo.

    python -m unittest app.characterization_tests.test_scheduler -v
"""
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch
import uuid

from app.characterization_tests import cart_fixtures as cart_fx
from app.characterization_tests import table_sessions_fixtures as fx
from app.characterization_tests.fixtures import make_shared_tenant
from app.core import scheduler
from app.models.cart import Cart
from app.models.customer_order import CustomerOrder

PRECIO = Decimal("10000")


class TestSweepSchema(unittest.TestCase):
    def _run_sweep(self, db, corte):
        @contextmanager
        def fake_with_db(schema):
            yield db

        with patch("app.core.scheduler.with_db", fake_with_db):
            return scheduler._sweep_schema("tenant", corte)

    # -------------------------------------------------------- US2 escenario 3

    def test_sweep_libera_mesa_vencida_sin_nada_por_cobrar_y_borra_carrito_huerfano(self):
        """US2 escenario 3 (spec 039, Acceptance Scenario 3): mesa vencida por
        inactividad, sin nada por cobrar, con un `Cart` huérfano de un
        comensal ya `closed` → el barrido libera la mesa y borra el `Cart`
        huérfano en la misma operación."""
        db = fx.new_session()
        table = fx.make_dining_table(db, status="ocupada")
        corte = datetime.now(timezone.utc).replace(tzinfo=None)
        ts = fx.make_table_session(db, table=table, opened_at=corte - timedelta(hours=7))
        huerfano = fx.make_participant(db, table_session=ts, status="closed")
        cart = fx.make_cart(db, participant=huerfano, status="abandonado")
        cart_id = cart.id
        db.commit()

        tocadas = self._run_sweep(db, corte)

        self.assertEqual(tocadas, 1)
        db.refresh(table)
        db.refresh(ts)
        self.assertEqual(table.status, "libre")
        self.assertEqual(ts.status, "closed")
        self.assertIsNone(db.get(Cart, cart_id))

    # -------------------------------------------------------- US3 escenario 1

    def test_sweep_no_libera_ni_borra_con_pedido_facturable_pendiente(self):
        """US3 escenario 1 (spec 039, Acceptance Scenario 1, RN-SCHED-03):
        sesión vencida con un pedido `'abierta'` sin cobrar y un `Cart`
        `'abierto'` de un comensal todavía activo → el barrido solo cierra a
        los comensales (`close_participants`; `close_table_sessions` no
        corre para esa sesión), la mesa sigue `'ocupada'`, y el `Cart`
        huérfano sigue existiendo (pasa a `'abandonado'`, mismo
        comportamiento de siempre — no se borra)."""
        db = fx.new_session()
        table = fx.make_dining_table(db, status="ocupada")
        corte = datetime.now(timezone.utc).replace(tzinfo=None)
        ts = fx.make_table_session(db, table=table, opened_at=corte - timedelta(hours=7))
        participant = fx.make_participant(db, table_session=ts, status="open")
        cart = fx.make_cart(db, participant=participant, status="abierto")
        cart_id = cart.id
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        order = fx.make_customer_order(db, ts, participant=participant, status="pendiente")
        fx.make_order_item(db, order, variant)
        db.commit()

        tocadas = self._run_sweep(db, corte)

        self.assertEqual(tocadas, 1)
        db.refresh(table)
        db.refresh(ts)
        db.refresh(participant)
        self.assertEqual(table.status, "ocupada")
        self.assertEqual(ts.status, "active")
        self.assertEqual(participant.status, "closed")
        cart_after = db.get(Cart, cart_id)
        self.assertIsNotNone(cart_after)
        self.assertEqual(cart_after.status, "abandonado")

    # -------------------------------------------------------- US3 escenario 2

    def test_sweep_cierra_sesion_pero_no_libera_con_pedido_huerfano_de_la_mesa(self):
        """US3 escenario 2 (spec 039, Acceptance Scenario 2, RN-SCHED-04):
        sesión vencida sin nada por cobrar, pero con un `CustomerOrder` no
        terminal huérfano de la misma mesa física (sin `table_session_id`)
        → el barrido cierra la sesión (`close_table_sessions` sí corre)
        pero la mesa **no** vuelve a `'libre'` — `delete_orphan_carts` no se
        invoca (queda dentro del `if quedo_libre:` que no se cumple) y el
        `Cart` de esa sesión sigue existiendo."""
        db = fx.new_session()
        table = fx.make_dining_table(db, status="ocupada")
        corte = datetime.now(timezone.utc).replace(tzinfo=None)
        ts = fx.make_table_session(db, table=table, opened_at=corte - timedelta(hours=7))
        huerfano = fx.make_participant(db, table_session=ts, status="closed")
        cart = fx.make_cart(db, participant=huerfano, status="abandonado")
        cart_id = cart.id

        pedido_huerfano = CustomerOrder(
            id=uuid.uuid4(), dining_table_id=table.id, table_session_id=None,
            participant_id=None, channel="POS", status="pendiente",
            created_at=datetime.now(),
        )
        db.add(pedido_huerfano)
        db.commit()

        tocadas = self._run_sweep(db, corte)

        self.assertEqual(tocadas, 1)
        db.refresh(table)
        db.refresh(ts)
        self.assertEqual(ts.status, "closed")
        self.assertEqual(table.status, "ocupada")
        self.assertIsNotNone(db.get(Cart, cart_id))


class TestExpirePromotions(unittest.TestCase):
    """`scheduler.expire_promotions()` (spec 063/A-08, y corrección posterior):
    `ends_at` se guarda en hora local del tenant (medianoche naive -- el
    frontend solo manda fecha, sin hora), así que:

    1. El corte de expiración se calcula en la zona horaria local de cada
       tenant -- no contra un único `now` en UTC crudo (caso real: a las
       00:19 UTC, en Bogotá -UTC-5- todavía es el día anterior).
    2. El corte real NO es solo la fecha de `ends_at`: también depende de
       `days_of_week` y de la ventana horaria `start_time`/`end_time`, igual
       que `_valid_now` (`promotion_expiry_cutoff`, en
       app/api/v1/promotions/service.py). Bug real que motivó esto: una
       promoción "lunes a jueves" con `ends_at` = un viernes (a medianoche)
       se marcaba `finished` apenas empezaba ese viernes a las 00:00, en vez
       de haber terminado el jueves en la noche (su último día real) o, sin
       `days_of_week`, seguir vigente todo el día de `ends_at`."""

    TZ_BOGOTA = "America/Bogota"  # UTC-5 fijo, sin horario de verano

    def _run_expire(self, db):
        @contextmanager
        def fake_with_db(schema):
            yield db

        with patch("app.core.scheduler.with_db", fake_with_db):
            return scheduler.expire_promotions()

    def _fake_now_utc(self, instante_utc):
        class FakeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return instante_utc if tz is None else instante_utc.astimezone(tz)

        return patch("app.core.scheduler.datetime", FakeDateTime)

    def _correr_a_la_hora_bogota(self, db, local_naive: datetime) -> int:
        """`local_naive` es la hora "de pared" en Bogotá (UTC-5) en la que se
        quiere que corra el job."""
        instante_utc = (local_naive + timedelta(hours=5)).replace(tzinfo=timezone.utc)
        with self._fake_now_utc(instante_utc):
            return self._run_expire(db)

    def test_no_expira_antes_de_tiempo_comparando_en_hora_local_del_tenant(self):
        """A-128: "hoy" se calcula en la zona horaria del tenant, no en UTC.
        A las 00:19 UTC del 23 de octubre todavía son las 19:19 del 22 en
        Bogotá -- una promoción vigente *hasta* el 22 no debe expirar, y una
        vigente solo *hasta* el 21 sí."""
        db = cart_fx.new_session()
        make_shared_tenant(db, schema="tenant", timezone=self.TZ_BOGOTA)
        aun_vigente = cart_fx.make_promotion(
            db, status="active", ends_at=datetime(2026, 10, 22, 0, 0),
        )
        ya_vencida = cart_fx.make_promotion(
            db, status="active", ends_at=datetime(2026, 10, 21, 0, 0),
        )
        db.commit()

        instante_utc = datetime(2026, 10, 23, 0, 19, tzinfo=timezone.utc)
        with self._fake_now_utc(instante_utc):
            total = self._run_expire(db)

        self.assertEqual(total, 1)
        db.refresh(aun_vigente)
        db.refresh(ya_vencida)
        self.assertEqual(aun_vigente.status, "active")
        self.assertEqual(ya_vencida.status, "finished")

    def test_ends_at_es_inclusivo_hasta_el_final_del_dia(self):
        """Caso original del bug: `ends_at` = hoy a medianoche (como lo manda
        el frontend, que solo pide fecha) debe seguir vigente TODO ese día,
        no expirar apenas empieza."""
        db = cart_fx.new_session()
        make_shared_tenant(db, schema="tenant", timezone=self.TZ_BOGOTA)
        promo = cart_fx.make_promotion(
            db, status="active", ends_at=datetime(2026, 11, 20, 0, 0),
        )
        db.commit()

        total = self._correr_a_la_hora_bogota(db, datetime(2026, 11, 20, 9, 4))
        self.assertEqual(total, 0)
        db.refresh(promo)
        self.assertEqual(promo.status, "active")

        total = self._correr_a_la_hora_bogota(db, datetime(2026, 11, 21, 0, 1))
        self.assertEqual(total, 1)
        db.refresh(promo)
        self.assertEqual(promo.status, "finished")

    def test_promocion_lunes_a_jueves_expira_el_jueves_en_la_noche_no_el_viernes(self):
        """Caso real reportado en producción: "Promociones de lunes a
        jueves" (`days_of_week="0,1,2,3"`) con `ends_at` = viernes 2026-11-20
        -- debe expirar el jueves 19 en la noche (su último día real), no
        esperar hasta el viernes."""
        db = cart_fx.new_session()
        make_shared_tenant(db, schema="tenant", timezone=self.TZ_BOGOTA)
        promo = cart_fx.make_promotion(
            db, status="active", ends_at=datetime(2026, 11, 20, 0, 0),
            days_of_week="0,1,2,3",
        )
        db.commit()

        # Jueves 19 a las 20:00 -- todavía dentro de su último día real.
        total = self._correr_a_la_hora_bogota(db, datetime(2026, 11, 19, 20, 0))
        self.assertEqual(total, 0)
        db.refresh(promo)
        self.assertEqual(promo.status, "active")

        # Viernes 20 a las 00:01 -- ya pasó su último día (jueves).
        total = self._correr_a_la_hora_bogota(db, datetime(2026, 11, 20, 0, 1))
        self.assertEqual(total, 1)
        db.refresh(promo)
        self.assertEqual(promo.status, "finished")

    def test_ventana_horaria_del_mismo_dia_expira_al_terminar_la_ventana(self):
        """`start_time`/`end_time` dentro del mismo día: con `ends_at` = hoy
        y ventana 08:00-12:00, debe expirar al pasar las 12:00 de ese día,
        no esperar a medianoche."""
        db = cart_fx.new_session()
        make_shared_tenant(db, schema="tenant", timezone=self.TZ_BOGOTA)
        promo = cart_fx.make_promotion(
            db, status="active", ends_at=datetime(2026, 11, 20, 0, 0),
            start_time=time(8, 0), end_time=time(12, 0),
        )
        db.commit()

        total = self._correr_a_la_hora_bogota(db, datetime(2026, 11, 20, 11, 59))
        self.assertEqual(total, 0)
        db.refresh(promo)
        self.assertEqual(promo.status, "active")

        total = self._correr_a_la_hora_bogota(db, datetime(2026, 11, 20, 12, 1))
        self.assertEqual(total, 1)
        db.refresh(promo)
        self.assertEqual(promo.status, "finished")

    def test_ventana_horaria_que_cruza_medianoche_expira_al_dia_siguiente(self):
        """Caso borde (no usado hoy en producción, pero cubierto para no
        reintroducir el mismo tipo de bug si se usa a futuro): ventana
        22:00-02:00 con `ends_at` = día D debe seguir vigente hasta las 02:00
        del día D+1."""
        db = cart_fx.new_session()
        make_shared_tenant(db, schema="tenant", timezone=self.TZ_BOGOTA)
        promo = cart_fx.make_promotion(
            db, status="active", ends_at=datetime(2026, 11, 20, 0, 0),
            start_time=time(22, 0), end_time=time(2, 0),
        )
        db.commit()

        total = self._correr_a_la_hora_bogota(db, datetime(2026, 11, 21, 1, 59))
        self.assertEqual(total, 0)
        db.refresh(promo)
        self.assertEqual(promo.status, "active")

        total = self._correr_a_la_hora_bogota(db, datetime(2026, 11, 21, 2, 1))
        self.assertEqual(total, 1)
        db.refresh(promo)
        self.assertEqual(promo.status, "finished")

    def test_respalda_en_tenant_timezone_global_si_el_schema_no_tiene_fila_en_shared_tenants(self):
        """Si `shared.tenants` no devuelve ninguna fila para el schema (dato
        inconsistente, no debería pasar tras la migración de A-46), el schema
        simplemente no corre -- no hay from onde sacar el schema a iterar."""
        db = cart_fx.new_session()
        promo = cart_fx.make_promotion(
            db, status="active", ends_at=datetime(2026, 10, 22, 18, 0),
        )
        db.commit()

        instante_utc = datetime(2026, 10, 23, 0, 19, tzinfo=timezone.utc)
        with self._fake_now_utc(instante_utc):
            total = self._run_expire(db)

        self.assertEqual(total, 0)
        db.refresh(promo)
        self.assertEqual(promo.status, "active")


if __name__ == "__main__":
    unittest.main()
