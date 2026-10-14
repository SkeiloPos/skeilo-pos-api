"""Las 3 funciones públicas de `app/api/v1/orders/kitchen.py`
(specs/017-caracterizacion-orders, Historia 3).

`transition_kitchen` sigue CONGELADA, independiente del status de pago de la
orden a propósito (el flujo QR/mostrador vigente cobra antes de que cocina
termine — bloquearla ahí rompería la operación diaria). `mark_order_ready` ya
validaba parcialmente (**A-16**). `void_item` **dejó de estar congelada
sobre un pedido ya pagado**: spec 029 (Historia 1, FR-005/006/007) cierra la
porción `ACCIDENTAL` de A-16 para esta función — anular ya no se permite una
vez existe un pago registrado (`status == "pagada"` o ya existe una `Sale`
asociada, ver `research.md` D3 de esa spec) — decisión de negocio explícita,
no un olvido corregido en silencio.

Documenta también que las transiciones internas de las tres están cerradas a
una lista blanca sin vía genérica de asignación libre (**A-25 [PROTEGIDA]**),
invariante que también verifica sobre las otras cuatro funciones públicas de
`checkout.py` que mutan estado (`block_order`, `confirm_order`, `pay_order`,
`cancel_order`).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_orders_kitchen -v
"""
from decimal import Decimal
import unittest

from fastapi import HTTPException

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.catalog.schemas import OptionSelectionIn
from app.api.v1.orders import checkout, kitchen
from app.api.v1.orders.schemas import BlockIn, CancelIn, KitchenTransitionIn, OrderItemIn, PayIn, VoidItemIn
from app.api.v1.sales.schemas import PaymentIn
from app.models.sale import Sale

PRECIO = Decimal("10000")


class TestKitchen(unittest.TestCase):
    # ------------------------------------------------------------- Helpers

    def _seed_item(self, *, order_status="abierta", estado_cocina="pendiente"):
        db = fx.new_session()
        ts = fx.make_table_session(db)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        insumo = fx.make_inventory_item(db, current_stock=Decimal("1000"))
        fx.make_recipe_item(db, variant, insumo, quantity=Decimal("1"))
        order = fx.make_customer_order(db, ts, status=order_status, channel="POS")
        item = fx.make_order_item(db, order, variant, estado_cocina=estado_cocina)
        db.commit()
        return dict(db=db, ts=ts, order=order, item=item, variant=variant, insumo=insumo)

    def _user(self):
        return fx.make_user_double()

    # ------------------------------------------------- transition_kitchen (T035)

    def test_transition_kitchen_salto_directo_y_retroceso_rechazado(self):
        """CONGELA comportamiento actual — A-25 [PROTEGIDA]
        (`kitchen.py:43-60`, spec.md Historia 3, escenario 1): un ítem
        'pendiente' salta directo a 'listo' (el botón de un toque de la
        terminal); un ítem 'listo' que intenta retroceder a 'pendiente'
        responde 409 — la lista blanca `_ALLOWED` solo permite avanzar."""
        s = self._seed_item(estado_cocina="pendiente")
        db, item = s["db"], s["item"]

        actualizado = kitchen.transition_kitchen(
            db, item.id, KitchenTransitionIn(estado_cocina="listo")
        )
        self.assertEqual(actualizado.estado_cocina, "listo")

        with self.assertRaises(HTTPException) as ctx:
            kitchen.transition_kitchen(db, item.id, KitchenTransitionIn(estado_cocina="pendiente"))
        self.assertEqual(ctx.exception.status_code, 409)

    # ----------------------------- transition_kitchen ignora orden (T036, sin cambios)

    def test_transition_kitchen_no_valida_status_de_la_orden_a16(self):
        """CONGELA comportamiento actual — A-16 (`kitchen.py:43-60`, spec.md
        Historia 3, escenario 2): orden ya 'pagada' con ítems aún 'pendiente'
        → `transition_kitchen` se ejecuta igual, sin ningún error por el
        status de la orden padre. Sin cambios por spec 029 — bloquear esto
        rompería el flujo QR/mostrador vigente, donde cocina sigue avanzando
        ítems después de que el pago ya se registró (ver docstring del
        módulo)."""
        s = self._seed_item(order_status="pagada", estado_cocina="pendiente")
        db, item = s["db"], s["item"]

        actualizado = kitchen.transition_kitchen(
            db, item.id, KitchenTransitionIn(estado_cocina="en_preparacion")
        )
        self.assertEqual(actualizado.estado_cocina, "en_preparacion")

    # ------------------------ void_item ya no anula un pedido pagado (spec 029)

    def test_void_item_409_si_orden_pagada_spec_029_a16(self):
        """Comportamiento NUEVO (spec 029, Historia 1, FR-005/006/007 —
        cierra la porción ACCIDENTAL de A-16 para esta función): orden ya
        'pagada' con un ítem aún 'pendiente' → `void_item` responde 409 en
        vez de anular, sin mutar el ítem ni registrar `OrderItemVoidLog`.
        Antes de esta spec se comportaba igual que `transition_kitchen`
        (test anterior) — ahora se distingue a propósito, porque anular es
        una reversa de mercancía ya entregada/cobrada, no una transición de
        cocina."""
        s = self._seed_item(order_status="pagada", estado_cocina="pendiente")
        db, item = s["db"], s["item"]
        user = self._user()

        with self.assertRaises(HTTPException) as ctx:
            kitchen.void_item(db, item.id, VoidItemIn(motivo="prueba"), user)
        self.assertEqual(ctx.exception.status_code, 409)
        db.refresh(item)
        self.assertEqual(item.estado_cocina, "pendiente")

    def test_void_item_409_si_orden_abierta_con_sale_camino_qr_mostrador_spec_029(self):
        """Comportamiento NUEVO (spec 029): el caso real que motivó la spec,
        distinto del anterior — `checkout_and_send`/`_confirm_order_impl`
        dejan la orden en `status="abierta"` a propósito, con la `Sale` ya
        emitida (nunca llega a `"pagada"`). `void_item` debe rechazar igual,
        reconociendo el pago por la `Sale` asociada, no por `status`."""
        s = self._seed_item(order_status="abierta", estado_cocina="listo")
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
        self.assertEqual(item.estado_cocina, "listo")

    # ---------------------------------------------- mark_order_ready valida (T037)

    def test_mark_order_ready_409_si_orden_pagada_contraste_a16(self):
        """CONGELA comportamiento actual — A-16 (`kitchen.py:63-90`, spec.md
        Historia 3, escenario 3): la misma orden 'pagada' con ítems
        'pendiente' → `mark_order_ready` sí responde 409 citando que la
        orden ya es terminal, a diferencia de `transition_kitchen`/
        `void_item` (T036)."""
        s = self._seed_item(order_status="pagada", estado_cocina="pendiente")
        db, order = s["db"], s["order"]

        with self.assertRaises(HTTPException) as ctx:
            kitchen.mark_order_ready(db, order.id)
        self.assertEqual(ctx.exception.status_code, 409)

    # ------------------------------------- mark_order_ready sí pasa en bloqueada (T038)

    def test_mark_order_ready_pasa_items_en_orden_bloqueada(self):
        """CONGELA comportamiento actual (`kitchen.py:63-90`, spec.md
        Historia 3, escenario 4): orden 'bloqueada' (no terminal de pago)
        con ítems en curso → `mark_order_ready` los pasa a 'listo' sin
        error — la validación bloquea solo 'pagada'/'cancelada', no
        'bloqueada' (la porción pendiente de A-16)."""
        s = self._seed_item(order_status="bloqueada", estado_cocina="en_preparacion")
        db, order = s["db"], s["order"]

        actualizada, cambiados = kitchen.mark_order_ready(db, order.id)
        self.assertEqual(len(cambiados), 1)
        self.assertTrue(all(it.estado_cocina == "listo" for it in cambiados))

    # ------------------------------------------------------------ void_item (T039)

    def test_void_item_anula_y_reemplaza_con_reversa_y_nuevo_descuento(self):
        """CONGELA comportamiento actual (`kitchen.py:93-176`, spec.md
        Historia 3, escenario 5): ítem 'pendiente' con `data.replacement`
        válido → el original queda 'anulado' (con reversa de inventario, por
        ser 'pendiente') y se crea uno nuevo 'pendiente' con `void_de`
        apuntando al original y su propio descuento de inventario."""
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
        self.assertEqual(original.estado_cocina, "anulado")

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

    # ----------------------------------- invariante A-25 [PROTEGIDA] de referencia (T040)

    def test_a25_protegida_ninguna_funcion_acepta_transicion_fuera_de_su_lista_blanca(self):
        """CONGELA comportamiento actual — A-25 [PROTEGIDA] (spec.md Historia
        3, escenario 6): de las siete funciones públicas de estos cinco
        ficheros que mutan `status`/`estado_cocina` (`block_order`,
        `confirm_order`, `pay_order`, `cancel_order`, `transition_kitchen`,
        `mark_order_ready`, `void_item`), cada una impone su propia
        transición validada — ninguna acepta una arbitraria sin pasar por su
        propia guarda. Este test es de referencia: reutiliza los casos ya
        congelados en test_orders_checkout.py (T022, T026, T028, T029) y
        arriba (T035, T037, T039), verificando el contraste directo aquí
        para las tres funciones de `kitchen.py`."""
        # transition_kitchen: 'anulado' no está en ningún valor de _ALLOWED.
        s = self._seed_item(estado_cocina="anulado")
        db, item = s["db"], s["item"]
        with self.assertRaises(HTTPException) as ctx:
            kitchen.transition_kitchen(db, item.id, KitchenTransitionIn(estado_cocina="listo"))
        self.assertEqual(ctx.exception.status_code, 409)

        # mark_order_ready: orden ya 'cancelada' es terminal.
        s2 = self._seed_item(order_status="cancelada", estado_cocina="pendiente")
        with self.assertRaises(HTTPException) as ctx2:
            kitchen.mark_order_ready(s2["db"], s2["order"].id)
        self.assertEqual(ctx2.exception.status_code, 409)

        # void_item: un ítem ya anulado no se puede volver a anular.
        s3 = self._seed_item(estado_cocina="anulado")
        with self.assertRaises(HTTPException) as ctx3:
            kitchen.void_item(s3["db"], s3["item"].id, VoidItemIn(motivo="x"), self._user())
        self.assertEqual(ctx3.exception.status_code, 409)

        # block_order: orden 'bloqueada' no puede volver a bloquearse.
        s4 = self._seed_item(order_status="bloqueada", estado_cocina="listo")
        with self.assertRaises(HTTPException) as ctx4:
            checkout.block_order(s4["db"], s4["order"].id, BlockIn(version=0))
        self.assertEqual(ctx4.exception.status_code, 409)

        # confirm_order: orden 'abierta' no está en 'recibida'.
        s5 = self._seed_item(order_status="abierta", estado_cocina="listo")
        with self.assertRaises(HTTPException) as ctx5:
            checkout.confirm_order(s5["db"], s5["order"].id, self._user())
        self.assertEqual(ctx5.exception.status_code, 409)

        # pay_order: orden 'abierta' no está 'bloqueada'.
        s6 = self._seed_item(order_status="abierta", estado_cocina="listo")
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

        # cancel_order: orden ya 'pagada' es terminal.
        s7 = self._seed_item(order_status="pagada", estado_cocina="listo")
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
        order = fx.make_customer_order(db, ts, status="abierta", channel="POS")
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
