"""Tests de la nueva funcionalidad — spec 024-pagos-ordenes-mesa, User Stories
2 (revisión de comprobante), 3 (confirmación de efectivo) y 4 (gate de
comanda): FR-009/FR-010/FR-010a/FR-013/FR-014/FR-017/FR-018.

No son characterization tests: la revisión de comprobantes, la confirmación de
efectivo por intento y el gate de `confirm_order` son comportamiento nuevo
(research.md spec 024, Decisiones 5 y 9) — se verifican contra
`spec.md`/`contracts/cashier-payment-review.md`/`contracts/order-confirm-gate.md`,
no contra un comportamiento heredado.

Extendido por spec 026-mejoras-ux-comanda (FR-001/FR-002, research.md
Decisión 1): `confirm_cash_payment_attempt`/`approve_payment_attempt` ahora
disparan `_confirm_order_impl` (antes `confirm_order`, llamado manualmente
aparte) dentro de su propia transacción — confirmar el pago y enviar el
pedido a cocina ocurren en una sola llamada. Por eso `_seed_order_recibida`
siembra receta+inventario por defecto: cualquier confirmación exitosa ahora
también descuenta inventario, no solo cambia el estado del intento de pago.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_orders_payment_gate -v
"""
from datetime import datetime, time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
import unittest
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import select

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import checkout
from app.models.inventory_movement import InventoryMovement
from app.models.sale import Sale

PRECIO = Decimal("18000")

_BOGOTA = ZoneInfo("America/Bogota")


def _utc_para_hora_local(y, mo, d, h, mi) -> datetime:
    """Instante aware UTC que corresponde a `h:mi` hora local de Bogotá el
    `y-mo-d` — para sembrar `promotion_evaluated_at` como lo haría el flujo QR
    al confirmar el carrito (que guarda `datetime.now(timezone.utc)` aware)
    para un pedido tomado a esa hora local."""
    return datetime(y, mo, d, h, mi, tzinfo=_BOGOTA).astimezone(timezone.utc)


class TestOrdersPaymentGate(unittest.TestCase):
    # ------------------------------------------------------------- Helpers

    def _seed_order_recibida(self, *, precio=PRECIO, stock=Decimal("1000"), autoflush=True):
        """spec 026: siempre siembra receta+inventario con stock amplio, para
        que confirmar el pago (que ahora también descuenta inventario) no
        falle por falta de receta en los tests que no están probando
        justamente eso (ver `_seed_order_recibida_sin_receta` más abajo).

        `autoflush=False` reproduce la sesión real de producción (spec 028):
        ver `test_confirm_cash_y_approve_funcionan_con_autoflush_false`."""
        db = fx.new_session(autoflush=autoflush)
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        participant = fx.make_participant(db, table_session=ts)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=precio)
        insumo = fx.make_inventory_item(db, current_stock=stock)
        fx.make_recipe_item(db, variant, insumo, quantity=Decimal("1"))
        order = fx.make_customer_order(db, ts, participant=participant, status="recibida")
        fx.make_order_item(db, order, variant, estado_cocina="pendiente")
        db.commit()
        return db, order

    def _user(self):
        return fx.make_user_double()

    def _shift(self, db):
        """Spec 028: aprobar/confirmar ya factura en la misma llamada, así
        que necesita un turno de caja abierto (mismo requisito que
        `pay_order`/`checkout_and_send`)."""
        return fx.make_cash_shift(db)

    # ------------------------------------------------- approve/reject (US2)

    def test_approve_confirma_intento_de_transferencia(self):
        """Acceptance Scenario 4 (US2)."""
        db, order = self._seed_order_recibida()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        attempt = fx.make_payment_attempt(
            db, order, nequi, status="pendiente", receipt_file_url="https://example.invalid/a.jpg"
        )
        db.commit()

        result = checkout.approve_payment_attempt(db, attempt.id, self._shift(db).id, self._user())
        self.assertEqual(result.status, "confirmado")
        self.assertIsNotNone(result.resolved_at)
        # spec 028: aprobar ya genera la venta/factura en la misma llamada.
        db.refresh(order)
        sale = db.execute(select(Sale).where(Sale.customer_order_id == order.id)).scalar_one()
        self.assertEqual(sale.status, "paid")
        self.assertIsNotNone(sale.invoice)

    def test_approve_orden_delivery_suma_el_valor_del_domicilio_sin_fallar(self):
        """spec 056, research.md Decisión 5 — punto de mayor riesgo: el pago
        que este método autogenera para el intento de transferencia debe
        cubrir subtotal + domicilio, o `build_sale` lo rechazaría con 422 en
        cuanto su propio total (ya con el domicilio sumado) no coincida con
        lo pagado."""
        db, order = self._seed_order_recibida()
        order.order_type = "DELIVERY"
        order.delivery_fee = Decimal("6000")
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        attempt = fx.make_payment_attempt(
            db, order, nequi, status="pendiente", receipt_file_url="https://example.invalid/a.jpg"
        )
        db.commit()

        result = checkout.approve_payment_attempt(db, attempt.id, self._shift(db).id, self._user())
        self.assertEqual(result.status, "confirmado")
        db.refresh(order)
        sale = db.execute(select(Sale).where(Sale.customer_order_id == order.id)).scalar_one()
        self.assertEqual(sale.delivery_fee, Decimal("6000"))
        self.assertEqual(sale.total, PRECIO + Decimal("6000"))

    def test_approve_409_sin_comprobante_todavia(self):
        db, order = self._seed_order_recibida()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        attempt = fx.make_payment_attempt(db, order, nequi, status="pendiente")
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            checkout.approve_payment_attempt(db, attempt.id, self._shift(db).id, self._user())
        self.assertEqual(ctx.exception.status_code, 409)

    def test_reject_exige_motivo_y_lo_registra(self):
        """FR-014, Acceptance Scenario 6 (US2)."""
        db, order = self._seed_order_recibida()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        attempt = fx.make_payment_attempt(
            db, order, nequi, status="pendiente", receipt_file_url="https://example.invalid/a.jpg"
        )
        db.commit()

        result = checkout.reject_payment_attempt(
            db, attempt.id, "el monto no coincide", self._user()
        )
        self.assertEqual(result.status, "rechazado")
        self.assertEqual(result.rejection_reason, "el monto no coincide")

    def test_approve_o_reject_de_metodo_efectivo_falla_409(self):
        db, order = self._seed_order_recibida()
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        attempt = fx.make_payment_attempt(db, order, efectivo, status="pendiente")
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            checkout.approve_payment_attempt(db, attempt.id, self._shift(db).id, self._user())
        self.assertEqual(ctx.exception.status_code, 409)

    # ------------------------------------------------- confirm-cash (US3)

    def test_confirm_cash_calcula_cambio(self):
        """Acceptance Scenario 1 (US3): $18.000 de total, $20.000 recibidos →
        $2.000 de cambio."""
        db, order = self._seed_order_recibida(precio=PRECIO)
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        attempt = fx.make_payment_attempt(db, order, efectivo, status="pendiente")
        db.commit()

        result = checkout.confirm_cash_payment_attempt(
            db, attempt.id, Decimal("20000"), self._shift(db).id, self._user()
        )
        self.assertEqual(result.status, "confirmado")
        self.assertEqual(result.change_amount, Decimal("2000"))
        # spec 028: confirmar ya genera la venta/factura en la misma llamada
        # — mismo cambio calculado en ambos lados (Sale.change_given y
        # attempt.change_amount coinciden por construcción).
        db.refresh(order)
        sale = db.execute(select(Sale).where(Sale.customer_order_id == order.id)).scalar_one()
        self.assertEqual(sale.status, "paid")
        self.assertEqual(sale.change_given, Decimal("2000"))
        self.assertIsNotNone(sale.invoice)

    def test_confirm_cash_orden_delivery_exige_cubrir_domicilio_y_lo_suma_al_total(self):
        """spec 056: el chequeo previo FR-010a y `build_sale` deben coincidir en
        incluir el valor del domicilio. spec 073 US7 (research.md D13): el
        chequeo previo pasó de `_order_total` a `compute_checkout_preview(...)
        .total` — sin promoción el resultado es el mismo (`discount = 0`)."""
        db, order = self._seed_order_recibida(precio=PRECIO)
        order.order_type = "DELIVERY"
        order.delivery_fee = Decimal("6000")
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        attempt = fx.make_payment_attempt(db, order, efectivo, status="pendiente")
        db.commit()

        # Monto que cubre solo el subtotal (sin domicilio) debe rechazarse.
        with self.assertRaises(HTTPException) as ctx:
            checkout.confirm_cash_payment_attempt(
                db, attempt.id, PRECIO, self._shift(db).id, self._user()
            )
        self.assertEqual(ctx.exception.status_code, 422)

        # Monto que sí cubre subtotal + domicilio se confirma correctamente.
        result = checkout.confirm_cash_payment_attempt(
            db, attempt.id, PRECIO + Decimal("6000"), self._shift(db).id, self._user()
        )
        self.assertEqual(result.status, "confirmado")
        db.refresh(order)
        sale = db.execute(select(Sale).where(Sale.customer_order_id == order.id)).scalar_one()
        self.assertEqual(sale.delivery_fee, Decimal("6000"))
        self.assertEqual(sale.total, PRECIO + Decimal("6000"))

    def test_confirm_cash_monto_exacto_cambio_cero(self):
        """Acceptance Scenario 2 (US3)."""
        db, order = self._seed_order_recibida(precio=PRECIO)
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        attempt = fx.make_payment_attempt(db, order, efectivo, status="pendiente")
        db.commit()

        result = checkout.confirm_cash_payment_attempt(
            db, attempt.id, Decimal("18000"), self._shift(db).id, self._user()
        )
        self.assertEqual(result.change_amount, Decimal("0"))

    def test_confirm_cash_monto_insuficiente_falla_422(self):
        """FR-010a, Acceptance Scenario 4 (US3): el sistema impide confirmar
        con un monto recibido menor al total."""
        db, order = self._seed_order_recibida(precio=PRECIO)
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        attempt = fx.make_payment_attempt(db, order, efectivo, status="pendiente")
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            checkout.confirm_cash_payment_attempt(
                db, attempt.id, Decimal("15000"), self._shift(db).id, self._user()
            )
        self.assertEqual(ctx.exception.status_code, 422)
        # No quedó confirmado ni con cambio calculado.
        db.refresh(attempt)
        self.assertEqual(attempt.status, "pendiente")
        self.assertIsNone(attempt.change_amount)

    def test_confirm_cash_de_metodo_transferencia_falla_409(self):
        db, order = self._seed_order_recibida()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        attempt = fx.make_payment_attempt(db, order, nequi, status="pendiente")
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            checkout.confirm_cash_payment_attempt(
            db, attempt.id, Decimal("20000"), self._shift(db).id, self._user()
        )
        self.assertEqual(ctx.exception.status_code, 409)

    # ------------------------------------------------- FR-018/SC-007: doble resolución

    def test_segunda_resolucion_del_mismo_intento_falla_409(self):
        """FR-018/SC-007: ante dos resoluciones casi simultáneas del mismo
        intento, solo la primera surte efecto. Reforzado por spec 026,
        FR-001: tampoco duplica el envío a cocina (un solo movimiento de
        inventario)."""
        db, order = self._seed_order_recibida()
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        attempt = fx.make_payment_attempt(db, order, efectivo, status="pendiente")
        db.commit()

        checkout.confirm_cash_payment_attempt(
            db, attempt.id, Decimal("20000"), self._shift(db).id, self._user()
        )
        with self.assertRaises(HTTPException) as ctx:
            checkout.confirm_cash_payment_attempt(
            db, attempt.id, Decimal("20000"), self._shift(db).id, self._user()
        )
        self.assertEqual(ctx.exception.status_code, 409)

        movimientos = db.execute(
            select(InventoryMovement).where(InventoryMovement.reference_id == order.id)
        ).scalars().all()
        self.assertEqual(len(movimientos), 1)

    # ------------------------------------------------- confirm_order gate (US4)

    def test_confirm_order_409_con_intento_pendiente(self):
        """Acceptance Scenario 1 (US4)."""
        db, order = self._seed_order_recibida()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        fx.make_payment_attempt(db, order, nequi, status="pendiente")
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            checkout.confirm_order(db, order.id, self._user())
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("pago confirmado", str(ctx.exception.detail))

    def test_confirm_order_409_con_intento_rechazado(self):
        """Acceptance Scenario 2 (US4): un rechazo no habilita el paso a
        comanda, aunque el comensal ya lo haya visto."""
        db, order = self._seed_order_recibida()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        fx.make_payment_attempt(
            db, order, nequi, status="rechazado", rejection_reason="no coincide"
        )
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            checkout.confirm_order(db, order.id, self._user())
        self.assertEqual(ctx.exception.status_code, 409)

    def test_confirm_order_ok_con_intento_confirmado(self):
        """Acceptance Scenario 3 (US4), actualizado por spec 026 FR-001 y
        spec 035 (A-52): con un intento de pago recién confirmado en
        efectivo, la orden queda 'pagada' (ya con su venta) en la MISMA
        llamada a `confirm_cash_payment_attempt` — ya no hace falta una
        segunda llamada manual a `confirm_order` (research.md spec 026,
        Decisión 1). `confirm_order` sigue expuesto como vía de recuperación
        (Decisión 2), pero sobre una orden que ya está 'pagada' no tiene nada
        que hacer."""
        db, order = self._seed_order_recibida()
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        attempt = fx.make_payment_attempt(db, order, efectivo, status="pendiente")
        db.commit()

        result = checkout.confirm_cash_payment_attempt(
            db, attempt.id, Decimal("20000"), self._shift(db).id, self._user()
        )
        self.assertEqual(result.status, "confirmado")
        db.refresh(order)
        self.assertEqual(order.status, "pagada")

        with self.assertRaises(HTTPException) as ctx:
            checkout.confirm_order(db, order.id, self._user())
        self.assertEqual(ctx.exception.status_code, 409)

    def test_confirm_order_dos_veces_seguidas_la_segunda_no_tiene_efecto(self):
        """Acceptance Scenario 4 (US4): `confirm_order`, llamado directamente
        (vía de recuperación, spec 026 research.md Decisión 2) sobre una
        orden con un intento ya confirmado, sigue siendo idempotente — solo
        la primera llamada tiene efecto."""
        db, order = self._seed_order_recibida()
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        fx.make_payment_attempt(db, order, efectivo, status="confirmado")
        db.commit()

        result = checkout.confirm_order(db, order.id, self._user())
        self.assertEqual(result.status, "abierta")

        with self.assertRaises(HTTPException) as ctx:
            checkout.confirm_order(db, order.id, self._user())
        self.assertEqual(ctx.exception.status_code, 409)

    # ------------------------------------------------- Fusión pago→cocina (spec 026, FR-001/FR-002)

    def test_confirm_cash_envia_a_cocina_en_la_misma_llamada(self):
        """spec 026, FR-001 (US1, escenario 1), estado ampliado por spec 035
        (A-52): confirmar el efectivo descuenta inventario y deja la orden
        'pagada' (ya con su venta) en la misma llamada, sin ninguna acción
        manual adicional."""
        db, order = self._seed_order_recibida()
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        attempt = fx.make_payment_attempt(db, order, efectivo, status="pendiente")
        db.commit()

        checkout.confirm_cash_payment_attempt(
            db, attempt.id, Decimal("20000"), self._shift(db).id, self._user()
        )

        db.refresh(order)
        self.assertEqual(order.status, "pagada")
        movimientos = db.execute(
            select(InventoryMovement).where(InventoryMovement.reference_id == order.id)
        ).scalars().all()
        self.assertEqual(len(movimientos), 1)

    def test_approve_envia_a_cocina_en_la_misma_llamada(self):
        """spec 026, FR-001 (US1, escenario 2), estado ampliado por spec 035
        (A-52): aprobar un comprobante de transferencia descuenta inventario
        y deja la orden 'pagada' (ya con su venta) en la misma llamada."""
        db, order = self._seed_order_recibida()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        attempt = fx.make_payment_attempt(
            db, order, nequi, status="pendiente", receipt_file_url="https://example.invalid/a.jpg"
        )
        db.commit()

        checkout.approve_payment_attempt(db, attempt.id, self._shift(db).id, self._user())

        db.refresh(order)
        self.assertEqual(order.status, "pagada")
        movimientos = db.execute(
            select(InventoryMovement).where(InventoryMovement.reference_id == order.id)
        ).scalars().all()
        self.assertEqual(len(movimientos), 1)

    def test_confirm_cash_y_approve_funcionan_con_autoflush_false(self):
        """Regresión (spec 028): la sesión real de producción
        (`app/core/db.py::with_db`) usa `autoflush=False`, a diferencia del
        default de esta suite (`fx.new_session()`, `autoflush=True`) — que
        por eso nunca detectó que `confirm_cash_payment_attempt` y
        `approve_payment_attempt` mutaban `attempt.status = "confirmado"` y
        llamaban a `_confirm_order_impl` sin un `db.flush()` de por medio: su
        chequeo `has_confirmed_payment` es una `SELECT` fresca que, sin
        flush, no ve el `UPDATE` todavía pendiente y siempre rechazaba con
        409 "La orden no tiene un pago confirmado" — en producción, **cada
        intento de confirmar un pago fallaba**. Este test reproduce la
        configuración real de la sesión para que esa regresión no pueda
        volver a colarse sin que la suite la detecte."""
        db, order = self._seed_order_recibida(autoflush=False)
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        attempt = fx.make_payment_attempt(db, order, efectivo, status="pendiente")
        db.commit()

        result = checkout.confirm_cash_payment_attempt(
            db, attempt.id, Decimal("20000"), self._shift(db).id, self._user()
        )
        self.assertEqual(result.status, "confirmado")
        self.assertEqual(result.change_amount, Decimal("2000"))
        db.refresh(order)
        self.assertEqual(order.status, "pagada")

        db2, order2 = self._seed_order_recibida(autoflush=False)
        nequi = fx.make_payment_method(db2, name="Nequi", is_cash=False, type="transfer")
        attempt2 = fx.make_payment_attempt(
            db2, order2, nequi, status="pendiente", receipt_file_url="https://example.invalid/a.jpg"
        )
        db2.commit()

        result2 = checkout.approve_payment_attempt(db2, attempt2.id, self._shift(db2).id, self._user())
        self.assertEqual(result2.status, "confirmado")
        db2.refresh(order2)
        self.assertEqual(order2.status, "pagada")

    def test_confirm_cash_stock_insuficiente_no_confirma_el_pago(self):
        """spec 026, FR-002 (US1, escenario 3): si el descuento automático de
        inventario falla por falta de stock, el intento de pago NO queda
        confirmado y la orden NO avanza — ninguna de las dos cosas ocurre a
        medias (research.md Decisión 1: un solo `commit`/`rollback` cubre
        ambos cambios)."""
        db, order = self._seed_order_recibida(stock=Decimal("0"))
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        attempt = fx.make_payment_attempt(db, order, efectivo, status="pendiente")
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            checkout.confirm_cash_payment_attempt(
            db, attempt.id, Decimal("20000"), self._shift(db).id, self._user()
        )
        self.assertEqual(ctx.exception.status_code, 400)

        db.refresh(attempt)
        db.refresh(order)
        self.assertEqual(attempt.status, "pendiente")
        self.assertIsNone(attempt.change_amount)
        self.assertEqual(order.status, "recibida")
        movimientos = db.execute(
            select(InventoryMovement).where(InventoryMovement.reference_id == order.id)
        ).scalars().all()
        self.assertEqual(len(movimientos), 0)

    # ------------------------------------------------- Historial (FR-016)

    def test_list_payment_attempts_incluye_rechazados_y_motivo(self):
        """Acceptance Scenario 3 (US5): el historial completo, con el motivo
        del rechazo, visible para el cajero."""
        db, order = self._seed_order_recibida()
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        fx.make_payment_attempt(
            db, order, nequi, status="rechazado", rejection_reason="el monto no coincide",
        )
        fx.make_payment_attempt(db, order, nequi, status="pendiente")
        db.commit()

        attempts = checkout.list_payment_attempts(db, order.id)
        self.assertEqual(len(attempts), 2)
        rechazado = next(a for a in attempts if a.status == "rechazado")
        self.assertEqual(rechazado.rejection_reason, "el monto no coincide")

    # ---------------------------------- Orden creada por spec 025 (T020)

    def test_approve_y_reintento_tras_rechazo_sobre_orden_de_submit_cart(self):
        """spec 025-revision-pago-antes-envio, Acceptance Scenario 4 (US3):
        una orden creada por `cart.service.submit_cart(..., receipt_file_url=...)`
        (en vez de sembrada directamente con `fx.make_customer_order` +
        `fx.make_payment_attempt`, como el resto de este módulo) sigue
        pudiendo resolverse con `checkout.approve_payment_attempt`/
        `reject_payment_attempt`, sin cambios — incluido el reintento tras
        rechazo (`POST /cart/orders/{order_id}/payment-attempts`, spec 024
        Historia 5, sin cambios en esta spec)."""
        from app.api.v1.cart import service as cart_service

        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        participant = fx.make_participant(db, table_session=ts)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        insumo = fx.make_inventory_item(db, current_stock=Decimal("1000"))
        fx.make_recipe_item(db, variant, insumo, quantity=Decimal("2"))
        cart = fx.make_cart(db, participant=participant)
        fx.make_cart_item(db, cart, variant)
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        db.commit()

        # spec 088 (A-93): el comprobante es una key existente de la carpeta `comprobantes` del
        # negocio (R2 simulado); en base de datos se guarda la key.
        with mock.patch("app.core.asset_refs.object_exists", return_value=True):
            order = cart_service.submit_cart(
                db, participant, nequi.id,
                receipt_file_url="tenant_test/comprobantes/a1b2c3d4e5f60718293a4b5c6d7e8f90.jpg",
                tenant_schema="tenant_test",
            )
        first_attempt_id = order.current_payment_attempt.id

        rejected = checkout.reject_payment_attempt(
            db, first_attempt_id, "el monto no coincide", self._user()
        )
        self.assertEqual(rejected.status, "rechazado")

        # Reintento tras rechazo: crea un intento nuevo sobre la misma orden
        # (spec 024, Historia 5) — endpoint separado, sin tocar en esta spec.
        second = cart_service.create_payment_attempt(db, participant.id, order.id, nequi.id)
        presign = cart_service.presign_receipt(
            db, "tenant_test", participant.id, second.id, "image/jpeg"
        )
        with mock.patch("app.core.asset_refs.object_exists", return_value=True):
            cart_service.attach_receipt(
                db, participant.id, second.id, presign.public_url, tenant_schema="tenant_test"
            )

        approved = checkout.approve_payment_attempt(db, second.id, self._shift(db).id, self._user())
        self.assertEqual(approved.status, "confirmado")

        # spec 026, FR-001 + spec 035 (A-52): aprobar ya deja la orden
        # 'pagada' (con su venta) en la misma llamada — no hace falta una
        # segunda llamada manual a confirm_order.
        db.refresh(order)
        self.assertEqual(order.status, "pagada")


class TestConfirmCashChequeoPrevioConPromocion073(unittest.TestCase):
    """spec 073, US7 (FR-021 a FR-024, A-70, research.md D13): el chequeo previo
    del "monto recibido" de `confirm_cash_payment_attempt` compara contra el
    `Total` autoritativo (subtotal − descuento por promoción + domicilio,
    instante congelado) que devuelve `compute_checkout_preview`, no contra la
    suma sin descuento de la ya eliminada `_order_total`.

    No es characterization: es la corrección de un defecto — se verifica contra
    spec.md Historia 7 / contracts/revision-pago-cajero-qr.md."""

    CONO = Decimal("8000")

    def _user(self):
        return fx.make_user_double()

    def _shift(self, db):
        return fx.make_cash_shift(db)

    def _seed_qr_order_con_promo(
        self, *, promo_evaluated_at=None, start_time=None, end_time=None,
        promo_status="active", qty=2, delivery_fee=None,
    ):
        """Pedido de comensal por QR en `recibida` con 2 conos a $8.000 y una
        promoción del 50% llevando 2 sobre esa variante — más un intento de pago
        en efectivo pendiente, listo para `confirm_cash_payment_attempt`."""
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        participant = fx.make_participant(db, table_session=ts)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=self.CONO)
        insumo = fx.make_inventory_item(db, current_stock=Decimal("1000"))
        fx.make_recipe_item(db, variant, insumo, quantity=Decimal("1"))
        order_kw = dict(status="recibida", channel="QR_MENU", promotion_evaluated_at=promo_evaluated_at)
        if delivery_fee is not None:
            order_kw.update(order_type="DELIVERY", delivery_fee=delivery_fee)
        order = fx.make_customer_order(db, ts, participant=participant, **order_kw)
        fx.make_order_item(db, order, variant, quantity=qty, estado_cocina="pendiente")
        promo = fx.make_promotion(
            db, status=promo_status, start_time=start_time, end_time=end_time,
        )
        fx.add_rule_to_promotion(
            db, promo, type="percent", value=Decimal("50"), min_qty=2, variants=[variant],
        )
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        attempt = fx.make_payment_attempt(db, order, efectivo, status="pendiente")
        db.commit()
        return dict(db=db, order=order, promo=promo, attempt=attempt, variant=variant)

    def test_scenario3_monto_exacto_con_descuento_confirma_sin_422_y_cambio_cero(self):
        """Historia 7, Scenario 3: 2 conos + promoción del 50% llevando 2 →
        `confirm_cash_payment_attempt` con $8.000 confirma al primer intento,
        `change_amount = 0`, sin el 422 "monto menor al total"."""
        s = self._seed_qr_order_con_promo()
        db, attempt = s["db"], s["attempt"]

        result = checkout.confirm_cash_payment_attempt(
            db, attempt.id, Decimal("8000"), self._shift(db).id, self._user(),
        )

        self.assertEqual(result.status, "confirmado")
        self.assertEqual(result.change_amount, Decimal("0"))
        sale = db.execute(
            select(Sale).where(Sale.customer_order_id == s["order"].id)
        ).scalar_one()
        self.assertEqual(sale.total, Decimal("8000"))
        self.assertEqual(sale.discount, Decimal("8000"))
        self.assertEqual(sale.change_given, Decimal("0"))

    def test_scenario2_diez_mil_recibidos_cambio_dos_mil_sobre_ocho_mil(self):
        """Historia 7, Scenario 2: $10.000 recibidos → cambio $2.000 calculado
        sobre $8.000 (no sobre $16.000), venta por $8.000."""
        s = self._seed_qr_order_con_promo()
        db, attempt = s["db"], s["attempt"]

        result = checkout.confirm_cash_payment_attempt(
            db, attempt.id, Decimal("10000"), self._shift(db).id, self._user(),
        )

        self.assertEqual(result.change_amount, Decimal("2000"))
        sale = db.execute(
            select(Sale).where(Sale.customer_order_id == s["order"].id)
        ).scalar_one()
        self.assertEqual(sale.total, Decimal("8000"))
        self.assertEqual(sale.change_given, Decimal("2000"))

    def test_scenario4_monto_insuficiente_422_cita_el_total_real_no_el_inflado(self):
        """Historia 7, Scenario 4: $5.000 no cubre el `Total` real $8.000 → 422
        cuyo total citado es 8000 (faltan $3.000), NUNCA 16000."""
        s = self._seed_qr_order_con_promo()
        db, attempt = s["db"], s["attempt"]

        with self.assertRaises(HTTPException) as ctx:
            checkout.confirm_cash_payment_attempt(
                db, attempt.id, Decimal("5000"), self._shift(db).id, self._user(),
            )

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("8000", str(ctx.exception.detail))
        self.assertNotIn("16000", str(ctx.exception.detail))
        db.refresh(attempt)
        self.assertEqual(attempt.status, "pendiente")

    def test_pedido_qr_delivery_con_promocion_compara_contra_subtotal_menos_descuento_mas_domicilio(self):
        """FR-021/FR-023: pedido QR a domicilio con promoción + envío $5.000 →
        el chequeo previo compara contra `16000 − 8000 + 5000 = 13000`."""
        s = self._seed_qr_order_con_promo(delivery_fee=Decimal("5000"))
        db, attempt = s["db"], s["attempt"]

        with self.assertRaises(HTTPException) as ctx:
            checkout.confirm_cash_payment_attempt(
                db, attempt.id, Decimal("12000"), self._shift(db).id, self._user(),
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("13000", str(ctx.exception.detail))

        result = checkout.confirm_cash_payment_attempt(
            db, attempt.id, Decimal("13000"), self._shift(db).id, self._user(),
        )
        self.assertEqual(result.status, "confirmado")
        sale = db.execute(
            select(Sale).where(Sale.customer_order_id == s["order"].id)
        ).scalar_one()
        self.assertEqual(sale.total, Decimal("13000"))

    def test_scenario6_instante_congelado_del_flujo_qr_conserva_el_descuento_tras_vencer_la_franja(self):
        """Historia 7, Scenario 6: pedido QR creado 19:59 dentro de una
        promoción vigente hasta las 20:00; el pago se confirma después (hora de
        pared real del test), pero la vigencia se evalúa contra el instante
        congelado → descuento aplicado, `Total $8.000` (T027 congela el instante
        del flujo QR)."""
        s = self._seed_qr_order_con_promo(
            promo_evaluated_at=_utc_para_hora_local(2026, 9, 2, 19, 59),
            start_time=time(18, 0), end_time=time(20, 0),
        )
        db, attempt = s["db"], s["attempt"]

        result = checkout.confirm_cash_payment_attempt(
            db, attempt.id, Decimal("8000"), self._shift(db).id, self._user(),
        )

        self.assertEqual(result.status, "confirmado")
        sale = db.execute(
            select(Sale).where(Sale.customer_order_id == s["order"].id)
        ).scalar_one()
        self.assertEqual(sale.total, Decimal("8000"))
        self.assertEqual(sale.discount, Decimal("8000"))

    def test_scenario7_promocion_pausada_estado_vivo_total_sube_a_16000(self):
        """Historia 7, Scenario 7 / FR-009a: la promoción se pausó entre el
        pedido y el cobro → `compute_checkout_preview(...).total` devuelve
        $16.000 (estado leído vivo, el instante congelado NO lo evita) y el
        chequeo previo valida contra ese valor: $10.000 → 422 citando 16000,
        $16.000 → confirma."""
        s = self._seed_qr_order_con_promo(promo_status="paused")
        db, attempt = s["db"], s["attempt"]

        self.assertEqual(
            checkout.compute_checkout_preview(db, s["order"].id).total, Decimal("16000"),
        )
        with self.assertRaises(HTTPException) as ctx:
            checkout.confirm_cash_payment_attempt(
                db, attempt.id, Decimal("10000"), self._shift(db).id, self._user(),
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("16000", str(ctx.exception.detail))

        result = checkout.confirm_cash_payment_attempt(
            db, attempt.id, Decimal("16000"), self._shift(db).id, self._user(),
        )
        self.assertEqual(result.status, "confirmado")
        sale = db.execute(
            select(Sale).where(Sale.customer_order_id == s["order"].id)
        ).scalar_one()
        self.assertEqual(sale.total, Decimal("16000"))
        self.assertEqual(sale.discount, Decimal("0"))

    def test_sin_promocion_el_chequeo_previo_sigue_igual_discount_cero(self):
        """No regresión: un pedido QR sin ninguna promoción vigente confirma
        exactamente como antes del cambio de `_order_total` (`discount = 0`)."""
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        participant = fx.make_participant(db, table_session=ts)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=self.CONO)
        insumo = fx.make_inventory_item(db, current_stock=Decimal("1000"))
        fx.make_recipe_item(db, variant, insumo, quantity=Decimal("1"))
        order = fx.make_customer_order(db, ts, participant=participant, status="recibida", channel="QR_MENU")
        fx.make_order_item(db, order, variant, quantity=2, estado_cocina="pendiente")
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        attempt = fx.make_payment_attempt(db, order, efectivo, status="pendiente")
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            checkout.confirm_cash_payment_attempt(
                db, attempt.id, Decimal("15000"), self._shift(db).id, self._user(),
            )
        self.assertEqual(ctx.exception.status_code, 422)

        result = checkout.confirm_cash_payment_attempt(
            db, attempt.id, Decimal("16000"), self._shift(db).id, self._user(),
        )
        self.assertEqual(result.status, "confirmado")
        self.assertEqual(result.change_amount, Decimal("0"))


class TestConfirmCashConAdicionesSucesivasPorCanalesDistintos118(unittest.TestCase):
    """spec 118 (research.md D5, FR-004, Acceptance Scenario 4 de Historia 1,
    Edge Case de canales mixtos): un pedido QR con 1 unidad recibe una
    segunda unidad de la misma presentación vía `add_item_to_order` (mesero)
    antes de que `confirm_cash_payment_attempt` se llame -- el monto
    validado ya incluye el descuento combinado.

    No hay cambio de código en `confirm_cash_payment_attempt`/
    `approve_payment_attempt`: ambos ya comparan contra
    `compute_checkout_preview`, que agrupa entre filas distintas sin importar
    por qué camino se creó cada una (research.md D5) -- esta es cobertura
    nueva, no una corrección."""

    PRECIO = Decimal("7000")

    def _user(self):
        return fx.make_user_double()

    def _shift(self, db):
        return fx.make_cash_shift(db)

    def test_segunda_unidad_agregada_por_el_mesero_ya_incluye_el_descuento_combinado(self):
        from app.api.v1.orders import consolidation
        from app.api.v1.orders.schemas import OrderItemIn

        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        participant = fx.make_participant(db, table_session=ts)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=self.PRECIO)
        insumo = fx.make_inventory_item(db, current_stock=Decimal("1000"))
        fx.make_recipe_item(db, variant, insumo, quantity=Decimal("1"))
        order = fx.make_customer_order(
            db, ts, participant=participant, status="recibida", channel="QR_MENU",
        )
        # 1 unidad pedida por el comensal vía el menú QR.
        fx.make_order_item(db, order, variant, quantity=1, estado_cocina="pendiente")
        promo = fx.make_promotion(db)
        fx.add_rule_to_promotion(
            db, promo, type="package_price", value=Decimal("12000"), min_qty=2,
            variants=[variant],
        )
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        attempt = fx.make_payment_attempt(db, order, efectivo, status="pendiente")
        db.commit()

        # El mesero anexa 1 unidad más de la misma presentación, por un
        # camino distinto (Terminal, no el menú QR del comensal) y en un
        # momento posterior, antes de que el cajero abra "Pagos por confirmar".
        order = consolidation.add_item_to_order(
            db, order.id, OrderItemIn(product_variant_id=variant.id, quantity=1), self._user(),
        )
        self.assertEqual(len(order.items), 2)

        result = checkout.confirm_cash_payment_attempt(
            db, attempt.id, Decimal("12000"), self._shift(db).id, self._user(),
        )

        self.assertEqual(result.status, "confirmado")
        self.assertEqual(result.change_amount, Decimal("0"))
        sale = db.execute(
            select(Sale).where(Sale.customer_order_id == order.id)
        ).scalar_one()
        self.assertEqual(sale.total, Decimal("12000"))
        self.assertEqual(sale.discount, Decimal("2000"))


if __name__ == "__main__":
    unittest.main()
