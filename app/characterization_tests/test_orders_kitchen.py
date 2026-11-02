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
from decimal import Decimal
import unittest
from unittest import mock

from fastapi import HTTPException

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.catalog.schemas import OptionSelectionIn
from app.api.v1.orders import checkout, kitchen
from app.api.v1.orders.schemas import BlockIn, CancelIn, OrderItemIn, PayIn, VoidItemIn
from app.api.v1.sales.schemas import PaymentIn
from app.models.sale import Sale

PRECIO = Decimal("10000")


class TestKitchen(unittest.TestCase):
    # ------------------------------------------------------------- Helpers

    def _seed_item(self, *, order_status="pendiente", estado_cocina="pendiente", payment_status="UNPAID"):
        db = fx.new_session()
        ts = fx.make_table_session(db)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        insumo = fx.make_inventory_item(db, current_stock=Decimal("1000"))
        fx.make_recipe_item(db, variant, insumo, quantity=Decimal("1"))
        order = fx.make_customer_order(
            db, ts, status=order_status, payment_status=payment_status, channel="POS",
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


if __name__ == "__main__":
    unittest.main()
