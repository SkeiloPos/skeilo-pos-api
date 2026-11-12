"""`app/api/v1/orders/kitchen.py` (specs/017-caracterizacion-orders, Historia 3;
reescrito spec 147, A-151).

Spec 147 retira `transition_kitchen`/`_ALLOWED`, `mark_order_ready` y
`accept_order_for_kitchen` (FR-002/research.md R4): la entrega por ítem
individual deja de existir, sustituida por la acción única
`kitchen.deliver_order` (idempotente, FR-001/FR-002a/FR-011). Los tests de
esas tres funciones retiradas se eliminan aquí (no se comentan ni se dejan en
rojo) — este módulo pasa a cubrir `deliver_order` y mantiene `void_item`,
adaptado a `estado_cocina == "cancelado"` (antes `"anulado"`) y al nuevo
criterio de "pedido ya cobrado" (`payment_status == "PAID"`, ya no
`status == "pagada"`, que no existe).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_orders_kitchen -v
"""
from datetime import datetime, timedelta
from decimal import Decimal
import unittest
from unittest import mock

from fastapi import HTTPException

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.catalog.schemas import OptionSelectionIn
from app.api.v1.orders import checkout, kitchen
from app.api.v1.orders.schemas import BlockIn, CancelIn, OrderItemIn, PayIn, VoidItemIn
from app.api.v1.sales.schemas import PaymentIn
from app.core.domain_errors import ConflictError
from app.models.sale import Sale

PRECIO = Decimal("10000")


class TestKitchen(unittest.TestCase):
    # ------------------------------------------------------------- Helpers

    def _seed_item(
        self, *, order_status="pendiente", estado_cocina="pendiente", payment_status="UNPAID",
        kitchen_status=None,
    ):
        db = fx.new_session()
        ts = fx.make_table_session(db)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        insumo = fx.make_inventory_item(db, current_stock=Decimal("1000"))
        fx.make_recipe_item(db, variant, insumo, quantity=Decimal("1"))
        order = fx.make_customer_order(
            db, ts, status=order_status, payment_status=payment_status, channel="POS",
            kitchen_status=kitchen_status,
        )
        item = fx.make_order_item(db, order, variant, estado_cocina=estado_cocina)
        db.commit()
        return dict(db=db, ts=ts, order=order, item=item, variant=variant, insumo=insumo)

    def _user(self):
        return fx.make_user_double()

    # ------------------------------------------------------ deliver_order (spec 147)

    def test_deliver_order_pasa_pedido_e_items_pendientes_a_entregado(self):
        """Comportamiento NUEVO (spec 147, FR-001): pedido `'pendiente'` con
        ítems `'pendiente'` → una sola llamada a `deliver_order` deja el
        pedido `'entregada'` y todos sus ítems `'entregado'`, sin tocar
        `payment_status` (FR-003)."""
        s = self._seed_item(order_status="pendiente", estado_cocina="pendiente")
        db, order = s["db"], s["order"]

        with mock.patch("app.core.events.order_delivered") as publicar:
            actualizado = kitchen.deliver_order(db, order.id, tenant_id=1)
            publicar.assert_called_once()

        self.assertEqual(actualizado.status, "entregada")
        self.assertEqual(actualizado.payment_status, "UNPAID")
        self.assertTrue(all(it.estado_cocina == "entregado" for it in actualizado.items))

    def test_deliver_order_409_si_cancelada(self):
        """FR-010: un pedido `'cancelada'` rechaza la entrega como transición
        inválida."""
        s = self._seed_item(order_status="cancelada", estado_cocina="cancelado")
        db, order = s["db"], s["order"]

        with self.assertRaises(HTTPException) as ctx:
            kitchen.deliver_order(db, order.id, tenant_id=1)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_deliver_order_es_idempotente_sin_segundo_evento(self):
        """FR-011/SC-006: entregar un pedido ya `'entregada'` sin ítems
        `'pendiente'` nuevos es un no-op exitoso -- no cambia nada y no emite
        un segundo `order_delivered`."""
        s = self._seed_item(order_status="entregada", estado_cocina="entregado")
        db, order = s["db"], s["order"]

        with mock.patch("app.core.events.order_delivered") as publicar:
            actualizado = kitchen.deliver_order(db, order.id, tenant_id=1)
            publicar.assert_not_called()

        self.assertEqual(actualizado.status, "entregada")
        self.assertTrue(all(it.estado_cocina == "entregado" for it in actualizado.items))

    def test_deliver_order_re_entrega_fr002a_promueve_solo_el_item_nuevo(self):
        """FR-002a: un pedido ya `'entregada'` puede recibir un ítem nuevo
        (nace `'pendiente'`); una segunda `deliver_order` lo promueve sin
        tocar los ítems ya `'entregado'`, y SÍ emite un segundo evento (hubo
        un cambio real)."""
        s = self._seed_item(order_status="entregada", estado_cocina="entregado")
        db, order, variant = s["db"], s["order"], s["variant"]
        nuevo = fx.make_order_item(db, order, variant, estado_cocina="pendiente")
        db.commit()

        with mock.patch("app.core.events.order_delivered") as publicar:
            actualizado = kitchen.deliver_order(db, order.id, tenant_id=1)
            publicar.assert_called_once()

        self.assertEqual(actualizado.status, "entregada")
        nuevo_actualizado = next(it for it in actualizado.items if it.id == nuevo.id)
        self.assertEqual(nuevo_actualizado.estado_cocina, "entregado")

    # ------------------------------------------------- receive_order (a pedido del usuario)

    def test_receive_order_pasa_pendiente_a_confirmado_sin_tocar_cocina_ni_pago(self):
        """A pedido del usuario: "Recibir" solo mueve `status`, nunca
        `estado_cocina` ni `payment_status`."""
        s = self._seed_item(order_status="pendiente", estado_cocina="pendiente")
        db, order = s["db"], s["order"]

        with mock.patch("app.core.events.order_received") as publicar:
            actualizado = kitchen.receive_order(db, order.id, tenant_id=1)
            publicar.assert_called_once()

        self.assertEqual(actualizado.status, "confirmado")
        self.assertEqual(actualizado.payment_status, "UNPAID")
        self.assertTrue(all(it.estado_cocina == "pendiente" for it in actualizado.items))

    def test_receive_order_409_si_cancelada(self):
        s = self._seed_item(order_status="cancelada", estado_cocina="cancelado")
        db, order = s["db"], s["order"]

        with self.assertRaises(HTTPException) as ctx:
            kitchen.receive_order(db, order.id, tenant_id=1)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_receive_order_es_idempotente_sin_segundo_evento(self):
        s = self._seed_item(order_status="confirmado", estado_cocina="pendiente")
        db, order = s["db"], s["order"]

        with mock.patch("app.core.events.order_received") as publicar:
            actualizado = kitchen.receive_order(db, order.id, tenant_id=1)
            publicar.assert_not_called()

        self.assertEqual(actualizado.status, "confirmado")

    def test_receive_order_no_op_si_ya_entregada(self):
        """Recibir un pedido que ya avanzó a `'entregada'` (p. ej. "Entregar"
        directo desde `'pendiente'`, sin pasar por "Recibir") no lo retrocede."""
        s = self._seed_item(order_status="entregada", estado_cocina="entregado")
        db, order = s["db"], s["order"]

        actualizado = kitchen.receive_order(db, order.id, tenant_id=1)
        self.assertEqual(actualizado.status, "entregada")

    def test_deliver_order_acepta_confirmado_como_origen(self):
        """A pedido del usuario: `deliver_order` entrega igual desde
        `'confirmado'`, no solo desde `'pendiente'`."""
        s = self._seed_item(order_status="confirmado", estado_cocina="pendiente")
        db, order = s["db"], s["order"]

        with mock.patch("app.core.events.order_delivered") as publicar:
            actualizado = kitchen.deliver_order(db, order.id, tenant_id=1)
            publicar.assert_called_once()

        self.assertEqual(actualizado.status, "entregada")
        self.assertTrue(all(it.estado_cocina == "entregado" for it in actualizado.items))

    # --------------------------- deliver_order: guard de cocina (spec 153) --

    def test_deliver_order_409_order_not_ready_con_cocina_activa_y_no_listo(self):
        """Comportamiento NUEVO (spec 153, FR-004): con el módulo de Cocina
        activo, un pedido de mesa con `kitchen_status` distinto de `'LISTO'`
        (p. ej. `'SIN_PREPARAR'`) responde 409 `ORDER_NOT_READY` al intentar
        entregarlo -- hoy este caso falla porque la entrega se permite sin
        esa validación."""
        s = self._seed_item(
            order_status="confirmado", estado_cocina="pendiente",
            payment_status="PAID", kitchen_status="SIN_PREPARAR",
        )
        db, order = s["db"], s["order"]
        tenant = fx.make_tenant_double()

        with mock.patch.object(kitchen, "has_module_access", return_value=True):
            with self.assertRaises(ConflictError) as ctx:
                kitchen.deliver_order(db, order.id, tenant_id=1, tenant=tenant)
        self.assertEqual(ctx.exception.code, "ORDER_NOT_READY")
        db.refresh(order)
        self.assertEqual(order.status, "confirmado")

    def test_deliver_order_permite_entregar_con_cocina_activa_y_listo(self):
        """Camino feliz (spec 153, FR-004): con el módulo de Cocina activo,
        `kitchen_status=='LISTO'` sí permite entregar -- no debe cambiar."""
        s = self._seed_item(
            order_status="confirmado", estado_cocina="pendiente",
            payment_status="PAID", kitchen_status="LISTO",
        )
        db, order = s["db"], s["order"]
        tenant = fx.make_tenant_double()

        with mock.patch.object(kitchen, "has_module_access", return_value=True):
            actualizado = kitchen.deliver_order(db, order.id, tenant_id=1, tenant=tenant)

        self.assertEqual(actualizado.status, "entregada")

    def test_deliver_order_sin_cocina_activa_no_exige_kitchen_status(self):
        """No-regresión (spec 153, US3, FR-006/FR-009): sin el módulo de
        Cocina activo, `deliver_order` sigue rechazando únicamente
        `status=='cancelada'`, sin exigir `kitchen_status` -- igual que
        antes de esta spec."""
        s = self._seed_item(
            order_status="confirmado", estado_cocina="pendiente",
            payment_status="PAID", kitchen_status=None,
        )
        db, order = s["db"], s["order"]
        tenant = fx.make_tenant_double()

        with mock.patch.object(kitchen, "has_module_access", return_value=False):
            actualizado = kitchen.deliver_order(db, order.id, tenant_id=1, tenant=tenant)

        self.assertEqual(actualizado.status, "entregada")

    # ----------------------------------------- send_to_kitchen (spec 150)

    def _seed_order_sin_items(self, *, order_status="pendiente", payment_status="PAID", channel="POS", order_type="DINE_IN"):
        """Variante sin ítem (innecesario para `send_to_kitchen`, que no toca
        `estado_cocina` de ningún ítem) -- más liviana para ejercitar los 3
        orígenes (manual/mesa/domicilio) sin arrastrar producto/variante."""
        db = fx.new_session()
        ts = fx.make_table_session(db)
        order = fx.make_customer_order(
            db, ts, status=order_status, payment_status=payment_status,
            channel=channel, order_type=order_type,
        )
        db.commit()
        return db, order

    def test_send_to_kitchen_exitoso_origen_mesa_usa_hora_del_servidor(self):
        """Historia 1 (FR-001/FR-003/FR-004): pedido confirmado y pagado de
        mesa (POS/DINE_IN) → `kitchen_status` pasa a `'SIN_PREPARAR'` y
        `sent_to_kitchen_at` queda poblado con la hora del SERVIDOR (no hay
        ningún valor de cliente que pudiera colarse: la función no recibe
        ningún timestamp como parámetro)."""
        db, order = self._seed_order_sin_items(channel="POS", order_type="DINE_IN")

        with mock.patch("app.core.events.order_sent_to_kitchen") as publicar:
            actualizado = kitchen.send_to_kitchen(db, order.id, tenant_id=1)
            publicar.assert_called_once()

        self.assertEqual(actualizado.kitchen_status, "SIN_PREPARAR")
        self.assertIsNotNone(actualizado.sent_to_kitchen_at)

    def test_send_to_kitchen_exitoso_origen_manual_mostrador(self):
        """Historia 1 (FR-002): mismo comportamiento para un pedido de
        mostrador (sin mesa, DINE_IN/TAKEAWAY vía POS) -- la elegibilidad no
        depende de `channel`/`order_type` (research.md Decisión 7)."""
        db, order = self._seed_order_sin_items(channel="POS", order_type="TAKEAWAY")

        actualizado = kitchen.send_to_kitchen(db, order.id, tenant_id=1)

        self.assertEqual(actualizado.kitchen_status, "SIN_PREPARAR")
        self.assertIsNotNone(actualizado.sent_to_kitchen_at)

    def test_send_to_kitchen_exitoso_origen_domicilio(self):
        """Historia 1 (FR-002): mismo comportamiento para un pedido a
        domicilio (WHATSAPP/DELIVERY)."""
        db, order = self._seed_order_sin_items(channel="WHATSAPP", order_type="DELIVERY")

        actualizado = kitchen.send_to_kitchen(db, order.id, tenant_id=1)

        self.assertEqual(actualizado.kitchen_status, "SIN_PREPARAR")
        self.assertIsNotNone(actualizado.sent_to_kitchen_at)

    def test_send_to_kitchen_404_si_no_existe(self):
        db, _ = self._seed_order_sin_items()
        import uuid
        with self.assertRaises(HTTPException) as ctx:
            kitchen.send_to_kitchen(db, uuid.uuid4(), tenant_id=1)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_send_to_kitchen_409_order_not_confirmed_si_cancelada(self):
        """Historia 1/FR-010 (edge case, data-model.md): `status == 'cancelada'`
        es la única exclusión de "orden confirmada" (research.md Decisión 0)."""
        db, order = self._seed_order_sin_items(order_status="cancelada", payment_status="PAID")

        with self.assertRaises(ConflictError) as ctx:
            kitchen.send_to_kitchen(db, order.id, tenant_id=1)
        self.assertEqual(ctx.exception.code, "ORDER_NOT_CONFIRMED")
        db.refresh(order)
        self.assertIsNone(order.kitchen_status)

    def test_send_to_kitchen_409_order_not_paid_si_unpaid(self):
        """Historia 2 (FR-006): `payment_status != 'PAID'` rechaza el envío
        sin mutar `kitchen_status`."""
        db, order = self._seed_order_sin_items(payment_status="UNPAID")

        with self.assertRaises(ConflictError) as ctx:
            kitchen.send_to_kitchen(db, order.id, tenant_id=1)
        self.assertEqual(ctx.exception.code, "ORDER_NOT_PAID")
        db.refresh(order)
        self.assertIsNone(order.kitchen_status)

    def test_send_to_kitchen_409_already_in_kitchen_en_segundo_intento(self):
        """Historia 3 (FR-007/FR-008/SC-003): un segundo envío sobre el mismo
        pedido (doble clic o reintento de red) se rechaza con
        `ALREADY_IN_KITCHEN`, sin duplicar la cola ni alterar
        `sent_to_kitchen_at` del envío original. El `SELECT ... FOR UPDATE`
        serializa la carrera concurrente real contra Postgres (ver
        quickstart.md); en SQLite en memoria (sin bloqueo real de fila) dos
        invocaciones secuenciales sobre la misma fila ya ejercitan la misma
        rama de código que vería la segunda transacción tras adquirir el
        lock."""
        db, order = self._seed_order_sin_items()

        primero = kitchen.send_to_kitchen(db, order.id, tenant_id=1)
        momento_original = primero.sent_to_kitchen_at

        with self.assertRaises(ConflictError) as ctx:
            kitchen.send_to_kitchen(db, order.id, tenant_id=1)
        self.assertEqual(ctx.exception.code, "ALREADY_IN_KITCHEN")

        db.refresh(order)
        self.assertEqual(order.kitchen_status, "SIN_PREPARAR")
        self.assertEqual(order.sent_to_kitchen_at, momento_original)

    # --- send_to_kitchen: modalidad de cobro del plan para TAKEAWAY/DELIVERY (spec 157, A-160)

    def _seed_order_mostrador_sin_items(
        self, *, order_type="TAKEAWAY", payment_status="UNPAID",
    ):
        """TAKEAWAY/DELIVERY sin mesa/sesión real -- variante de
        `_seed_order_sin_items` con `dining_table_id=None` explícito, para
        que `kitchen_requires_prior_payment` entre por la rama que lee la
        modalidad del plan en vez de la rama de mesa."""
        db = fx.new_session()
        ts = fx.make_table_session(db)
        order = fx.make_customer_order(
            db, ts, table_session_id=None, dining_table_id=None,
            status="confirmado", payment_status=payment_status,
            channel="POS", order_type=order_type,
        )
        db.commit()
        return db, order

    def test_send_to_kitchen_rechaza_takeaway_sin_pagar_con_pay_before_receiving(self):
        """FR-004 (sin cambios para esta combinación): con el plan en
        `PAY_BEFORE_RECEIVING`, el envío manual de un TAKEAWAY sin pagar
        sigue rechazándose con `ORDER_NOT_PAID` -- la única vía para esta
        combinación es el auto-envío al cobrar (US1, `checkout.pay_order`)."""
        db, order = self._seed_order_mostrador_sin_items(order_type="TAKEAWAY")
        tenant = fx.make_tenant_double()

        with mock.patch.object(
            kitchen.plan_service, "get_qr_payment_instruction_mode",
            return_value="PAY_BEFORE_RECEIVING",
        ):
            with self.assertRaises(ConflictError) as ctx:
                kitchen.send_to_kitchen(db, order.id, tenant_id=1, tenant=tenant)
        self.assertEqual(ctx.exception.code, "ORDER_NOT_PAID")
        db.refresh(order)
        self.assertIsNone(order.kitchen_status)

    def test_send_to_kitchen_permite_takeaway_sin_pagar_con_pay_after_receiving(self):
        """Comportamiento NUEVO (spec 157, FR-003, A-160): con el plan en
        `PAY_AFTER_RECEIVING` (o sin configurar, FR-008), el envío manual de
        un TAKEAWAY sin pagar tiene éxito -- `payment_status` sigue
        `'UNPAID'`."""
        db, order = self._seed_order_mostrador_sin_items(order_type="TAKEAWAY")
        tenant = fx.make_tenant_double()

        with mock.patch.object(
            kitchen.plan_service, "get_qr_payment_instruction_mode",
            return_value="PAY_AFTER_RECEIVING",
        ):
            actualizado = kitchen.send_to_kitchen(db, order.id, tenant_id=1, tenant=tenant)

        self.assertEqual(actualizado.kitchen_status, "SIN_PREPARAR")
        self.assertEqual(actualizado.payment_status, "UNPAID")

    def test_send_to_kitchen_permite_delivery_sin_pagar_con_pay_after_receiving(self):
        """Mismo comportamiento que el test anterior, para DELIVERY
        (equivalencia TAKEAWAY/DELIVERY confirmada en Clarifications)."""
        db, order = self._seed_order_mostrador_sin_items(order_type="DELIVERY")
        tenant = fx.make_tenant_double()

        with mock.patch.object(
            kitchen.plan_service, "get_qr_payment_instruction_mode",
            return_value="PAY_AFTER_RECEIVING",
        ):
            actualizado = kitchen.send_to_kitchen(db, order.id, tenant_id=1, tenant=tenant)

        self.assertEqual(actualizado.kitchen_status, "SIN_PREPARAR")
        self.assertEqual(actualizado.payment_status, "UNPAID")

    def test_send_to_kitchen_takeaway_sin_pagar_sin_tenant_se_asume_sin_exigir_pago(self):
        """`kitchen_requires_prior_payment` (research.md Decisión 1): sin
        `tenant` resuelto (p. ej. llamador legado que no lo pasa), un
        TAKEAWAY/DELIVERY no exige pago previo -- no debe romper llamadores
        existentes que invocan `send_to_kitchen` sin el nuevo kwarg."""
        db, order = self._seed_order_mostrador_sin_items(order_type="TAKEAWAY")

        actualizado = kitchen.send_to_kitchen(db, order.id, tenant_id=1)

        self.assertEqual(actualizado.kitchen_status, "SIN_PREPARAR")
        self.assertEqual(actualizado.payment_status, "UNPAID")

    def test_deliver_order_exige_listo_tambien_para_takeaway_con_cocina_activa(self):
        """Hallazgo post-implementación (spec 157, A-160): ahora que
        `checkout.checkout_and_send` ya no auto-despacha un TAKEAWAY/DELIVERY
        a `'entregada'` con el módulo de Cocina activo (sigue su propio ciclo
        de cocina en vez de bypasearlo), `deliver_order` debe exigir
        `kitchen_status == 'LISTO'` para CUALQUIER pedido con el módulo
        activo, no solo los de mesa -- el guard `ORDER_NOT_READY` (spec 153)
        deja de estar condicionado a `dining_table_id is not None`.
        `payment_status` no interviene en el guard, igual que antes."""
        s = self._seed_item(
            order_status="confirmado", estado_cocina="pendiente",
            payment_status="UNPAID", kitchen_status="SIN_PREPARAR",
        )
        s["order"].dining_table_id = None
        s["order"].order_type = "TAKEAWAY"
        s["db"].commit()
        db, order = s["db"], s["order"]
        tenant = fx.make_tenant_double()

        with mock.patch.object(kitchen, "has_module_access", return_value=True):
            with self.assertRaises(ConflictError) as ctx:
                kitchen.deliver_order(db, order.id, tenant_id=1, tenant=tenant)
        self.assertEqual(ctx.exception.code, "ORDER_NOT_READY")
        db.refresh(order)
        self.assertEqual(order.status, "confirmado")

    def test_deliver_order_permite_entregar_takeaway_listo_con_cocina_activa(self):
        """Camino feliz del hallazgo anterior: con `kitchen_status == 'LISTO'`,
        un TAKEAWAY/DELIVERY sí puede entregarse, igual que una mesa."""
        s = self._seed_item(
            order_status="confirmado", estado_cocina="pendiente",
            payment_status="UNPAID", kitchen_status="LISTO",
        )
        s["order"].dining_table_id = None
        s["order"].order_type = "TAKEAWAY"
        s["db"].commit()
        db, order = s["db"], s["order"]
        tenant = fx.make_tenant_double()

        with mock.patch.object(kitchen, "has_module_access", return_value=True):
            actualizado = kitchen.deliver_order(db, order.id, tenant_id=1, tenant=tenant)

        self.assertEqual(actualizado.status, "entregada")
        self.assertEqual(actualizado.payment_status, "UNPAID")

    def test_deliver_order_sin_cocina_activa_takeaway_no_exige_kitchen_status(self):
        """No-regresión (FR-007): sin el módulo de Cocina activo, un
        TAKEAWAY/DELIVERY sigue entregándose sin exigir `kitchen_status`,
        igual que antes de esta spec."""
        s = self._seed_item(
            order_status="confirmado", estado_cocina="pendiente",
            payment_status="UNPAID", kitchen_status=None,
        )
        s["order"].dining_table_id = None
        s["order"].order_type = "TAKEAWAY"
        s["db"].commit()
        db, order = s["db"], s["order"]
        tenant = fx.make_tenant_double()

        with mock.patch.object(kitchen, "has_module_access", return_value=False):
            actualizado = kitchen.deliver_order(db, order.id, tenant_id=1, tenant=tenant)

        self.assertEqual(actualizado.status, "entregada")
        self.assertEqual(actualizado.payment_status, "UNPAID")

    # ------------------------ void_item ya no anula un pedido pagado (spec 029/147)

    def test_void_item_409_si_payment_status_paid_spec_029_147(self):
        """Comportamiento NUEVO (spec 029, Historia 1, FR-005/006/007;
        criterio reescrito spec 147): pedido con `payment_status == 'PAID'` y
        un ítem aún `'pendiente'` → `void_item` responde 409 en vez de
        anular, sin mutar el ítem ni registrar `OrderItemVoidLog`. Antes de
        spec 147 esto se leía de `status == 'pagada'`; ese valor ya no
        existe, el pago es independiente del status."""
        s = self._seed_item(order_status="entregada", payment_status="PAID", estado_cocina="pendiente")
        db, item = s["db"], s["item"]
        user = self._user()

        with self.assertRaises(HTTPException) as ctx:
            kitchen.void_item(db, item.id, VoidItemIn(motivo="prueba"), user)
        self.assertEqual(ctx.exception.status_code, 409)
        db.refresh(item)
        self.assertEqual(item.estado_cocina, "pendiente")

    def test_void_item_409_si_sale_existe_pero_payment_status_no_se_actualizo_spec_029(self):
        """Comportamiento NUEVO (spec 029): el caso real que motivó la spec,
        distinto del anterior -- un camino QR/mostrador legado puede dejar la
        `Sale` emitida sin que `payment_status` llegara a `'PAID'` (D2/D3 de
        research.md). `void_item` debe rechazar igual, reconociendo el pago
        por la `Sale` asociada, no solo por `payment_status`."""
        s = self._seed_item(order_status="entregada", payment_status="UNPAID", estado_cocina="entregado")
        db, item, order = s["db"], s["item"], s["order"]
        shift = fx.make_cash_shift(db)
        cashier = fx.make_user_double()
        db.add(Sale(
            cash_shift_id=shift.id,
            customer_order_id=order.id,
            user_id=cashier.id,
            status="paid",
        ))
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            kitchen.void_item(db, item.id, VoidItemIn(motivo="prueba"), cashier)
        self.assertEqual(ctx.exception.status_code, 409)
        db.refresh(item)
        self.assertEqual(item.estado_cocina, "entregado")

    # ------------------------------------------------------------ void_item (T039 original)

    def test_void_item_anula_y_reemplaza_con_reversa_y_nuevo_descuento(self):
        """CONGELA comportamiento actual (`kitchen.py`, spec.md Historia 3,
        escenario 5; literal `estado_cocina` reescrito spec 147,
        `'anulado'` → `'cancelado'`): ítem `'pendiente'` con
        `data.replacement` válido → el original queda `'cancelado'` (con
        reversa de inventario, por ser `'pendiente'`) y se crea uno nuevo
        `'pendiente'` con `void_de` apuntando al original y su propio
        descuento de inventario."""
        from sqlalchemy import select
        from app.models.inventory_movement import InventoryMovement

        s = self._seed_item(estado_cocina="pendiente")
        db, item, insumo = s["db"], s["item"], s["insumo"]
        stock_inicial = Decimal(insumo.current_stock)
        user = self._user()

        resultado = kitchen.void_item(
            db, item.id,
            VoidItemIn(
                motivo="cambio de sabor",
                replacement=OrderItemIn(product_variant_id=s["variant"].id, quantity=1),
            ),
            user,
        )

        original = next(it for it in resultado.items if it.id == item.id)
        self.assertEqual(original.estado_cocina, "cancelado")

        nuevo = next(it for it in resultado.items if it.void_de == item.id)
        self.assertEqual(nuevo.estado_cocina, "pendiente")

        movimientos = db.execute(
            select(InventoryMovement).where(InventoryMovement.reference_id == item.order_id)
        ).scalars().all()
        tipos = sorted(m.type for m in movimientos)
        self.assertEqual(tipos, ["in", "out"])
        # Reversa del original ('in') + descuento del nuevo ('out'), cantidad
        # idéntica (1 unidad de receta): stock termina igual que al empezar.
        db.refresh(insumo)
        self.assertEqual(Decimal(insumo.current_stock), stock_inicial)

    def test_void_item_con_reemplazo_de_grupo_cantidad_separa_unit_price_y_marca_per_line(self):
        """Spec 117 (A-126): el ítem de reemplazo de un `void_item` separa
        `unit_price`/`addons_total` y persiste `per_line=True` en su
        `OrderItemOption` cuando la selección del reemplazo trae una opción de un
        grupo "Por cantidad" -- mismo tratamiento que los demás call-sites de
        escritura, nunca listado hasta ahora como séptimo call-site equivalente."""
        s = self._seed_item(estado_cocina="pendiente")
        db, item, user = s["db"], s["item"], self._user()
        group = fx.make_option_group(
            db, selection_mode="cantidad", pricing_type="incluido",
            min_select=0, max_select=1,
        )
        fx.link_variant_group(db, s["variant"], group, min_select=0, max_select=1)
        gomitas = fx.make_option(db, group=group, extra_price=Decimal("1000"))
        db.commit()

        resultado = kitchen.void_item(
            db, item.id,
            VoidItemIn(
                motivo="cambio de sabor",
                replacement=OrderItemIn(
                    product_variant_id=s["variant"].id, quantity=2,
                    options=[OptionSelectionIn(option_id=gomitas.id, quantity=1)],
                ),
            ),
            user,
        )

        nuevo = next(it for it in resultado.items if it.void_de == item.id)
        self.assertEqual(nuevo.unit_price, PRECIO)
        self.assertEqual(nuevo.addons_total, Decimal("1000"))
        self.assertEqual(nuevo.line_total, PRECIO * 2 + Decimal("1000"))
        self.assertTrue(nuevo.options[0].per_line)

    # ----------------------------- invariante A-25 [PROTEGIDA] de referencia

    def test_a25_protegida_ninguna_funcion_acepta_transicion_fuera_de_su_lista_blanca(self):
        """CONGELA comportamiento actual -- A-25 [PROTEGIDA] (spec.md
        Historia 3, escenario 6), adaptado spec 147: de las cinco funciones
        públicas que mutan `status`/`estado_cocina`/`payment_status`
        (`block_order`, `confirm_order`, `pay_order`, `cancel_order`,
        `void_item` -- `deliver_order`/`transition_kitchen`/
        `mark_order_ready` ya tienen su propio test de rechazo arriba o
        fueron retirados), cada una impone su propia transición validada
        sobre el modelo de 3 estados."""
        # void_item: un ítem ya cancelado no se puede volver a cancelar.
        s3 = self._seed_item(estado_cocina="cancelado")
        with self.assertRaises(HTTPException) as ctx3:
            kitchen.void_item(s3["db"], s3["item"].id, VoidItemIn(motivo="x"), self._user())
        self.assertEqual(ctx3.exception.status_code, 409)

        # block_order: orden ya 'cancelada' es transición inválida (spec 147
        # retiró el status intermedio 'bloqueada' y el gate de cocina, FR-004).
        s4 = self._seed_item(order_status="cancelada", estado_cocina="cancelado")
        with self.assertRaises(HTTPException) as ctx4:
            checkout.block_order(s4["db"], s4["order"].id, BlockIn(version=0))
        self.assertEqual(ctx4.exception.status_code, 409)

        # confirm_order: orden 'entregada' no está en 'pendiente' (antes: 'abierta').
        s5 = self._seed_item(order_status="entregada", estado_cocina="entregado")
        with self.assertRaises(HTTPException) as ctx5:
            checkout.confirm_order(s5["db"], s5["order"].id, self._user())
        self.assertEqual(ctx5.exception.status_code, 409)

        # pay_order: orden 'cancelada' es transición inválida (spec 147: ya
        # no depende de 'bloqueada', FR-004/FR-010).
        s6 = self._seed_item(order_status="cancelada", estado_cocina="cancelado")
        db6, order6 = s6["db"], s6["order"]
        register = fx.make_cash_register(db6)
        shift = fx.make_cash_shift(db6, register=register)
        method = fx.make_payment_method(db6)
        db6.commit()
        with self.assertRaises(HTTPException) as ctx6:
            checkout.pay_order(
                db6, order6.id,
                PayIn(cash_shift_id=shift.id, payments=[PaymentIn(payment_method_id=method.id, amount=PRECIO)]),
                self._user(),
            )
        self.assertEqual(ctx6.exception.status_code, 409)

        # cancel_order: orden ya 'entregada'+'PAID' requiere reembolso, fuera
        # de alcance (spec 147, FR-009) -- ya no existe 'pagada'.
        s7 = self._seed_item(order_status="entregada", payment_status="PAID", estado_cocina="entregado")
        with self.assertRaises(HTTPException) as ctx7:
            checkout.cancel_order(s7["db"], s7["order"].id, CancelIn(motivo="x"), self._user())
        self.assertEqual(ctx7.exception.status_code, 409)


class TestKitchenBoard(unittest.TestCase):
    """Spec 151, Historia 1 (FR-001/FR-002/FR-003/FR-011): lectura agregada
    del tablero (`kitchen.get_kitchen_board`)."""

    def _seed_kitchen_order(
        self, *, order_status="pendiente", payment_status="PAID", kitchen_status="SIN_PREPARAR",
        version=0, sent_to_kitchen_at=None, order_type="DINE_IN", channel="POS",
    ):
        db = fx.new_session()
        ts = fx.make_table_session(db)
        order = fx.make_customer_order(
            db, ts, status=order_status, payment_status=payment_status, channel=channel,
            order_type=order_type, kitchen_status=kitchen_status, version=version,
            sent_to_kitchen_at=sent_to_kitchen_at or datetime.now(),
        )
        db.commit()
        return db, order

    def test_tablero_vacio_sin_pedidos_en_cocina(self):
        db = fx.new_session()
        columnas = kitchen.get_kitchen_board(db)
        self.assertEqual(columnas, {"SIN_PREPARAR": [], "EN_PROGRESO": [], "LISTO": []})

    def test_dos_pedidos_quedan_en_orden_fifo_por_sent_to_kitchen_at(self):
        """FR-003: orden ascendente por `sent_to_kitchen_at`."""
        db, mas_viejo = self._seed_kitchen_order(sent_to_kitchen_at=datetime(2026, 1, 1, 10, 0))
        ts = fx.make_table_session(db)
        mas_nuevo = fx.make_customer_order(
            db, ts, status="pendiente", payment_status="PAID", channel="POS", order_type="DINE_IN",
            kitchen_status="SIN_PREPARAR", version=0, sent_to_kitchen_at=datetime(2026, 1, 1, 10, 5),
        )
        db.commit()

        columnas = kitchen.get_kitchen_board(db)

        ids = [c["order_id"] for c in columnas["SIN_PREPARAR"]]
        self.assertEqual(ids, [mas_viejo.id, mas_nuevo.id])

    def test_empate_de_sent_to_kitchen_at_se_desempata_por_id_ascendente(self):
        """FR-003: mismo `sent_to_kitchen_at` → desempate por `id`."""
        momento = datetime(2026, 1, 1, 10, 0)
        db, primero = self._seed_kitchen_order(sent_to_kitchen_at=momento)
        ts = fx.make_table_session(db)
        segundo = fx.make_customer_order(
            db, ts, status="pendiente", payment_status="PAID", channel="POS", order_type="DINE_IN",
            kitchen_status="SIN_PREPARAR", version=0, sent_to_kitchen_at=momento,
        )
        db.commit()

        ids_esperados = sorted([primero.id, segundo.id])

        columnas = kitchen.get_kitchen_board(db)

        ids = [c["order_id"] for c in columnas["SIN_PREPARAR"]]
        self.assertEqual(ids, ids_esperados)

    def test_pedido_cancelado_con_kitchen_status_no_nulo_no_aparece(self):
        """FR-011 (adelantado desde US4): `status == 'cancelada'` excluye la
        orden del tablero aunque `kitchen_status` siga sin limpiarse."""
        db, cancelado = self._seed_kitchen_order(order_status="cancelada", kitchen_status="EN_PROGRESO")

        columnas = kitchen.get_kitchen_board(db)

        todos_los_ids = [c["order_id"] for lst in columnas.values() for c in lst]
        self.assertNotIn(cancelado.id, todos_los_ids)

    def test_pedido_entregado_con_kitchen_status_no_nulo_no_aparece(self):
        db, entregado = self._seed_kitchen_order(order_status="entregada", kitchen_status="LISTO")

        columnas = kitchen.get_kitchen_board(db)

        todos_los_ids = [c["order_id"] for lst in columnas.values() for c in lst]
        self.assertNotIn(entregado.id, todos_los_ids)


class TestKitchenCardSerialization(unittest.TestCase):
    """Spec 151, Historia 3 (FR-012): serialización de `CardResponse` vía
    `kitchen.get_kitchen_board` (contracts/kitchen-board.md)."""

    def _seed_order_type(self, *, order_type, customer_name=None, delivery_address=None):
        db = fx.new_session()
        ts = fx.make_table_session(db)
        order = fx.make_customer_order(
            db, ts, status="pendiente", payment_status="PAID", channel="POS",
            order_type=order_type, kitchen_status="SIN_PREPARAR", version=0,
            sent_to_kitchen_at=datetime(2026, 1, 1, 10, 0),
            customer_name=customer_name, delivery_address=delivery_address,
        )
        db.commit()
        return db, order

    def test_dine_in_serializa_origin_mesa_con_table_number(self):
        db, _ = self._seed_order_type(order_type="DINE_IN")

        card = kitchen.get_kitchen_board(db)["SIN_PREPARAR"][0]

        self.assertEqual(card["origin"], "mesa")
        self.assertIsNotNone(card["table_number"])
        self.assertIsNone(card["customer_name"])
        self.assertIsNone(card["delivery_address"])

    def test_delivery_serializa_origin_domicilio_con_cliente_y_direccion(self):
        db, _ = self._seed_order_type(
            order_type="DELIVERY", customer_name="Ana", delivery_address="Calle 1",
        )

        card = kitchen.get_kitchen_board(db)["SIN_PREPARAR"][0]

        self.assertEqual(card["origin"], "domicilio")
        self.assertIsNone(card["table_number"])
        self.assertEqual(card["customer_name"], "Ana")
        self.assertEqual(card["delivery_address"], "Calle 1")

    def test_takeaway_sin_cliente_serializa_origin_caja_sin_nombre_ni_error(self):
        """Edge case (`spec.md`): pedido TAKEAWAY sin cliente -- sin nombre
        inventado, sin error."""
        db, _ = self._seed_order_type(order_type="TAKEAWAY")

        card = kitchen.get_kitchen_board(db)["SIN_PREPARAR"][0]

        self.assertEqual(card["origin"], "caja")
        self.assertIsNone(card["table_number"])
        self.assertIsNone(card["customer_name"])
        self.assertIsNone(card["delivery_address"])

    def test_items_con_adiciones_y_notas_se_incluyen_completos(self):
        from app.models.order_item import OrderItemOption

        db, order = self._seed_order_type(order_type="DINE_IN")
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        group = fx.make_option_group(
            db, selection_mode="conteo", pricing_type="incluido", min_select=0, max_select=1,
        )
        fx.link_variant_group(db, variant, group, min_select=0, max_select=1)
        opcion = fx.make_option(db, group=group)
        item = fx.make_order_item(db, order, variant, quantity=2, notes="Sin cebolla")
        db.add(OrderItemOption(order_item_id=item.id, option_id=opcion.id, quantity=1))
        db.commit()

        card = kitchen.get_kitchen_board(db)["SIN_PREPARAR"][0]

        self.assertEqual(len(card["items"]), 1)
        linea = card["items"][0]
        self.assertEqual(linea["product_name"], product.name)
        self.assertEqual(linea["quantity"], 2)
        self.assertEqual(linea["notes"], "Sin cebolla")
        self.assertEqual(linea["addons"], [{"name": opcion.name, "quantity": 1}])


class TestChangeKitchenStatus(unittest.TestCase):
    """Spec 151, Historia 2 (FR-005 a FR-010): `kitchen.change_kitchen_status`."""

    def _seed_kitchen_order(
        self, *, order_status="pendiente", payment_status="PAID", kitchen_status="SIN_PREPARAR",
        version=0, sent_to_kitchen_at=None,
    ):
        db = fx.new_session()
        ts = fx.make_table_session(db)
        order = fx.make_customer_order(
            db, ts, status=order_status, payment_status=payment_status, channel="POS",
            order_type="DINE_IN", kitchen_status=kitchen_status, version=version,
            sent_to_kitchen_at=sent_to_kitchen_at or datetime(2026, 1, 1, 10, 0),
        )
        db.commit()
        return db, order

    def test_exito_cambia_kitchen_status_incrementa_version_sin_tocar_otros_campos(self):
        db, order = self._seed_kitchen_order(kitchen_status="SIN_PREPARAR", version=0)
        momento_original = order.sent_to_kitchen_at

        with mock.patch("app.core.events.order_kitchen_status_changed") as publicar:
            kitchen.change_kitchen_status(db, order.id, "EN_PROGRESO", 0, tenant_id=1)
            publicar.assert_called_once()

        db.refresh(order)
        self.assertEqual(order.kitchen_status, "EN_PROGRESO")
        self.assertEqual(order.version, 1)
        self.assertEqual(order.status, "pendiente")
        self.assertEqual(order.payment_status, "PAID")
        self.assertEqual(order.sent_to_kitchen_at, momento_original)

    def test_retroceso_de_listo_a_en_progreso_es_aceptado(self):
        """FR-005: cualquier destino del conjunto es válido, incluyendo
        retrocesos -- no hay máquina de estados direccional."""
        db, order = self._seed_kitchen_order(kitchen_status="LISTO", version=3)

        resultado = kitchen.change_kitchen_status(db, order.id, "EN_PROGRESO", 3, tenant_id=1)

        self.assertEqual(resultado["kitchen_status"], "EN_PROGRESO")
        self.assertEqual(resultado["version"], 4)

    def test_404_si_el_pedido_no_existe(self):
        db, _ = self._seed_kitchen_order()
        import uuid
        with self.assertRaises(HTTPException) as ctx:
            kitchen.change_kitchen_status(db, uuid.uuid4(), "EN_PROGRESO", 0, tenant_id=1)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_409_not_in_kitchen_si_kitchen_status_es_nulo(self):
        db, order = self._seed_kitchen_order(kitchen_status=None, version=0)

        with self.assertRaises(ConflictError) as ctx:
            kitchen.change_kitchen_status(db, order.id, "EN_PROGRESO", 0, tenant_id=1)
        self.assertEqual(ctx.exception.code, "NOT_IN_KITCHEN")

    def test_409_kitchen_status_conflict_si_version_no_coincide(self):
        """FR-008: el payload del error incluye el `kitchen_status` vigente."""
        db, order = self._seed_kitchen_order(kitchen_status="SIN_PREPARAR", version=5)

        with self.assertRaises(ConflictError) as ctx:
            kitchen.change_kitchen_status(db, order.id, "EN_PROGRESO", 0, tenant_id=1)
        self.assertEqual(ctx.exception.code, "KITCHEN_STATUS_CONFLICT")
        self.assertEqual(ctx.exception.details, {"kitchen_status": "SIN_PREPARAR"})
        db.refresh(order)
        self.assertEqual(order.kitchen_status, "SIN_PREPARAR")
        self.assertEqual(order.version, 5)

    def test_409_payment_not_paid_si_no_esta_pagado(self):
        db, order = self._seed_kitchen_order(payment_status="UNPAID", version=0)

        with self.assertRaises(ConflictError) as ctx:
            kitchen.change_kitchen_status(db, order.id, "EN_PROGRESO", 0, tenant_id=1)
        self.assertEqual(ctx.exception.code, "PAYMENT_NOT_PAID")

    def test_409_order_not_active_si_cancelada(self):
        db, order = self._seed_kitchen_order(order_status="cancelada", version=0)

        with self.assertRaises(ConflictError) as ctx:
            kitchen.change_kitchen_status(db, order.id, "EN_PROGRESO", 0, tenant_id=1)
        self.assertEqual(ctx.exception.code, "ORDER_NOT_ACTIVE")

    def test_409_order_not_active_si_entregada(self):
        db, order = self._seed_kitchen_order(order_status="entregada", version=0)

        with self.assertRaises(ConflictError) as ctx:
            kitchen.change_kitchen_status(db, order.id, "EN_PROGRESO", 0, tenant_id=1)
        self.assertEqual(ctx.exception.code, "ORDER_NOT_ACTIVE")

    def test_dos_movimientos_concurrentes_solo_uno_aplica_el_otro_409(self):
        """FR-008 (research.md Decisión 9): el `SELECT ... FOR UPDATE` sirve
        al segundo movimiento el `version` ya incrementado por el primero --
        mismo idioma que `test_send_to_kitchen_409_already_in_kitchen_en_segundo_intento`."""
        db, order = self._seed_kitchen_order(kitchen_status="SIN_PREPARAR", version=0)

        primero = kitchen.change_kitchen_status(db, order.id, "EN_PROGRESO", 0, tenant_id=1)
        self.assertEqual(primero["version"], 1)

        with self.assertRaises(ConflictError) as ctx:
            kitchen.change_kitchen_status(db, order.id, "LISTO", 0, tenant_id=1)
        self.assertEqual(ctx.exception.code, "KITCHEN_STATUS_CONFLICT")

        db.refresh(order)
        self.assertEqual(order.kitchen_status, "EN_PROGRESO")
        self.assertEqual(order.version, 1)


class TestKitchenBoardRoutesWiring(unittest.TestCase):
    """Mismo idioma que `TestSendToKitchenRouteWiring`: inspecciona
    `route.dependant.dependencies` de la app real en vez de levantar un
    servidor HTTP."""

    @classmethod
    def setUpClass(cls):
        from unittest import mock as _mock
        from fastapi.routing import APIRoute
        with _mock.patch("app.core.db.initialize_database", lambda *a, **k: None):
            from app.main import app
        cls.board_route = next(
            r for r in app.routes
            if isinstance(r, APIRoute) and r.path == "/api/v1/orders/kitchen-board"
        )
        cls.move_route = next(
            r for r in app.routes
            if isinstance(r, APIRoute) and r.path == "/api/v1/orders/{order_id}/kitchen-status"
        )

    def test_la_ruta_de_lectura_exige_require_kitchen_board_view(self):
        from app.core.dependencies import require_kitchen_board_view
        llamadas = {d.call for d in self.board_route.dependant.dependencies}
        self.assertIn(require_kitchen_board_view, llamadas)

    def test_la_ruta_de_movimiento_exige_require_kitchen_role(self):
        from app.core.dependencies import require_kitchen_role
        llamadas = {d.call for d in self.move_route.dependant.dependencies}
        self.assertIn(require_kitchen_role, llamadas)

    def test_la_ruta_de_movimiento_exige_el_modulo_cocina_del_plan(self):
        module_keys = {
            getattr(d.call, "module_key", None) for d in self.move_route.dependant.dependencies
        }
        self.assertIn("cocina", module_keys)


class TestVoidItemRevertsDiscounts118(unittest.TestCase):
    """spec 118 (research.md D3, FR-006): `void_item` recalcula y persiste
    `discounted_unit_price`/`discounted_line_total` sobre las líneas vigentes
    restantes del pedido vía `refresh_order_item_discounts` -- al anular una
    de dos filas con descuento ya persistido, la fila restante vuelve a
    `None` si la cantidad restante cae bajo el umbral de la promoción.

    No es un characterization test: el defecto que corrige nunca estuvo
    congelado (research.md D6)."""

    def test_anular_una_de_dos_filas_con_descuento_revierte_la_restante_a_none(self):
        db = fx.new_session()
        ts = fx.make_table_session(db)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=Decimal("7000"))
        insumo = fx.make_inventory_item(db, current_stock=Decimal("1000"))
        fx.make_recipe_item(db, variant, insumo, quantity=Decimal("1"))
        order = fx.make_customer_order(db, ts, status="pendiente", channel="POS")
        promo = fx.make_promotion(db)
        fx.add_rule_to_promotion(
            db, promo, type="package_price", value=Decimal("12000"), min_qty=2,
            variants=[variant],
        )
        # Dos filas con el descuento ya persistido, como lo dejaría
        # `refresh_order_item_discounts` tras una segunda adición (Historia 2).
        item_a = fx.make_order_item(
            db, order, variant, quantity=1, estado_cocina="pendiente",
            discounted_unit_price=Decimal("6000"), discounted_line_total=Decimal("6000"),
        )
        item_b = fx.make_order_item(
            db, order, variant, quantity=1, estado_cocina="pendiente",
            discounted_unit_price=Decimal("6000"), discounted_line_total=Decimal("6000"),
        )
        db.commit()

        kitchen.void_item(db, item_a.id, VoidItemIn(motivo="cambio de parecer"), fx.make_user_double())

        db.refresh(item_b)
        self.assertIsNone(item_b.discounted_unit_price)
        self.assertIsNone(item_b.discounted_line_total)


class TestSendToKitchenRouteWiring(unittest.TestCase):
    """Edge cases de `spec.md` (FR-011, módulo de plan) que no se pueden
    ejercitar llamando a `kitchen.send_to_kitchen` directamente -- viven en
    las dependencias de la ruta, resueltas por FastAPI antes de llegar a la
    función. Mismo idioma que `test_module_route_inventory.py`: inspecciona
    `route.dependant.dependencies` de la app real en vez de levantar un
    servidor HTTP."""

    @classmethod
    def setUpClass(cls):
        from unittest import mock as _mock
        from fastapi.routing import APIRoute
        # `app.main` ejecuta `create_app()` al importarse, que abre Postgres y
        # migra -- se sustituye por un no-op antes de importar (mismo patrón
        # que `test_module_route_inventory._aplicacion_real`).
        with _mock.patch("app.core.db.initialize_database", lambda *a, **k: None):
            from app.main import app
        cls.route = next(
            r for r in app.routes
            if isinstance(r, APIRoute) and r.path == "/api/v1/orders/{order_id}/send-to-kitchen"
        )

    def test_la_ruta_exige_admin_o_cajero(self):
        """FR-011: cualquier otro rol recibe 403 — dependencia
        `require_tenant_admin_or_cashier`, a diferencia de `/receive` y
        `/deliver`, que solo exigen `get_current_user`."""
        from app.core.dependencies import require_tenant_admin_or_cashier
        llamadas = {d.call for d in self.route.dependant.dependencies}
        self.assertIn(require_tenant_admin_or_cashier, llamadas)

    def test_la_ruta_exige_el_modulo_cocina_del_plan(self):
        """Edge case de `spec.md`: un tenant sin `cocina` en el plan recibe
        403 `MODULE_NOT_IN_PLAN` -- dependencia `require_module_access("cocina")`
        (gating ya existente desde spec 149, primer punto real de
        enforcement)."""
        module_keys = {
            getattr(d.call, "module_key", None) for d in self.route.dependant.dependencies
        }
        self.assertIn("cocina", module_keys)


if __name__ == "__main__":
    unittest.main()
