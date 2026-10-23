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
from datetime import datetime, timedelta, timezone
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
        order = fx.make_customer_order(db, ts, participant=participant, status="abierta")
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
            participant_id=None, channel="POS", status="abierta",
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
    """`scheduler.expire_promotions()` (spec 063/A-08): `ends_at` se guarda en
    hora local del tenant, así que el corte de expiración tiene que
    calcularse en esa misma zona horaria por tenant -- no contra un único
    `now` en UTC crudo. Caso real que motivó la corrección: a las 00:19 UTC
    (ya 23 de octubre en UTC) en Bogotá (UTC-5) todavía son las 19:19 del 22
    de octubre -- una promoción vigente hasta las 23:00 del 22 aún NO debería
    expirar en ese instante."""

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

    def test_no_expira_antes_de_tiempo_comparando_en_hora_local_del_tenant(self):
        db = cart_fx.new_session()
        make_shared_tenant(db, schema="tenant", timezone="America/Bogota")
        aun_vigente = cart_fx.make_promotion(
            db, status="active", ends_at=datetime(2026, 10, 22, 23, 0),
        )
        ya_vencida = cart_fx.make_promotion(
            db, status="active", ends_at=datetime(2026, 10, 22, 18, 0),
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
