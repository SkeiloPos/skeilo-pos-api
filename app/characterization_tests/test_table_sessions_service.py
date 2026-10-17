"""Characterization tests de las 9 funciones públicas de
`app/api/v1/table_sessions/service.py` (specs/016-caracterizacion-table-sessions,
Historia 2).

Cada test CONGELA el comportamiento actual, ejercitando `orders.checkout` y
`promotions.service` reales (sin mocks, FR-009) contra SQLite en memoria vía
`table_sessions_fixtures.py`. Incluye los cuatro casos de anomalía que esta
Historia cubre (FR-004, FR-006, FR-007, FR-008), citados explícitamente:

  - A-01 (camino A, correcto): `test_compute_bill_a01_...`
  - A-17 (R12): `test_add_remove_set_assignments_a17_r12_...`
  - A-29: `test_close_session_unified_a29_...`
  - A-38 (RN-MESA-13): `test_close_session_split_rn_mesa_13_a38_...`
  - A-38 (RN-MESA-24): `test_remove_participant_rn_mesa_24_a38_...`

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_table_sessions_service -v
"""
from datetime import datetime, time, timezone
from decimal import Decimal
import unittest
from unittest import mock
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, select

from app.characterization_tests import table_sessions_fixtures as fx
from app.api.v1.table_sessions import service
from app.api.v1.table_sessions.schemas import CloseSessionIn, ItemAssignmentIn
from app.api.v1.sales.schemas import PaymentIn
from app.models.cart import Cart
from app.models.dining_table import DiningTable
from app.models.order_item import OrderItem, OrderItemOption
from app.models.sale import Sale
from app.models.session_participant import SessionParticipant
from app.models.table_session import TableSession

PRECIO = Decimal("10000")


class TestTableSessionsService(unittest.TestCase):
    # ------------------------------------------------------------- Helpers

    def _seed_bare_session(self, *, table_status: str = "ocupada"):
        """Mesa + `TableSession` activa, sin comensales ni pedidos aún."""
        db = fx.new_session()
        table = fx.make_dining_table(db, status=table_status)
        ts = fx.make_table_session(db, table=table)
        db.commit()
        return db, table, ts

    def _seed_billable_session(self):
        """Mesa + sesión activa + dos comensales (Ana, Beto) + infraestructura de
        caja + un pedido `abierta` con una línea de Ana, lista para cobrar."""
        db, table, ts = self._seed_bare_session()
        ana = fx.make_participant(db, table_session=ts, display_name="Ana", display_label="Ana")
        beto = fx.make_participant(db, table_session=ts, display_name="Beto", display_label="Beto")

        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)

        order = fx.make_customer_order(db, ts, status="abierta")
        item = fx.make_order_item(db, order, variant, participant_id=ana.id)

        register = fx.make_cash_register(db)
        shift = fx.make_cash_shift(db, register=register)
        method = fx.make_payment_method(db)
        cashier = fx.make_user_double()

        db.commit()
        return dict(
            db=db, table=table, ts=ts, ana=ana, beto=beto, category=category,
            product=product, variant=variant, order=order, item=item,
            shift=shift, method=method, cashier=cashier,
        )

    def _pago(self, method_id, amount=PRECIO):
        return PaymentIn(payment_method_id=method_id, amount=amount).model_dump(mode="json")

    def _ventas(self, db, ts_id) -> list[Sale]:
        db.flush()
        return list(db.execute(select(Sale).where(Sale.table_session_id == ts_id)).scalars())

    # ---------------------------------------------------------- get_session (T017)

    def test_get_session_existente_y_404(self):
        """CONGELA comportamiento actual: sesión existente devuelve el
        `TableSession` esperado (`service.py:38-59`); un id inexistente propaga
        `HTTPException` 404."""
        db, table, ts = self._seed_bare_session()

        got = service.get_session(db, ts.id)
        self.assertEqual(got.id, ts.id)
        self.assertEqual(got.dining_table_id, table.id)

        with self.assertRaises(HTTPException) as ctx:
            service.get_session(db, uuid4())
        self.assertEqual(ctx.exception.status_code, 404)

    # ------------------------------------------------- has_billable_orders (T018)

    def test_has_billable_orders_true_y_false(self):
        """CONGELA comportamiento actual (`service.py:64-71`): una sesión con un
        pedido `recibida`/`abierta` (no terminal) devuelve `True`; con solo
        pedidos `cancelada`/`pagada` devuelve `False`."""
        db, table, ts = self._seed_bare_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)

        order_abierta = fx.make_customer_order(db, ts, status="abierta")
        fx.make_order_item(db, order_abierta, variant)
        db.commit()
        self.assertTrue(service.has_billable_orders(db, ts.id))

        order_abierta.status = "pagada"
        db.commit()
        order_cancelada = fx.make_customer_order(db, ts, status="cancelada")
        fx.make_order_item(db, order_cancelada, variant)
        db.commit()
        self.assertFalse(service.has_billable_orders(db, ts.id))

    # ------------------------------------------- has_pending_balance (spec 123)

    def _seed_pending_balance(self, db, ts, variant, *, cubierta=False):
        """Pedido ya facturado (tiene `Sale`) con una línea que tiene una
        adición agregada después de facturar -- `added_after_sale=True`.
        `cubierta=True` simula que ya se cobró el pago complementario
        (`complementary_sale_id` fijado), sin pasar por ese endpoint."""
        order = fx.make_customer_order(db, ts, status="abierta")
        item = fx.make_order_item(db, order, variant, estado_cocina="pendiente")
        group = fx.make_option_group(db)
        option = fx.make_option(db, group=group, extra_price=Decimal("2000"))
        register = fx.make_cash_register(db)
        shift = fx.make_cash_shift(db, register=register)
        sale = Sale(
            customer_order_id=order.id, cash_shift_id=shift.id, user_id=uuid4(),
            total=Decimal("22000"), status="paid",
        )
        db.add(sale)
        db.flush()
        complementaria = None
        if cubierta:
            complementaria = Sale(
                cash_shift_id=shift.id, user_id=uuid4(), customer_order_id=order.id,
                total=Decimal("2000"), status="paid", kind="complementario",
            )
            db.add(complementaria)
            db.flush()
        db.add(OrderItemOption(
            order_item_id=item.id, option_id=option.id, quantity=1,
            added_after_sale=True,
            complementary_sale_id=complementaria.id if complementaria else None,
        ))
        db.commit()
        return order

    def test_has_pending_balance_true_y_false(self):
        db, table, ts = self._seed_bare_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)

        self.assertFalse(service.has_pending_balance(db, ts.id))

        self._seed_pending_balance(db, ts, variant)
        self.assertTrue(service.has_pending_balance(db, ts.id))

    def test_release_paid_session_409_con_saldo_pendiente_por_adicion(self):
        db, table, ts = self._seed_bare_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        order = self._seed_pending_balance(db, ts, variant)
        for it in order.items:
            it.estado_cocina = "listo"
        db.commit()
        cashier = fx.make_user_double()

        with self.assertRaises(HTTPException) as ctx:
            service.release_paid_session(db, ts.id, cashier)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail, "Hay un saldo pendiente por cobrar")
        db.refresh(ts)
        self.assertEqual(ts.status, "active")

    def test_release_paid_session_libera_una_vez_el_saldo_pendiente_esta_cubierto(self):
        db, table, ts = self._seed_bare_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        order = self._seed_pending_balance(db, ts, variant, cubierta=True)
        for it in order.items:
            it.estado_cocina = "listo"
        db.commit()
        cashier = fx.make_user_double()

        resp = service.release_paid_session(db, ts.id, cashier)
        self.assertEqual(resp.status, "libre")

    # ----------------------------------------------- try_release_if_empty (T019)

    def test_try_release_if_empty_libera_y_no_libera(self):
        """CONGELA comportamiento actual (spec.md Historia 2, escenario 6): sin
        comensales activos y sin pedidos cobrables, libera la mesa y cierra la
        sesión; con un pedido todavía cobrable, no libera nada."""
        db, table, ts = self._seed_bare_session()
        participant = fx.make_participant(db, table_session=ts, status="closed")
        db.commit()

        released = service.try_release_if_empty(db, ts.id)
        self.assertTrue(released)
        db.refresh(ts)
        db.refresh(table)
        self.assertEqual(ts.status, "closed")
        self.assertEqual(table.status, "libre")

        # Segunda sesión: queda un pedido cobrable, no se libera.
        table2 = fx.make_dining_table(db, status="ocupada")
        ts2 = fx.make_table_session(db, table=table2)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        order = fx.make_customer_order(db, ts2, status="abierta")
        fx.make_order_item(db, order, variant)
        db.commit()

        released2 = service.try_release_if_empty(db, ts2.id)
        self.assertFalse(released2)
        db.refresh(ts2)
        self.assertEqual(ts2.status, "active")

    def test_try_release_if_empty_borra_carritos_sin_importar_status(self):
        """US1 escenario 2 (spec 039, Acceptance Scenario 2): dos comensales de
        la misma sesión, uno con carrito ya 'abandonado' (salió antes) y otro
        con carrito 'confirmado' (el mesero lo consolidó) — al salir el
        segundo siendo el último activo sin nada por cobrar, se libera la
        mesa y ninguno de los dos Cart sigue existiendo, sin importar su
        status."""
        db, table, ts = self._seed_bare_session()
        p1 = fx.make_participant(db, table_session=ts, status="closed")
        cart1 = fx.make_cart(db, participant=p1, status="abandonado")
        cart1_id = cart1.id
        p2 = fx.make_participant(db, table_session=ts, status="closed")
        cart2 = fx.make_cart(db, participant=p2, status="confirmado")
        cart2_id = cart2.id
        db.commit()

        released = service.try_release_if_empty(db, ts.id)

        self.assertTrue(released)
        db.refresh(table)
        self.assertEqual(table.status, "libre")
        self.assertIsNone(db.get(Cart, cart1_id))
        self.assertIsNone(db.get(Cart, cart2_id))

    def test_try_release_if_empty_libera_tras_cancelar_ultimo_pedido_activo(self):
        """US1 escenario 3 (spec 039, Acceptance Scenario 3): comensal con un
        Cart 'abierto' y un CustomerOrder vivo; al cancelarse ese último
        pedido activo (has_billable_orders pasa a False, igual que hace
        cart.service.cancel_my_order antes de llamar try_release_if_empty),
        la mesa se libera y el Cart de ese comensal se elimina en la misma
        operación."""
        db, table, ts = self._seed_bare_session()
        participant = fx.make_participant(db, table_session=ts, status="closed")
        cart = fx.make_cart(db, participant=participant, status="abierto")
        cart_id = cart.id
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        order = fx.make_customer_order(db, ts, participant=participant, status="abierta")
        fx.make_order_item(db, order, variant)
        db.commit()
        self.assertTrue(service.has_billable_orders(db, ts.id))

        order.status = "cancelada"
        db.commit()
        self.assertFalse(service.has_billable_orders(db, ts.id))

        released = service.try_release_if_empty(db, ts.id)

        self.assertTrue(released)
        db.refresh(table)
        self.assertEqual(table.status, "libre")
        self.assertIsNone(db.get(Cart, cart_id))

    def test_try_release_if_empty_no_toca_carritos_de_otra_mesa(self):
        """US4 (spec 039, Acceptance Scenario 1, FR-004): dos mesas activas
        independientes, cada una con su propia sesión/participante y un
        `Cart` huérfano — liberar solo una de las dos no afecta en absoluto
        el `Cart` de la mesa que sigue ocupada: mismo `id`, mismo `status`,
        sin eliminarse."""
        db, table_a, ts_a = self._seed_bare_session()
        participant_a = fx.make_participant(db, table_session=ts_a, status="closed")
        cart_a = fx.make_cart(db, participant=participant_a, status="abandonado")
        cart_a_id = cart_a.id

        table_b = fx.make_dining_table(db, status="ocupada")
        ts_b = fx.make_table_session(db, table=table_b)
        participant_b = fx.make_participant(db, table_session=ts_b, status="open")
        cart_b = fx.make_cart(db, participant=participant_b, status="abierto")
        cart_b_id = cart_b.id
        db.commit()

        released = service.try_release_if_empty(db, ts_a.id)

        self.assertTrue(released)
        db.refresh(table_a)
        self.assertEqual(table_a.status, "libre")
        self.assertIsNone(db.get(Cart, cart_a_id))

        db.refresh(table_b)
        self.assertEqual(table_b.status, "ocupada")
        cart_b_after = db.get(Cart, cart_b_id)
        self.assertIsNotNone(cart_b_after)
        self.assertEqual(cart_b_after.id, cart_b_id)
        self.assertEqual(cart_b_after.status, "abierto")

    # -------------------------------------------------------- list_sessions (T020)

    def test_list_sessions_only_active_y_todas(self):
        """CONGELA comportamiento actual: `only_active=True` (explícito, nunca el
        default `Query` sin resolver, research.md §1) solo devuelve sesiones
        `active`; `only_active=False` incluye también sesiones `closed`."""
        db = fx.new_session()
        t1 = fx.make_dining_table(db)
        ts_active = fx.make_table_session(db, table=t1, status="active")
        t2 = fx.make_dining_table(db)
        ts_closed = fx.make_table_session(db, table=t2, status="closed")
        db.commit()

        solo_activas = service.list_sessions(db, only_active=True)
        self.assertEqual({s.id for s in solo_activas}, {ts_active.id})

        todas = service.list_sessions(db, only_active=False)
        self.assertEqual({s.id for s in todas}, {ts_active.id, ts_closed.id})

    # -------------------------------------------------------- compute_bill (T021)

    def test_compute_bill_a01_camino_base(self):
        """CONGELA comportamiento actual — A-01 (camino A, correcto), reescrito
        para el modelo por conjunto de variantes de la spec 063 (A-58…A-65): con
        pedidos en distintos estados repartidos entre dos comensales y dos
        promociones activas (una `percent`, una `package_price`), el `total` y el
        desglose por comensal excluyen los pedidos `cancelada`/`pagada` y aplican
        el descuento por comensal."""
        db, table, ts = self._seed_bare_session()
        ana = fx.make_participant(db, table_session=ts, display_name="Ana", display_label="Ana")
        beto = fx.make_participant(db, table_session=ts, display_name="Beto", display_label="Beto")

        # Ana: una línea con descuento percent del 10%.
        cat_a = fx.make_category(db)
        prod_a = fx.make_product(db, category=cat_a)
        variant_a = fx.make_variant(db, product=prod_a, price=Decimal("10000"))
        promo = fx.make_promotion(db, status="active")
        fx.add_rule_to_promotion(
            db, promo, type="percent", value=Decimal("10"), min_qty=1, variants=[variant_a],
        )

        # Beto: un paquete de 2 variantes distintas del conjunto a $11.000
        # (mismo ahorro que el combo viejo: 6000+7000-11000 = 2000).
        cat_b = fx.make_category(db)
        prod_b = fx.make_product(db, category=cat_b)
        variant_1 = fx.make_variant(db, product=prod_b, price=Decimal("6000"))
        variant_2 = fx.make_variant(db, product=prod_b, price=Decimal("7000"))
        paquete = fx.make_promotion(db, status="active")
        fx.add_rule_to_promotion(
            db, paquete, type="package_price", value=Decimal("11000"), min_qty=2,
            variants=[variant_1, variant_2],
        )

        order_abierta = fx.make_customer_order(db, ts, status="abierta")
        fx.make_order_item(db, order_abierta, variant_a, participant_id=ana.id, estado_cocina="listo")
        fx.make_order_item(
            db, order_abierta, variant_1, participant_id=beto.id,
            estado_cocina="en_preparacion",
        )
        fx.make_order_item(
            db, order_abierta, variant_2, participant_id=beto.id,
            estado_cocina="listo",
        )

        order_cancelada = fx.make_customer_order(db, ts, status="cancelada")
        fx.make_order_item(db, order_cancelada, variant_a, participant_id=ana.id)

        order_pagada = fx.make_customer_order(db, ts, status="pagada")
        fx.make_order_item(db, order_pagada, variant_a, participant_id=beto.id)

        db.commit()

        resp = service.compute_bill(db, ts.id)

        por_comensal = {line.participant_id: line.subtotal for line in resp.split}
        self.assertEqual(por_comensal[ana.id], Decimal("9000.00"))
        self.assertEqual(por_comensal[beto.id], Decimal("11000.00"))
        self.assertEqual(resp.total, Decimal("20000.00"))
        # Solo el pedido 'abierta' entra en la cuenta; cancelada/pagada, no.
        self.assertEqual(resp.order_ids, [order_abierta.id])

    def test_compute_bill_expone_items_y_descuento_por_comensal(self):
        """spec 026, FR-006: no es CONGELA — comportamiento nuevo. `compute_bill`
        ya calculaba las líneas y el descuento de cada comensal para llegar al
        `subtotal`; ahora esos mismos valores también se serializan en
        `SessionBillLine.items`/`discount`, sin cambiar ningún cálculo."""
        db, table, ts = self._seed_bare_session()
        ana = fx.make_participant(db, table_session=ts, display_name="Ana", display_label="Ana")

        cat = fx.make_category(db)
        prod = fx.make_product(db, category=cat)
        variant = fx.make_variant(db, product=prod, price=Decimal("10000"))
        promo = fx.make_promotion(db, status="active")
        fx.add_rule_to_promotion(
            db, promo, type="percent", value=Decimal("10"), min_qty=1, variants=[variant],
        )

        order = fx.make_customer_order(db, ts, status="abierta")
        fx.make_order_item(db, order, variant, participant_id=ana.id, estado_cocina="listo", quantity=2)
        db.commit()

        resp = service.compute_bill(db, ts.id)

        line = next(l for l in resp.split if l.participant_id == ana.id)
        self.assertEqual(line.discount, Decimal("2000.00"))  # 10% de 20.000
        self.assertEqual(len(line.items), 1)
        self.assertEqual(line.items[0].quantity, Decimal("2"))
        self.assertEqual(line.items[0].unit_price, Decimal("10000.00"))
        self.assertEqual(line.items[0].line_total, Decimal("20000.00"))

    # -------------------------------------------------- close_session unified (T022)

    def test_close_session_unified_camino_feliz(self):
        """CONGELA comportamiento actual (`service.py:_close_unified`): una sola
        venta agrupa todo lo cobrable de la sesión, la sesión queda `closed` y la
        mesa se libera."""
        s = self._seed_billable_session()
        data = CloseSessionIn.model_validate({
            "cash_shift_id": str(s["shift"].id),
            "billing_mode": "unified",
            "payments": [self._pago(s["method"].id)],
        })

        resp = service.close_session(s["db"], s["ts"].id, data, s["cashier"])

        self.assertEqual(len(resp.sale_ids), 1)
        s["db"].refresh(s["ts"])
        s["db"].refresh(s["table"])
        self.assertEqual(s["ts"].status, "closed")
        self.assertEqual(s["table"].status, "libre")

    def test_close_session_borra_carrito_huerfano_de_comensal_ya_cerrado(self):
        """US2 escenario 1 (spec 039, Acceptance Scenario 1): además de los
        comensales activos de la sesión, un comensal ya `closed` desde antes
        del cobro con un `Cart` huérfano — al cobrar y cerrar
        (`close_session`), la mesa queda `libre` y ese `Cart` deja de
        existir."""
        s = self._seed_billable_session()
        huerfano = fx.make_participant(s["db"], table_session=s["ts"], status="closed")
        cart = fx.make_cart(s["db"], participant=huerfano, status="abandonado")
        cart_id = cart.id
        s["db"].commit()

        data = CloseSessionIn.model_validate({
            "cash_shift_id": str(s["shift"].id),
            "billing_mode": "unified",
            "payments": [self._pago(s["method"].id)],
        })

        service.close_session(s["db"], s["ts"].id, data, s["cashier"])

        s["db"].refresh(s["table"])
        self.assertEqual(s["table"].status, "libre")
        self.assertIsNone(s["db"].get(Cart, cart_id))

    def test_close_session_rollback_no_borra_carrito_ante_fallo_generico(self):
        """US3 escenario 3 (spec 039, Acceptance Scenario 3, FR-002): un
        fallo genérico dentro de `close_session` (mockeando
        `_close_unified`) sobre una sesión con un `Cart` huérfano — tras el
        `rollback()` ya existente, el `Cart` sigue existiendo con el mismo
        `id` y `status` que tenía antes del intento; ninguna eliminación
        parcial."""
        s = self._seed_billable_session()
        huerfano = fx.make_participant(s["db"], table_session=s["ts"], status="closed")
        cart = fx.make_cart(s["db"], participant=huerfano, status="abandonado")
        cart_id = cart.id
        s["db"].commit()

        data = CloseSessionIn.model_validate({
            "cash_shift_id": str(s["shift"].id),
            "billing_mode": "unified",
            "payments": [self._pago(s["method"].id)],
        })

        with mock.patch(
            "app.api.v1.table_sessions.service._close_unified",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                service.close_session(s["db"], s["ts"].id, data, s["cashier"])

        reloaded = s["db"].get(Cart, cart_id)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.id, cart_id)
        self.assertEqual(reloaded.status, "abandonado")

    # ------------------------------------------------- A-17 (R12) — spy_load (T023)

    def test_add_remove_set_assignments_a17_r12_cargan_sin_lock(self):
        """CONGELA comportamiento actual — A-17 (R12, `service.py:38-55` y sus
        llamadas sin `lock=True` en `add_participant:335`,
        `remove_participant:370`, `set_assignments:403`): a diferencia de
        `close_session` (que carga con `_load(..., lock=True)`), estas tres
        funciones cargan la sesión sin bloqueo — reproducido sin concurrencia
        real, espiando `service._load` (spec.md Historia 2, escenario 2)."""
        s = self._seed_billable_session()
        db = s["db"]

        data = CloseSessionIn.model_validate({
            "cash_shift_id": str(s["shift"].id),
            "billing_mode": "unified",
            "payments": [self._pago(s["method"].id)],
        })

        with fx.spy_load() as spy:
            nuevo = service.add_participant(db, s["ts"].id, "Nuevo", tenant_id=None)
            service.remove_participant(db, s["ts"].id, nuevo.id, tenant_id=None)
            service.set_assignments(
                db, s["ts"].id,
                [ItemAssignmentIn(order_item_id=s["item"].id, participant_id=s["ana"].id)],
                tenant_id=None,
            )
            idx_antes_de_cerrar = len(spy.calls)
            service.close_session(db, s["ts"].id, data, s["cashier"])

        previas = spy.calls[:idx_antes_de_cerrar]
        self.assertTrue(previas)
        self.assertTrue(all(c.lock is False for c in previas))
        # `close_session` sí pide el lock, en su primera carga de la sesión.
        self.assertTrue(spy.calls[idx_antes_de_cerrar].lock)

    # --------------------------------------------------------------- A-29 (T024)

    def test_close_session_unified_a29_applied_promotions_registra_las_dos(self):
        """CONGELA comportamiento actual — A-29, reescrito para la spec 063
        (A-64: `applied_promotions` resuelve A-29). Con dos promociones distintas
        descontando líneas de la misma venta unificada, `promotion_id` queda
        `None` (como hoy) **pero** `applied_promotions` de la `Sale` y de cada
        `CustomerOrder` registra las dos, y la suma cuadra con `discount`."""
        db, table, ts = self._seed_bare_session()
        ana = fx.make_participant(db, table_session=ts)

        cat = fx.make_category(db)
        prod = fx.make_product(db, category=cat)
        v1a = fx.make_variant(db, product=prod, price=Decimal("6000"))
        v1b = fx.make_variant(db, product=prod, price=Decimal("7000"))
        paquete1 = fx.make_promotion(db, status="active")
        fx.add_rule_to_promotion(
            db, paquete1, type="package_price", value=Decimal("11000"), min_qty=2,
            variants=[v1a, v1b],
        )

        v2a = fx.make_variant(db, product=prod, price=Decimal("4000"))
        v2b = fx.make_variant(db, product=prod, price=Decimal("5000"))
        paquete2 = fx.make_promotion(db, status="active")
        fx.add_rule_to_promotion(
            db, paquete2, type="package_price", value=Decimal("8000"), min_qty=2,
            variants=[v2a, v2b],
        )

        order = fx.make_customer_order(db, ts, status="abierta")
        for variant in (v1a, v1b, v2a, v2b):
            fx.make_order_item(db, order, variant, participant_id=ana.id)

        register = fx.make_cash_register(db)
        shift = fx.make_cash_shift(db, register=register)
        method = fx.make_payment_method(db)
        cashier = fx.make_user_double()
        db.commit()

        data = CloseSessionIn.model_validate({
            "cash_shift_id": str(shift.id),
            "billing_mode": "unified",
            "payments": [self._pago(method.id, amount=Decimal("20000"))],
        })

        resp = service.close_session(db, ts.id, data, cashier)

        ventas = self._ventas(db, ts.id)
        self.assertEqual(len(ventas), 1)
        venta = ventas[0]
        # Ambos paquetes descuentan: (6000+7000-11000) + (4000+5000-8000) = 3000
        self.assertEqual(venta.discount, Decimal("3000.00"))
        self.assertIsNone(venta.promotion_id)
        registradas = {e["promotion_id"] for e in venta.applied_promotions}
        self.assertEqual(registradas, {str(paquete1.id), str(paquete2.id)})
        suma = sum(Decimal(e["amount"]) for e in venta.applied_promotions)
        self.assertEqual(suma, Decimal("3000.00"))
        db.refresh(order)
        self.assertEqual(
            {e["promotion_id"] for e in order.applied_promotions},
            {str(paquete1.id), str(paquete2.id)},
        )

    # ---------------------------------------------------- RN-MESA-13 / A-38 (T025)

    def test_close_session_split_rn_mesa_13_a38_un_solo_comensal_sin_minimo(self):
        """CONGELA comportamiento actual — A-38 (RN-MESA-13, `service.py:578-671`):
        una mesa de un único comensal con consumo propio se puede cerrar en
        `billing_mode='split'` con un solo bloque para ese comensal, sin ninguna
        restricción de mínimo de comensales — equivalente en la práctica a un
        `unified` disfrazado."""
        db, table, ts = self._seed_bare_session()
        ana = fx.make_participant(db, table_session=ts, display_name="Ana", display_label="Ana")

        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        order = fx.make_customer_order(db, ts, status="abierta")
        fx.make_order_item(db, order, variant, participant_id=ana.id)

        register = fx.make_cash_register(db)
        shift = fx.make_cash_shift(db, register=register)
        method = fx.make_payment_method(db)
        cashier = fx.make_user_double()
        db.commit()

        data = CloseSessionIn.model_validate({
            "cash_shift_id": str(shift.id),
            "billing_mode": "split",
            "splits": [
                {"participant_id": str(ana.id), "payments": [self._pago(method.id)]},
            ],
        })

        resp = service.close_session(db, ts.id, data, cashier)

        self.assertEqual(len(resp.sale_ids), 1)
        db.refresh(ts)
        self.assertEqual(ts.status, "closed")

    # ---------------------------------------------------- RN-MESA-24 / A-38 (T026)

    def test_remove_participant_rn_mesa_24_a38_con_productos_asignados_409(self):
        """CONGELA comportamiento actual — A-38 (RN-MESA-24, `service.py:362-388`):
        `remove_participant` responde 409 y no quita a un comensal con al menos
        un producto asignado, sin distinguir si ese ítem está `anulado` o su
        pedido ya no es cobrable."""
        s = self._seed_billable_session()
        db, ts, ana, item = s["db"], s["ts"], s["ana"], s["item"]

        with self.assertRaises(HTTPException) as ctx:
            service.remove_participant(db, ts.id, ana.id, tenant_id=None)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIsNotNone(db.get(SessionParticipant, ana.id))

        # Mismo resultado con el único ítem asignado ya anulado y su pedido
        # pagado (no cobrable): la consulta de `remove_participant` no filtra
        # por `estado_cocina` ni por `CustomerOrder.status`.
        item.estado_cocina = "anulado"
        s["order"].status = "pagada"
        db.commit()

        with self.assertRaises(HTTPException) as ctx2:
            service.remove_participant(db, ts.id, ana.id, tenant_id=None)
        self.assertEqual(ctx2.exception.status_code, 409)
        self.assertIsNotNone(db.get(SessionParticipant, ana.id))

    # ------------------------------------------- bill_changed tras commit (T027)

    def test_bill_changed_se_publica_una_vez_tras_commit(self):
        """CONGELA comportamiento actual (FR-010a, spec.md Historia 2, escenario
        7): `add_participant`, `remove_participant` y `set_assignments`, al
        tener éxito, invocan `app.core.events.bill_changed` exactamente una vez,
        con el `tenant_id`/`table_session_id` correctos, después de que la
        transacción del `service` ya hizo commit (`db` refleja el nuevo estado
        dentro del propio callback interceptado)."""
        s = self._seed_billable_session()
        db, ts = s["db"], s["ts"]
        seen: dict = {}

        def _spy_add(tenant_id, *, table_session_id):
            seen["count_after_add"] = db.execute(
                select(func.count(SessionParticipant.id))
                .where(SessionParticipant.table_session_id == table_session_id)
            ).scalar_one()

        with mock.patch("app.core.events.bill_changed", side_effect=_spy_add) as spy:
            nuevo = service.add_participant(db, ts.id, "Carla", tenant_id=42)
        spy.assert_called_once_with(42, table_session_id=ts.id)
        self.assertEqual(seen["count_after_add"], 3)  # Ana, Beto, Carla

        def _spy_remove(tenant_id, *, table_session_id):
            seen["removed_visible"] = db.get(SessionParticipant, nuevo.id)

        with mock.patch("app.core.events.bill_changed", side_effect=_spy_remove) as spy2:
            service.remove_participant(db, ts.id, nuevo.id, tenant_id=42)
        spy2.assert_called_once_with(42, table_session_id=ts.id)
        self.assertIsNone(seen["removed_visible"])

        def _spy_set(tenant_id, *, table_session_id):
            seen["item_participant_after_commit"] = db.get(OrderItem, s["item"].id).participant_id

        with mock.patch("app.core.events.bill_changed", side_effect=_spy_set) as spy3:
            service.set_assignments(
                db, ts.id,
                [ItemAssignmentIn(order_item_id=s["item"].id, participant_id=s["beto"].id)],
                tenant_id=42,
            )
        spy3.assert_called_once_with(42, table_session_id=ts.id)
        self.assertEqual(seen["item_participant_after_commit"], s["beto"].id)

    # -------------------------------------------------- add_participant (T028)

    def test_add_participant_camino_feliz_desambigua_nombre(self):
        """CONGELA comportamiento actual: `add_participant` desambigua
        `display_name` vía `_unique_label`/`unique_display_label` de
        `cart.service` (ya congelado por `specs/015-caracterizacion-cart/`):
        "Ana" repetido en la misma sesión queda "Ana (2)"."""
        db, table, ts = self._seed_bare_session()
        fx.make_participant(db, table_session=ts, display_name="Ana", display_label="Ana")
        db.commit()

        nuevo = service.add_participant(db, ts.id, "Ana", tenant_id=None)

        self.assertEqual(nuevo.display_name, "Ana")
        self.assertEqual(nuevo.display_label, "Ana (2)")
        self.assertEqual(nuevo.status, "open")
        self.assertEqual(nuevo.table_session_id, ts.id)

    # ------------------------------------------- release_paid_session (T030)

    def test_release_paid_session_409_si_queda_algo_por_cobrar(self):
        """Comportamiento nuevo (spec 028, T027): `release_paid_session` es la
        inversa de `close_session` — rechaza con 409 si todavía queda algo
        billable (un pedido ni pagado ni cancelado) en la sesión, y no toca
        nada."""
        s = self._seed_billable_session()
        db, ts = s["db"], s["ts"]

        with self.assertRaises(HTTPException) as ctx:
            service.release_paid_session(db, ts.id, s["cashier"])
        self.assertEqual(ctx.exception.status_code, 409)
        db.refresh(ts)
        self.assertEqual(ts.status, "active")

    def test_release_paid_session_409_con_cocina_en_curso_sobre_pedido_pagado(self):
        """Aun sin nada billable, la comida puede seguir en curso en cocina
        sobre un pedido ya 'pagada' (se cobró antes de que terminara de
        prepararse, p.ej. vía `checkout.checkout_and_send`):
        `release_paid_session` sigue rechazando, porque reusa
        `_assert_closable` sobre **todos** los pedidos de la sesión —no solo
        los billables, que ya no incluirían este— para que el chequeo de
        cocina en curso siga aplicando."""
        db, table, ts = self._seed_bare_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        order = fx.make_customer_order(db, ts, status="pagada")
        fx.make_order_item(db, order, variant, estado_cocina="en_preparacion")
        cashier = fx.make_user_double()
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.release_paid_session(db, ts.id, cashier)
        self.assertEqual(ctx.exception.status_code, 409)
        db.refresh(ts)
        self.assertEqual(ts.status, "active")

    def test_release_paid_session_libera_pese_a_cocina_pendiente_de_un_pedido_cancelado(self):
        """Bugfix (spec 050): un pedido `'cancelada'` no anula el `estado_cocina`
        de sus ítems (`cancel_order`, `orders/checkout.py` — deliberado, para no
        interferir con el ajuste de inventario), así que antes de este fix
        `_assert_closable` seguía viendo ese ítem como cocina en curso y
        bloqueaba `release_paid_session` para siempre, sin ninguna acción
        posible desde la UI (el pedido ya es terminal). Contraste directo con
        `test_release_paid_session_409_con_cocina_en_curso_sobre_pedido_pagado`
        (mismo escenario de ítem `'en_preparacion'`, pero con `status='pagada'`,
        que sí debe seguir bloqueando)."""
        db, table, ts = self._seed_bare_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        order = fx.make_customer_order(db, ts, status="cancelada")
        fx.make_order_item(db, order, variant, estado_cocina="pendiente")
        cashier = fx.make_user_double()
        db.commit()

        resp = service.release_paid_session(db, ts.id, cashier)

        self.assertEqual(resp.status, "libre")
        db.refresh(ts)
        db.refresh(table)
        self.assertEqual(ts.status, "closed")
        self.assertEqual(table.status, "libre")

    def test_release_paid_session_libera_la_mesa_cuando_todo_esta_pagado_y_listo(self):
        """Camino feliz: nada billable y la comida del único pedido 'pagada'
        ya está 'listo' → cierra la sesión en cascada y libera la mesa, igual
        que `close_session` pero sin ninguna venta que emitir."""
        db, table, ts = self._seed_bare_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        order = fx.make_customer_order(db, ts, status="pagada")
        fx.make_order_item(db, order, variant, estado_cocina="listo")
        cashier = fx.make_user_double()
        db.commit()

        resp = service.release_paid_session(db, ts.id, cashier)

        self.assertEqual(resp.status, "libre")
        self.assertEqual(resp.dining_table_id, table.id)
        db.refresh(ts)
        db.refresh(table)
        self.assertEqual(ts.status, "closed")
        self.assertEqual(table.status, "libre")

    def test_release_paid_session_borra_carrito_huerfano(self):
        """US2 escenario 2 (spec 039, Acceptance Scenario 2): sobre una sesión
        ya completamente pagada con un `Cart` huérfano, `release_paid_session`
        libera la mesa y ese `Cart` deja de existir."""
        db, table, ts = self._seed_bare_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        order = fx.make_customer_order(db, ts, status="pagada")
        fx.make_order_item(db, order, variant, estado_cocina="listo")
        huerfano = fx.make_participant(db, table_session=ts, status="closed")
        cart = fx.make_cart(db, participant=huerfano, status="abandonado")
        cart_id = cart.id
        cashier = fx.make_user_double()
        db.commit()

        resp = service.release_paid_session(db, ts.id, cashier)

        self.assertEqual(resp.status, "libre")
        db.refresh(table)
        self.assertEqual(table.status, "libre")
        self.assertIsNone(db.get(Cart, cart_id))

    def test_release_paid_session_libera_una_mesa_pagada_por_qr_aunque_la_orden_siga_abierta(self):
        """Regresión (spec 028): `approve_payment_attempt`/
        `confirm_cash_payment_attempt` (`orders/checkout.py`) ya generan la
        `Sale`/`Invoice` al confirmar un pago QR, pero DEJAN la orden en
        `status="abierta"` a propósito —no `"pagada"`— para que siga siendo
        consumo activo visible mientras cocina la termina (`activeOrders`/
        `tableOrders` del frontend excluyen `"pagada"`; marcarla así de
        inmediato haría ver la mesa como libre con el pedido aún en
        preparación). Antes de este fix, `has_billable_orders`/
        `_billable_orders` solo miraban `status`, así que ese pedido nunca
        dejaba de contar como "por cobrar" y la mesa jamás se liberaba —ni
        por el barrido ni por "Liberar Mesa"— aunque ya estuviera pagada y
        facturada: el bug reportado en producción. Se reproduce sembrando una
        `Sale` real asociada al pedido, sin pasar por `status="pagada"`."""
        db, table, ts = self._seed_bare_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        order = fx.make_customer_order(db, ts, status="abierta")
        fx.make_order_item(db, order, variant, estado_cocina="listo")
        cashier = fx.make_user_double()
        shift = fx.make_cash_shift(db)
        db.add(Sale(
            cash_shift_id=shift.id,
            customer_order_id=order.id,
            table_session_id=ts.id,
            dining_table_id=table.id,
            user_id=cashier.id,
            status="paid",
        ))
        db.commit()

        resp = service.release_paid_session(db, ts.id, cashier)

        self.assertEqual(resp.status, "libre")
        db.refresh(ts)
        db.refresh(table)
        self.assertEqual(ts.status, "closed")
        self.assertEqual(table.status, "libre")

    def test_release_paid_session_doble_intento_409_en_el_segundo(self):
        """Mismo mecanismo de lock por fila que `close_session` — reproducido
        sin concurrencia real (mismo patrón que el resto de este módulo, que
        no ejercita hilos): tras el primer `release_paid_session` la sesión ya
        no está 'active', así que un segundo intento sobre la misma sesión es
        409 y no repite el cierre ni vuelve a tocar la mesa."""
        db, table, ts = self._seed_bare_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        order = fx.make_customer_order(db, ts, status="pagada")
        fx.make_order_item(db, order, variant, estado_cocina="listo")
        cashier = fx.make_user_double()
        db.commit()

        service.release_paid_session(db, ts.id, cashier)

        with self.assertRaises(HTTPException) as ctx:
            service.release_paid_session(db, ts.id, cashier)
        self.assertEqual(ctx.exception.status_code, 409)
        db.refresh(table)
        self.assertEqual(table.status, "libre")


_BOGOTA = ZoneInfo("America/Bogota")


def _utc_para_hora_local(y, mo, d, h, mi) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=_BOGOTA).astimezone(timezone.utc)


class TestVigenciaCongeladaSesionMesa(unittest.TestCase):
    """spec 073, US4 (FR-012a, A-70): en una cuenta de mesa con varias rondas,
    la vigencia temporal se evalúa contra un único instante — el del pedido más
    antiguo pendiente de cobro. La agrupación de líneas no cambia."""

    def test_scenario5_dos_rondas_se_evaluan_juntas_contra_la_primera(self):
        """Ronda 1 a las 19:59 (1 cono), ronda 2 a las 20:05 (1 cono), promo
        del 50% llevando 2 vigente solo hasta las 20:00 → al cobrar la cuenta
        completa, los dos conos completan el umbral de 2 y se evalúan contra las
        19:59: el descuento se aplica."""
        db, table, ts = TestTableSessionsService()._seed_bare_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=Decimal("8000"))

        promo = fx.make_promotion(
            db, status="active", start_time=time(18, 0), end_time=time(20, 0),
        )
        fx.add_rule_to_promotion(
            db, promo, type="percent", value=Decimal("50"), min_qty=2, variants=[variant],
        )

        ronda1 = fx.make_customer_order(
            db, ts, status="abierta",
            promotion_evaluated_at=_utc_para_hora_local(2026, 9, 2, 19, 59),
        )
        fx.make_order_item(db, ronda1, variant, estado_cocina="listo")
        ronda2 = fx.make_customer_order(
            db, ts, status="abierta",
            promotion_evaluated_at=_utc_para_hora_local(2026, 9, 2, 20, 5),
        )
        fx.make_order_item(db, ronda2, variant, estado_cocina="listo")
        db.commit()

        resp = service.compute_bill(db, ts.id)
        # 2 conos a 8000 = 16000, 50% del bloque de 2 = 8000 de descuento.
        self.assertEqual(resp.total, Decimal("8000"))

    def test_mezcla_congelado_y_anterior_manda_el_congelado_mas_antiguo(self):
        """Cuenta que mezcla un pedido con instante congelado (19:59) y uno
        anterior a la spec (`promotion_evaluated_at` NULL) → manda el instante
        congelado más antiguo (FR-012a)."""
        db, table, ts = TestTableSessionsService()._seed_bare_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=Decimal("8000"))
        promo = fx.make_promotion(
            db, status="active", start_time=time(18, 0), end_time=time(20, 0),
        )
        fx.add_rule_to_promotion(
            db, promo, type="percent", value=Decimal("50"), min_qty=2, variants=[variant],
        )

        congelado = fx.make_customer_order(
            db, ts, status="abierta",
            promotion_evaluated_at=_utc_para_hora_local(2026, 9, 2, 19, 59),
        )
        fx.make_order_item(db, congelado, variant, estado_cocina="listo")
        anterior = fx.make_customer_order(db, ts, status="abierta")  # sin instante
        fx.make_order_item(db, anterior, variant, estado_cocina="listo")
        db.commit()

        resp = service.compute_bill(db, ts.id)
        # Manda 19:59 (dentro de franja) → descuento del bloque de 2 aplicado.
        self.assertEqual(resp.total, Decimal("8000"))


class TestTryReleaseAndNotify(unittest.TestCase):
    """spec 089 (A-95, contracts/session-closed.md §1): `try_release_if_empty` devuelve las sesiones
    que cerró (lista vacía = no cerró; la truthiness de siempre se conserva) y
    `notify_sessions_closed` publica un `session.closed` por sesión sin lanzar nunca."""

    def _seed(self, *, con_pedido_por_cobrar=False, comensal_abierto=False):
        db = fx.new_session()
        table = fx.make_dining_table(db, status="ocupada")
        ts = fx.make_table_session(db, table=table)
        ana = fx.make_participant(db, table_session=ts, display_name="Ana")
        ana.status = "closed"
        if comensal_abierto:
            fx.make_participant(db, table_session=ts, display_name="Beto")
        if con_pedido_por_cobrar:
            fx.make_customer_order(db, ts, status="abierta")
        db.commit()
        return db, table, ts

    def test_devuelve_la_sesion_cerrada_cuando_libera_la_mesa(self):
        db, table, ts = self._seed()

        closed = service.try_release_if_empty(db, ts.id)

        self.assertEqual([c.id for c in closed], [ts.id])
        self.assertTrue(closed)  # truthiness preservada para quien solo hacía `if try_release...`
        self.assertEqual(ts.status, "closed")
        self.assertEqual(table.status, "libre")

    def test_devuelve_lista_vacia_si_queda_un_comensal(self):
        db, table, ts = self._seed(comensal_abierto=True)

        closed = service.try_release_if_empty(db, ts.id)

        self.assertEqual(closed, [])
        self.assertFalse(closed)
        self.assertEqual(ts.status, "active")

    def test_devuelve_lista_vacia_si_hay_pedidos_por_cobrar(self):
        db, table, ts = self._seed(con_pedido_por_cobrar=True)

        self.assertEqual(service.try_release_if_empty(db, ts.id), [])
        self.assertEqual(ts.status, "active")

    def test_devuelve_lista_vacia_si_la_sesion_ya_estaba_cerrada(self):
        db, table, ts = self._seed()
        ts.status = "closed"
        db.commit()

        self.assertEqual(service.try_release_if_empty(db, ts.id), [])

    def test_notify_sessions_closed_publica_un_evento_por_sesion(self):
        db, table, ts = self._seed()
        otra = fx.make_table_session(db, table=fx.make_dining_table(db))
        db.commit()

        with mock.patch("app.core.events.session_closed") as publicar:
            service.notify_sessions_closed(7, [ts, otra], reason="released")

        self.assertEqual(publicar.call_count, 2)
        publicar.assert_any_call(
            7, table_session_id=ts.id, dining_table_id=ts.dining_table_id, reason="released"
        )
        publicar.assert_any_call(
            7, table_session_id=otra.id, dining_table_id=otra.dining_table_id, reason="released"
        )

    def test_notify_sessions_closed_no_lanza_si_el_publicador_falla(self):
        db, table, ts = self._seed()

        with mock.patch("app.core.events.session_closed", side_effect=RuntimeError("redis caído")):
            service.notify_sessions_closed(7, [ts], reason="empty")  # no debe propagar

    def test_notify_sessions_closed_sin_sesiones_o_sin_tenant_no_publica(self):
        db, table, ts = self._seed()

        with mock.patch("app.core.events.session_closed") as publicar:
            service.notify_sessions_closed(7, [], reason="empty")
            service.notify_sessions_closed(None, [ts], reason="empty")

        publicar.assert_not_called()


if __name__ == "__main__":
    unittest.main()
