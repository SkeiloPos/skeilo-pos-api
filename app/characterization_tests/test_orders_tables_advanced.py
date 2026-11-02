"""CONGELA comportamiento actual: las 4 funciones públicas de
`app/api/v1/orders/tables_advanced.py` (specs/017-caracterizacion-orders,
Historia 4) — mover una orden de mesa, fusionar mesas por grupo, y la cuenta
agrupada de un grupo fusionado.

Documenta los tres hallazgos de **A-26**:
  - RN-ORD-58: `move_order` exige la mesa destino completamente libre de
    órdenes activas, más estricto que el modelo general de varias órdenes
    por mesa.
  - RN-ORD-60: el manejador `except IntegrityError` de `move_order` está
    huérfano — el índice único que lo disparaba ya no existe en el modelo;
    se fuerza artificialmente con `orders_fixtures.force_flush_integrity_error`
    (Clarifications, sesión 2026-08-17).
  - RN-ORD-63: `merge_orders`, ante grupos preexistentes en colisión, resuelve
    el ganador con un `SELECT` sin `ORDER BY` — no determinista.

El camino C de **A-01** (`group_bill`) quedó corregido por
`specs/019-correccion-cuenta-mesas-fusionadas` (commit que cita esa decisión,
Constitución Principio II): excluye órdenes `pagada`/`cancelada` del cálculo y
aplica promociones/combos vigentes, igual que `table_sessions.compute_bill`
(camino A, referencia) — antes no filtraba por status ni aplicaba ningún
descuento.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_orders_tables_advanced -v
"""
from datetime import datetime, time, timezone
from decimal import Decimal
import unittest
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import checkout, tables_advanced
from app.api.v1.table_sessions import service as table_sessions_service

PRECIO = Decimal("10000")


class TestTablesAdvanced(unittest.TestCase):
    # ------------------------------------------------------------- Helpers

    def _seed_table_con_orden_activa(self, db=None, *, status="pendiente"):
        if db is None:
            db = fx.new_session()
        table = fx.make_dining_table(db, status="ocupada")
        ts = fx.make_table_session(db, table=table)
        order = fx.make_customer_order(db, ts, status=status)
        db.commit()
        return db, table, ts, order

    # -------------------------------------------------------- set_table_status (T042)

    def test_set_table_status_409_con_ordenes_activas_y_ok_sin_ellas(self):
        """CONGELA comportamiento actual (`tables_advanced.py:30-43`,
        spec.md Historia 4, escenario 5): mesa con al menos una orden activa
        → `new_status='libre'`/`'reservada'` responde 409; sin órdenes
        activas, el cambio se acepta."""
        db, table, ts, order = self._seed_table_con_orden_activa(status="pendiente")

        with self.assertRaises(HTTPException) as ctx:
            tables_advanced.set_table_status(db, table.id, "libre")
        self.assertEqual(ctx.exception.status_code, 409)

        order.status = "entregada"
        order.payment_status = "PAID"
        db.commit()

        result = tables_advanced.set_table_status(db, table.id, "libre")
        self.assertEqual(result.status, "libre")

    # ---------------------------------------------------------- move_order (T043)

    def test_move_order_409_si_mesa_destino_tiene_orden_activa_rn_ord_58(self):
        """CONGELA comportamiento actual — RN-ORD-58/A-26
        (`tables_advanced.move_order:45-73`, spec.md Historia 4, escenario
        1): mesa destino con una orden activa ya presente → `move_order`
        responde 409, más estricto que el modelo general de "varias órdenes
        por mesa"."""
        db, origen, ts_origen, order = self._seed_table_con_orden_activa(status="pendiente")
        _, destino, _, _ = self._seed_table_con_orden_activa(db, status="pendiente")

        with self.assertRaises(HTTPException) as ctx:
            tables_advanced.move_order(db, order.id, destino.id)
        self.assertEqual(ctx.exception.status_code, 409)

    # ----------------------------------- move_order IntegrityError forzado (T044)

    def test_move_order_integrity_error_forzado_sigue_traduciendo_a_409_rn_ord_60(self):
        """CONGELA comportamiento actual — RN-ORD-60/A-26
        (`tables_advanced.move_order:56-63`, spec.md Historia 4, escenario
        2): el índice único que originaba la colisión ya no existe en el
        modelo — ninguna secuencia de datos real dispara `IntegrityError`
        por sí sola. Se fuerza artificialmente (`db.flush` parcheado,
        Clarifications sesión 2026-08-17) y se confirma que el manejador
        `except IntegrityError` sigue presente y la traduce a 409."""
        db, origen, ts, order = self._seed_table_con_orden_activa(status="pendiente")
        destino = fx.make_dining_table(db, status="libre")
        db.commit()

        with fx.force_flush_integrity_error(db):
            with self.assertRaises(HTTPException) as ctx:
                tables_advanced.move_order(db, order.id, destino.id)
        self.assertEqual(ctx.exception.status_code, 409)

    # --------------------------------------------------------- merge_orders (T045)

    def test_merge_orders_no_determinista_entre_grupos_preexistentes_rn_ord_63(self):
        """CONGELA comportamiento actual — RN-ORD-63/A-26
        (`tables_advanced.merge_orders:75-89`, spec.md Historia 4, escenario
        3): dos órdenes que ya pertenecen a dos `merged_group_id` distintos
        preexistentes → el grupo resultante es uno de los dos (`SELECT` sin
        `ORDER BY`, no determinista) — se documenta la propiedad, sin fijar
        un valor específico (research.md §3)."""
        db, table_a, ts_a, order_a = self._seed_table_con_orden_activa(status="pendiente")
        db, table_b, ts_b, order_b = self._seed_table_con_orden_activa(db, status="pendiente")

        group_a = tables_advanced.merge_orders(db, [order_a.id])["merged_group_id"]

        db, table_c, ts_c, order_c = self._seed_table_con_orden_activa(db, status="pendiente")
        group_b = tables_advanced.merge_orders(db, [order_b.id, order_c.id])["merged_group_id"]

        self.assertNotEqual(group_a, group_b)

        resultado = tables_advanced.merge_orders(db, [order_a.id, order_b.id])
        self.assertIn(resultado["merged_group_id"], {group_a, group_b})

    # ----------------------------------------------------------- group_bill (T046)

    def test_group_bill_excluye_orden_pagada_del_total_historia_1_escenario_1(self):
        """CONGELA comportamiento corregido — A-01 camino C, Historia 1
        escenario 1 (FR-001): grupo con la orden A `pagada` ($20.000) y la
        orden B `abierta` ($15.000, sin promoción vigente) → el total
        devuelto es $15.000, la orden A queda excluida del cálculo pero
        sigue presente en `orders[]`."""
        db, table_a, ts_a, order_a = self._seed_table_con_orden_activa(status="pendiente")
        db, table_b, ts_b, order_b = self._seed_table_con_orden_activa(db, status="pendiente")

        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant_a = fx.make_variant(db, product=product, price=Decimal("20000"))
        variant_b = fx.make_variant(db, product=product, price=Decimal("15000"))
        fx.make_order_item(db, order_a, variant_a)
        fx.make_order_item(db, order_b, variant_b)

        group_id = tables_advanced.merge_orders(db, [order_a.id, order_b.id])["merged_group_id"]
        order_a.status = "entregada"
        order_a.payment_status = "PAID"
        db.commit()

        bill = tables_advanced.group_bill(db, group_id)

        order_ids = {o["order_id"] for o in bill["orders"]}
        self.assertEqual(order_ids, {order_a.id, order_b.id})
        self.assertEqual(bill["total"], Decimal("15000"))

    def test_group_bill_excluye_orden_cancelada_del_total_historia_1_escenario_2(self):
        """CONGELA comportamiento corregido — Historia 1 escenario 2
        (FR-001): una orden `cancelada` se excluye del total igual que una
        `pagada`, sin importar el `estado_cocina` de sus ítems (Edge Case de
        `spec.md`: el filtro por `status` de la orden tiene prioridad)."""
        db, table_a, ts_a, order_a = self._seed_table_con_orden_activa(status="pendiente")
        db, table_b, ts_b, order_b = self._seed_table_con_orden_activa(db, status="pendiente")

        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        fx.make_order_item(db, order_a, variant)
        fx.make_order_item(db, order_b, variant)

        group_id = tables_advanced.merge_orders(db, [order_a.id, order_b.id])["merged_group_id"]
        order_a.status = "cancelada"
        db.commit()

        bill = tables_advanced.group_bill(db, group_id)
        self.assertEqual(bill["total"], PRECIO)

    def test_group_bill_todas_terminales_da_total_cero_sin_error(self):
        """CONGELA comportamiento corregido — Edge Case de `spec.md`: si
        **todas** las órdenes del grupo están `pagada`/`cancelada`, el total
        es $0 (grupo sin nada pendiente de cobro), no un error — mismo
        comportamiento que ya tiene `table_sessions.compute_bill`."""
        db, table_a, ts_a, order_a = self._seed_table_con_orden_activa(status="pendiente")
        db, table_b, ts_b, order_b = self._seed_table_con_orden_activa(db, status="pendiente")

        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        fx.make_order_item(db, order_a, variant)
        fx.make_order_item(db, order_b, variant)

        group_id = tables_advanced.merge_orders(db, [order_a.id, order_b.id])["merged_group_id"]
        order_a.status = "entregada"
        order_a.payment_status = "PAID"
        order_b.status = "cancelada"
        db.commit()

        bill = tables_advanced.group_bill(db, group_id)
        self.assertEqual(bill["total"], Decimal("0"))

    def test_group_bill_aplica_promocion_percent_vigente_sin_terminales_historia_2_escenario_1(self):
        """CONGELA comportamiento corregido — Historia 2 escenario 1
        (FR-002): grupo con una sola orden `abierta` ($15.000 brutos) y una
        promoción `percent` del 10% vigente sobre su categoría, sin ninguna
        orden `pagada`/`cancelada` en el grupo → el total ya descuenta la
        promoción."""
        db, table_b, ts_b, order_b = self._seed_table_con_orden_activa(status="pendiente")

        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=Decimal("15000"))
        fx.make_order_item(db, order_b, variant)

        promo = fx.make_promotion(db, status="active")
        fx.add_rule_to_promotion(
            db, promo, type="percent", value=Decimal("10"), min_qty=1, variants=[variant],
        )

        group_id = tables_advanced.merge_orders(db, [order_b.id])["merged_group_id"]

        bill = tables_advanced.group_bill(db, group_id)
        self.assertEqual(bill["total"], Decimal("13500"))

    # spec 063 (FR-024, A-61): `test_group_bill_aplica_combo_vigente_sin_terminales_fr_002`
    # se elimina — el tipo `combo` y su mecanismo de selección se retiran.

    def test_group_bill_excluye_items_anulados_de_orden_billable_fr_003(self):
        """CONGELA comportamiento — FR-003 (sin cambio, se conserva): un
        ítem individual `anulado` sigue excluido del subtotal de su orden
        aunque la orden esté `abierta`, heredado de
        `checkout.order_sale_lines` — re-verificado a nivel de `group_bill`
        (gap de cobertura detectado en /speckit-analyze, G2: ninguna prueba
        de esta suite ejercitaba un ítem `anulado` en `group_bill`)."""
        db, table_b, ts_b, order_b = self._seed_table_con_orden_activa(status="pendiente")

        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        fx.make_order_item(db, order_b, variant)
        fx.make_order_item(db, order_b, variant, estado_cocina="cancelado")

        group_id = tables_advanced.merge_orders(db, [order_b.id])["merged_group_id"]

        bill = tables_advanced.group_bill(db, group_id)
        self.assertEqual(bill["total"], PRECIO)

    def test_group_bill_a01_camino_c_excluye_pagadas_y_aplica_promocion_vigente(self):
        """CONGELA comportamiento corregido — A-01 camino C
        (`tables_advanced.group_bill`, Historia 2 escenario 2 / SC-005):
        corrige `RN-ORD-64` [DUDOSA] tal como autoriza la entrada A-01,
        "Tratamiento acordado" (2026-08-15/16), de
        `registro-de-anomalias.md` — el commit que toque este test cita esa
        decisión (Constitución, Principio II). Grupo con la orden A `pagada`
        ($20.000) y la orden B `abierta` ($15.000 brutos, 10% de descuento
        vigente sobre su categoría): antes devolvía $35.000 (todo incluido,
        sin descuento); ahora devuelve $13.500 — ambos defectos corregidos a
        la vez (ejemplo de
        `contradiccion-06-cuenta-a-cobrar-tres-implementaciones.md §3`)."""
        db, table_a, ts_a, order_a = self._seed_table_con_orden_activa(status="pendiente")
        db, table_b, ts_b, order_b = self._seed_table_con_orden_activa(db, status="pendiente")

        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant_a = fx.make_variant(db, product=product, price=Decimal("20000"))
        variant_b = fx.make_variant(db, product=product, price=Decimal("15000"))
        fx.make_order_item(db, order_a, variant_a)
        fx.make_order_item(db, order_b, variant_b)

        promo = fx.make_promotion(db, status="active")
        fx.add_rule_to_promotion(
            db, promo, type="percent", value=Decimal("10"), min_qty=1,
            variants=[variant_a, variant_b],
        )

        # `merge_orders` rechaza órdenes ya terminales: las dos se fusionan
        # mientras están 'abierta' y el status terminal de A se fija después,
        # directo en la fila.
        group_id = tables_advanced.merge_orders(db, [order_a.id, order_b.id])["merged_group_id"]
        order_a.status = "entregada"
        order_a.payment_status = "PAID"
        db.commit()

        bill = tables_advanced.group_bill(db, group_id)

        order_ids = {o["order_id"] for o in bill["orders"]}
        self.assertEqual(order_ids, {order_a.id, order_b.id})
        self.assertEqual(bill["total"], Decimal("13500"))

    def test_group_bill_igual_a_compute_bill_para_mesa_fusionada_sola_historia_3(self):
        """CONGELA comportamiento corregido — Historia 3 / FR-005 / SC-003:
        para una mesa fusionada sola (fusión degenerada), el total de
        `group_bill` coincide centavo a centavo con el que produciría
        `table_sessions.compute_bill` para esa misma mesa sin fusionar, con
        una combinación de status (`pagada`/`abierta`) y una promoción
        vigente."""
        db = fx.new_session()
        table = fx.make_dining_table(db, status="ocupada")
        ts = fx.make_table_session(db, table=table)

        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant_a = fx.make_variant(db, product=product, price=Decimal("20000"))
        variant_b = fx.make_variant(db, product=product, price=Decimal("15000"))

        order_a = fx.make_customer_order(db, ts, status="pendiente")
        fx.make_order_item(db, order_a, variant_a)
        order_b = fx.make_customer_order(db, ts, status="pendiente")
        fx.make_order_item(db, order_b, variant_b)

        promo = fx.make_promotion(db, status="active")
        fx.add_rule_to_promotion(
            db, promo, type="percent", value=Decimal("10"), min_qty=1,
            variants=[variant_a, variant_b],
        )
        db.commit()

        # Mismo patrón que el test anterior: se fusionan mientras están
        # 'abierta' y el status terminal de A se fija después.
        group_id = tables_advanced.merge_orders(db, [order_a.id, order_b.id])["merged_group_id"]
        order_a.status = "entregada"
        order_a.payment_status = "PAID"
        db.commit()

        session_total = table_sessions_service.compute_bill(db, ts.id).total
        group_total = tables_advanced.group_bill(db, group_id)["total"]

        self.assertEqual(session_total, group_total)
        self.assertEqual(group_total, Decimal("13500"))

    # ------------------------- Nueva protección: orden cerrada con cocina pendiente (spec 035, A-52)

    def _seed_orden_pagada_con_item(self, db=None, *, estado_cocina="pendiente"):
        db, table, ts, order = self._seed_table_con_orden_activa(db, status="entregada")
        order.payment_status = "PAID"
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        fx.make_order_item(db, order, variant, estado_cocina=estado_cocina)
        db.commit()
        return db, table, order

    def test_orden_cerrada_con_item_pendiente_sigue_bloqueando_spec_035(self):
        """Spec 035 (A-52, `registro-de-anomalias.md`), criterio de "pedido
        cerrado" reescrito spec 147 (`'entregada'`+`'PAID'`, ya no
        `status == 'pagada'`): un pedido cerrado (como el que deja
        `checkout_and_send` desde spec 035) con algún ítem todavía sin
        terminar de preparar sigue bloqueando liberar, mover, y fusionar su
        mesa — antes de spec 035, 'pagada' dejaba de bloquear sin mirar el
        estado de cocina de sus ítems."""
        db, table, order = self._seed_orden_pagada_con_item(estado_cocina="pendiente")

        with self.assertRaises(HTTPException) as ctx:
            tables_advanced.set_table_status(db, table.id, "libre")
        self.assertEqual(ctx.exception.status_code, 409)

        destino = fx.make_dining_table(db, status="libre")
        db.commit()
        with self.assertRaises(HTTPException) as ctx:
            tables_advanced.move_order(db, order.id, destino.id)
        self.assertEqual(ctx.exception.status_code, 409)

        _, _, _, otra = self._seed_table_con_orden_activa(db, status="pendiente")
        with self.assertRaises(HTTPException) as ctx:
            tables_advanced.merge_orders(db, [order.id, otra.id])
        self.assertEqual(ctx.exception.status_code, 409)

    def test_orden_cerrada_con_items_terminados_ya_no_bloquea_spec_035(self):
        """Spec 035 (A-52): el mismo pedido cerrado, una vez sus ítems quedan
        `entregado`/`cancelado` (spec 147: antes `listo`/`anulado`), deja de
        bloquear — mismo resultado final que antes de esta spec para una
        orden verdaderamente sin nada pendiente."""
        db, table, order = self._seed_orden_pagada_con_item(estado_cocina="entregado")
        result = tables_advanced.set_table_status(db, table.id, "libre")
        self.assertEqual(result.status, "libre")

        db, table2, order2 = self._seed_orden_pagada_con_item(db, estado_cocina="cancelado")
        destino = fx.make_dining_table(db, status="libre")
        db.commit()
        moved = tables_advanced.move_order(db, order2.id, destino.id)
        self.assertEqual(moved.dining_table_id, destino.id)

        db, table3, order3 = self._seed_orden_pagada_con_item(db, estado_cocina="entregado")
        _, _, _, otra = self._seed_table_con_orden_activa(db, status="pendiente")
        merged = tables_advanced.merge_orders(db, [order3.id, otra.id])
        self.assertIn(order3.id, merged["order_ids"])


_BOGOTA = ZoneInfo("America/Bogota")


def _utc_para_hora_local(y, mo, d, h, mi) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=_BOGOTA).astimezone(timezone.utc)


class TestGroupBillVigenciaCongelada(unittest.TestCase):
    """spec 073, FR-018a (A-70): en la cuenta consolidada de mesas fusionadas
    cada pedido evalúa la vigencia contra SU PROPIO instante congelado, no el
    MIN del grupo — porque esos pedidos se cobran individualmente y el desglose
    consolidado debe coincidir con el cobro pedido-por-pedido."""

    def _seed_grupo(self, db):
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=Decimal("8000"))
        promo = fx.make_promotion(
            db, status="active", start_time=time(18, 0), end_time=time(20, 0),
        )
        fx.add_rule_to_promotion(
            db, promo, type="percent", value=Decimal("50"), min_qty=2, variants=[variant],
        )
        return variant

    def test_cada_pedido_contra_su_propio_instante_no_el_min_del_grupo(self):
        """Pedido A creado 19:59 (dentro de la franja 18:00–20:00), pedido B
        creado 20:05 (fuera). `group_bill` → descuento de A aplicado, de B no.
        El total consolidado == suma de los checkout-preview individuales."""
        db = fx.new_session()
        t_a = fx.make_dining_table(db, status="ocupada")
        ts_a = fx.make_table_session(db, table=t_a)
        t_b = fx.make_dining_table(db, status="ocupada")
        ts_b = fx.make_table_session(db, table=t_b)
        variant = self._seed_grupo(db)

        order_a = fx.make_customer_order(
            db, ts_a, status="pendiente",
            promotion_evaluated_at=_utc_para_hora_local(2026, 9, 2, 19, 59),
        )
        fx.make_order_item(db, order_a, variant, quantity=2, estado_cocina="entregado")
        order_b = fx.make_customer_order(
            db, ts_b, status="pendiente",
            promotion_evaluated_at=_utc_para_hora_local(2026, 9, 2, 20, 5),
        )
        fx.make_order_item(db, order_b, variant, quantity=2, estado_cocina="entregado")
        db.commit()

        group_id = tables_advanced.merge_orders(db, [order_a.id, order_b.id])["merged_group_id"]
        bill = tables_advanced.group_bill(db, group_id)

        por_pedido = {row["order_id"]: row["subtotal"] for row in bill["orders"]}
        self.assertEqual(por_pedido[order_a.id], Decimal("8000"))   # 16000 - 8000 descuento
        self.assertEqual(por_pedido[order_b.id], Decimal("16000"))  # sin descuento (fuera de franja)
        self.assertEqual(bill["total"], Decimal("24000"))
        # Coincide con el cobro pedido-por-pedido (checkout-preview de cada uno).
        self.assertEqual(
            checkout.compute_checkout_preview(db, order_a.id).total, Decimal("8000"),
        )
        self.assertEqual(
            checkout.compute_checkout_preview(db, order_b.id).total, Decimal("16000"),
        )

    def test_pedido_del_grupo_anterior_a_la_spec_evalua_con_la_hora_del_cobro(self):
        """Un pedido del grupo sin `promotion_evaluated_at` (anterior a esta
        spec) evalúa con la hora del cobro — la promoción sin franja siempre
        aplica, sin rama especial (FR-012)."""
        db = fx.new_session()
        t_a = fx.make_dining_table(db, status="ocupada")
        ts_a = fx.make_table_session(db, table=t_a)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=Decimal("8000"))
        # promo sin franja: siempre válida
        promo2 = fx.make_promotion(db, status="active")
        fx.add_rule_to_promotion(
            db, promo2, type="percent", value=Decimal("50"), min_qty=2, variants=[variant],
        )

        order = fx.make_customer_order(db, ts_a, status="pendiente")  # sin instante
        fx.make_order_item(db, order, variant, quantity=2, estado_cocina="entregado")
        t_b = fx.make_dining_table(db, status="ocupada")
        ts_b = fx.make_table_session(db, table=t_b)
        order_b = fx.make_customer_order(db, ts_b, status="pendiente")
        fx.make_order_item(db, order_b, variant, quantity=1, estado_cocina="entregado")
        db.commit()

        group_id = tables_advanced.merge_orders(db, [order.id, order_b.id])["merged_group_id"]
        bill = tables_advanced.group_bill(db, group_id)
        por_pedido = {row["order_id"]: row["subtotal"] for row in bill["orders"]}
        self.assertEqual(por_pedido[order.id], Decimal("8000"))


if __name__ == "__main__":
    unittest.main()
