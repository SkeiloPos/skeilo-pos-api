"""Contrato del desglose de facturación de `GET /orders/{id}` (spec 094).

Verifica las nueve garantías **G1–G9** de
`specs/094-detalle-orden-desglose-entrega/contracts/order-detail-api.md`: de
dónde salen los importes en cada uno de los tres casos de facturación, que la
aritmética cuadra, qué dispara cada aviso, que ningún importe se recalcula
contra el catálogo vigente, y que el detalle no gana un N+1.

Invoca las funciones de endpoint directamente como funciones Python (mismo
patrón que `test_orders_pagination.py`: `Depends(...)` solo se resuelve cuando
FastAPI atiende una request real vía ASGI, así que se pasan `db` y `_`
(usuario) a mano).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_orders_billing_summary -v
"""
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import event

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import router as orders_router

PRECIO = Decimal("8000")


class _FakeRequest:
    """Doble mínimo de `fastapi.Request` — solo lo que lee `json_or_304`."""

    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}
        self.client = SimpleNamespace(host="127.0.0.1")


def _get_order(db, order_id):
    return orders_router.get_order(order_id=order_id, db=db, _=fx.make_user_double())


def _list_orders(db, **kw):
    kw.setdefault("page", 1)
    kw.setdefault("size", 20)
    return orders_router.list_orders(
        _FakeRequest(),
        status_filter=None,
        order_type=None,
        active_sessions_only=False,
        page=kw["page"],
        size=kw["size"],
        date_from=None,
        date_to=None,
        q=None,
        db=db,
        _=fx.make_user_double(),
    )


def _seed(db=None, *, items=1, precio=PRECIO, cantidad=1, **order_kw):
    """Un pedido de mesa con `items` líneas iguales, listo para cobrar.

    Devuelve `(db, order, variant)`. Sin `user_id`: `shared.users` no está en el
    esquema SQLite de los tests de `orders`, igual que en el resto de la red.
    """
    db = db or fx.new_session()
    ts = fx.make_table_session(db)
    order = fx.make_customer_order(db, ts, **order_kw)
    variant = fx.make_variant(db, price=precio)
    for _ in range(items):
        fx.make_order_item(db, order, variant, unit_price=precio, quantity=cantidad)
    db.commit()
    return db, order, variant


class TestEstadoDeFacturacion(unittest.TestCase):
    """G3, G4, G4b, G5 — cuál de los tres casos es, y de dónde salen los importes."""

    def test_g3_venta_propia_lee_los_cuatro_importes_de_la_factura(self):
        # La `Sale` se siembra con importes DISTINTOS de la suma de líneas: si el
        # servidor leyera del pedido, el test no distinguiría la fuente.
        db, order, _ = _seed(items=2)  # Σ line_total = 16.000
        ts_id = order.table_session_id
        fx.make_sale(
            db, order=order, table_session_id=ts_id,
            subtotal=Decimal("28000"), discount=Decimal("4000"),
            delivery_fee=Decimal("0"), total=Decimal("24000"),
        )
        db.commit()

        billing = _get_order(db, order.id).billing

        self.assertEqual(billing.state, "factura_propia")
        self.assertEqual(billing.source, "factura")
        self.assertEqual(billing.subtotal, Decimal("28000"))
        self.assertEqual(billing.discount, Decimal("4000"))
        self.assertEqual(billing.delivery_fee, Decimal("0"))
        self.assertEqual(billing.total, Decimal("24000"))

    def test_g4_venta_de_la_sesion_sin_pedido_es_factura_agrupada_con_origen_pedido(self):
        # Lo que produce el cierre unificado de VARIOS pedidos
        # (`table_sessions/service.py:734` deja `customer_order_id` en None) y el
        # cierre dividido (`:832-847`, una venta por comensal, ninguna con pedido).
        db, order, _ = _seed(items=2)
        fx.make_sale(
            db, table_session_id=order.table_session_id,
            subtotal=Decimal("99999"), total=Decimal("99999"),
        )
        db.commit()

        billing = _get_order(db, order.id).billing

        self.assertEqual(billing.state, "factura_agrupada")
        self.assertEqual(billing.source, "pedido")
        # El desglose es el PROPIO del pedido (FR-024a): no el de esa factura, que
        # cubre varios pedidos y cuyo total no es el de ninguno.
        self.assertEqual(billing.subtotal, Decimal("16000"))
        self.assertEqual(billing.total, Decimal("16000"))

    def test_g4b_un_pedido_cancelado_en_una_sesion_cobrada_es_sin_factura(self):
        # El cierre de sesión solo cobra pedidos con
        # `status NOT IN ('cancelada','pagada')` (`table_sessions/service.py:180`):
        # un pedido cancelado antes del cierre se queda en la sesión con su
        # `table_session_id` intacto y SIN haberse cobrado nunca. Decirle "se
        # cobró junto con otros" (FR-024a) afirmaría un cobro inexistente
        # (research.md D2, RN-005).
        db, order, _ = _seed(items=2, status="cancelada")
        fx.make_sale(db, table_session_id=order.table_session_id, total=Decimal("99999"))
        db.commit()

        billing = _get_order(db, order.id).billing

        self.assertEqual(billing.state, "sin_factura")
        self.assertEqual(billing.source, "pedido")

    def test_la_exclusion_del_cancelado_no_se_aplica_a_la_venta_propia(self):
        # Un pedido `cancelada` CON venta propia es posible en datos históricos
        # (el camino que el hotfix #4 de la spec 029 cerró) y ahí la factura sí
        # cubre ese pedido: FR-024 sigue mandando.
        db, order, _ = _seed(items=1, status="cancelada")
        fx.make_sale(db, order=order, subtotal=Decimal("5000"), total=Decimal("5000"))
        db.commit()

        billing = _get_order(db, order.id).billing

        self.assertEqual(billing.state, "factura_propia")
        self.assertEqual(billing.source, "factura")
        self.assertEqual(billing.total, Decimal("5000"))

    def test_g5_sin_ninguna_venta_no_hay_descuento_ni_promociones(self):
        # FR-023: el descuento se calcula al cobrar, así que un pedido sin cobrar
        # no tiene descuento que mostrar — ni siquiera el que el pedido traiga en
        # su columna.
        db, order, _ = _seed(items=2, discount=Decimal("4000"))
        db.commit()

        billing = _get_order(db, order.id).billing

        self.assertEqual(billing.state, "sin_factura")
        self.assertEqual(billing.source, "pedido")
        self.assertEqual(billing.discount, Decimal("0"))
        self.assertIsNone(billing.discount_label)
        self.assertEqual(billing.promotions, [])
        self.assertEqual(billing.total, Decimal("16000"))

    def test_sin_table_session_y_sin_venta_tambien_es_sin_factura(self):
        # Un pedido de mostrador/domicilio sin sesión de mesa: la regla 2 no puede
        # aplicarse porque no hay `table_session_id` que cotejar.
        db = fx.new_session()
        ts = fx.make_table_session(db)
        order = fx.make_customer_order(
            db, ts, table_session_id=None, dining_table_id=None, order_type="DELIVERY",
        )
        variant = fx.make_variant(db, price=PRECIO)
        fx.make_order_item(db, order, variant, unit_price=PRECIO, quantity=1)
        db.commit()

        billing = _get_order(db, order.id).billing

        self.assertEqual(billing.state, "sin_factura")


class TestImportesDelPedido(unittest.TestCase):
    """G1, G2 — el subtotal es la suma de las líneas cobrables, y la aritmética cuadra."""

    def test_g1_subtotal_excluye_los_items_anulados(self):
        db, order, variant = _seed(items=2)  # 2 × 8.000 = 16.000
        fx.make_order_item(
            db, order, variant, unit_price=PRECIO, quantity=1, estado_cocina="anulado",
        )
        db.commit()

        billing = _get_order(db, order.id).billing

        # El ítem anulado no suma (FR-019), el mismo filtro que
        # `checkout.order_sale_lines` aplica al facturar.
        self.assertEqual(billing.subtotal, Decimal("16000"))

    def test_g1_subtotal_incluye_los_adicionales_por_linea(self):
        # FR-003: `unit_price × quantity` a secas subcobraría los adicionales por
        # línea del Menú QR. La fuente es `OrderItem.line_total`, el auxiliar único.
        db = fx.new_session()
        ts = fx.make_table_session(db)
        order = fx.make_customer_order(db, ts)
        variant = fx.make_variant(db, price=PRECIO)
        fx.make_order_item(
            db, order, variant, unit_price=PRECIO, quantity=2, addons_total=Decimal("2000"),
        )
        db.commit()

        billing = _get_order(db, order.id).billing

        # 8.000 × 2 + 2.000 = 18.000, no 16.000.
        self.assertEqual(billing.subtotal, Decimal("18000"))

    def test_g2_la_formula_cuadra_y_el_total_no_es_negativo_en_los_tres_estados(self):
        # sin_factura: Total = Subtotal + Envío (sin descuento, FR-023)
        db, sin_factura, _ = _seed(items=2, delivery_fee=Decimal("3000"))
        db.commit()
        b1 = _get_order(db, sin_factura.id).billing
        self.assertEqual(b1.total, b1.subtotal - b1.discount + b1.delivery_fee)
        self.assertEqual(b1.total, Decimal("19000"))

        # factura_agrupada: la fórmula completa sobre los datos del pedido
        db2, agrupada, _ = _seed(
            items=2, delivery_fee=Decimal("1000"), discount=Decimal("2000"),
        )
        fx.make_sale(db2, table_session_id=agrupada.table_session_id, total=Decimal("1"))
        db2.commit()
        b2 = _get_order(db2, agrupada.id).billing
        self.assertEqual(b2.state, "factura_agrupada")
        self.assertEqual(b2.total, b2.subtotal - b2.discount + b2.delivery_fee)
        self.assertEqual(b2.total, Decimal("15000"))

        # factura_propia: los importes son los de la factura y, con tax = tip = 0
        # (compuerta D5, medida antes de implementar), la fórmula también cuadra.
        db3, propia, _ = _seed(items=2)
        fx.make_sale(
            db3, order=propia, subtotal=Decimal("20000"), discount=Decimal("5000"),
            delivery_fee=Decimal("3000"), total=Decimal("18000"),
        )
        db3.commit()
        b3 = _get_order(db3, propia.id).billing
        self.assertEqual(b3.total, b3.subtotal - b3.discount + b3.delivery_fee)

        for b in (b1, b2, b3):
            self.assertGreaterEqual(b.total, Decimal("0"))

    def test_g2_un_descuento_mayor_que_el_subtotal_no_produce_un_total_negativo(self):
        # FR-008: el total nunca es negativo. Un pedido con un descuento mayor que
        # la suma de sus líneas no debería existir, pero si existe en el histórico
        # la pantalla no muestra un total en rojo.
        db, order, _ = _seed(items=1, discount=Decimal("50000"))
        fx.make_sale(db, table_session_id=order.table_session_id, total=Decimal("1"))
        db.commit()

        billing = _get_order(db, order.id).billing

        self.assertEqual(billing.state, "factura_agrupada")
        self.assertEqual(billing.total, Decimal("0"))

    def test_el_pedido_sin_items_tiene_subtotal_cero_y_total_igual_al_envio(self):
        db = fx.new_session()
        ts = fx.make_table_session(db)
        order = fx.make_customer_order(db, ts, delivery_fee=Decimal("3000"))
        db.commit()

        billing = _get_order(db, order.id).billing

        self.assertEqual(billing.subtotal, Decimal("0"))
        self.assertEqual(billing.total, Decimal("3000"))
        # El `0` es real, no un hueco: no se dispara el aviso de falta de detalle.
        self.assertFalse(billing.sin_detalle_de_precios)

    def test_delivery_fee_en_null_se_publica_como_cero(self):
        # La columna es nulable; el contrato publica `"0.00"` y la UI no pinta la
        # fila (RN-004). Nunca se pinta "$ 0".
        db, order, _ = _seed(items=1, delivery_fee=None)
        db.commit()

        billing = _get_order(db, order.id).billing

        self.assertEqual(billing.delivery_fee, Decimal("0"))
        self.assertEqual(billing.total, Decimal("8000"))


class TestSinDetalleDePrecios(unittest.TestCase):
    """G6 — la condición literal de FR-018, con sus tres contra-casos.

    `sin_detalle_de_precios` es `True` **si y solo si** hay ≥ 1 ítem no anulado
    y la suma de sus `line_total` es `0`. Las tres precisiones que FR-018 exige
    por separado salen gratis de esa definición (research.md D8).
    """

    def test_g6_items_no_anulados_que_suman_cero_disparan_el_aviso(self):
        db, order, _ = _seed(items=2, precio=Decimal("0"))
        db.commit()

        billing = _get_order(db, order.id).billing

        self.assertTrue(billing.sin_detalle_de_precios)
        self.assertEqual(billing.subtotal, Decimal("0"))

    def test_g6_contracaso_un_pedido_normal_sin_descuento_no_lo_dispara(self):
        db, order, _ = _seed(items=2)
        db.commit()

        self.assertFalse(_get_order(db, order.id).billing.sin_detalle_de_precios)

    def test_g6_contracaso_todas_las_lineas_anuladas_no_lo_dispara(self):
        # No hay ningún ítem no anulado, así que la condición no se cumple: el
        # `$ 0` del resumen es el valor real de lo cobrable (FR-018).
        db = fx.new_session()
        ts = fx.make_table_session(db)
        order = fx.make_customer_order(db, ts)
        variant = fx.make_variant(db, price=PRECIO)
        for _ in range(2):
            fx.make_order_item(
                db, order, variant, unit_price=PRECIO, quantity=1,
                estado_cocina="anulado",
            )
        db.commit()

        billing = _get_order(db, order.id).billing

        self.assertFalse(billing.sin_detalle_de_precios)
        self.assertEqual(billing.subtotal, Decimal("0"))

    def test_g6_contracaso_un_pedido_sin_lineas_no_lo_dispara(self):
        db = fx.new_session()
        ts = fx.make_table_session(db)
        order = fx.make_customer_order(db, ts)
        db.commit()

        self.assertFalse(_get_order(db, order.id).billing.sin_detalle_de_precios)


class TestEtiquetaDelDescuento(unittest.TestCase):
    """G7 — los cuatro casos de research.md D6.

    El snapshot guardado tiene **una entrada por regla**, no por promoción
    (`promotions/service.py:126-131`): contar entradas en vez de promociones
    pintaría "Descuento" donde FR-009 exige el nombre.
    """

    def _con_promociones(self, promos, *, discount=Decimal("4000")):
        db, order, _ = _seed(items=2)
        fx.make_sale(
            db, order=order, subtotal=Decimal("28000"), discount=discount,
            total=Decimal("28000") - discount, applied_promotions=promos,
        )
        db.commit()
        return _get_order(db, order.id).billing

    def test_g7_una_sola_promocion_da_su_nombre(self):
        pid = str(uuid4())
        billing = self._con_promociones(
            [{"promotion_id": pid, "rule_id": str(uuid4()),
              "name": "2 x $12.000", "amount": "4000.00"}]
        )

        self.assertEqual(billing.discount_label, "2 x $12.000")
        self.assertEqual(len(billing.promotions), 1)

    def test_g7_dos_reglas_de_la_misma_promocion_dan_el_nombre_de_esa_promocion(self):
        # Es el caso que motiva agrupar por `promotion_id` en vez de contar
        # entradas: son dos filas del JSONB, pero UNA promoción.
        pid = str(uuid4())
        billing = self._con_promociones([
            {"promotion_id": pid, "rule_id": str(uuid4()),
             "name": "2 x $12.000", "amount": "3000.00"},
            {"promotion_id": pid, "rule_id": str(uuid4()),
             "name": "2 x $12.000", "amount": "1000.00"},
        ])

        self.assertEqual(billing.discount_label, "2 x $12.000")

    def test_g7_dos_promociones_distintas_dan_none(self):
        # FR-010: repartir el agregado entre promociones para pintar una fila por
        # cada una sería recalcular. Con dos o más, la etiqueta es "Descuento".
        billing = self._con_promociones([
            {"promotion_id": str(uuid4()), "rule_id": str(uuid4()),
             "name": "2 x $12.000", "amount": "3000.00"},
            {"promotion_id": str(uuid4()), "rule_id": str(uuid4()),
             "name": "Martes de helado", "amount": "1000.00"},
        ])

        self.assertIsNone(billing.discount_label)

    def test_g7_las_entradas_con_monto_cero_se_ignoran_al_elegir_la_etiqueta(self):
        pid = str(uuid4())
        billing = self._con_promociones([
            {"promotion_id": pid, "rule_id": str(uuid4()),
             "name": "2 x $12.000", "amount": "4000.00"},
            {"promotion_id": str(uuid4()), "rule_id": str(uuid4()),
             "name": "Promo que no aportó", "amount": "0.00"},
        ])

        # La segunda no descontó nada: no convierte el caso en "dos promociones".
        self.assertEqual(billing.discount_label, "2 x $12.000")

    def test_g7_sin_promociones_la_etiqueta_es_none(self):
        billing = self._con_promociones([])

        self.assertIsNone(billing.discount_label)
        self.assertEqual(billing.promotions, [])

    def test_g7_no_publica_el_rule_id_del_jsonb(self):
        # `rule_id` existe en el snapshot y no se publica: expone un detalle
        # interno del motor de promociones (data-model.md §2).
        billing = self._con_promociones(
            [{"promotion_id": str(uuid4()), "rule_id": str(uuid4()),
              "name": "2 x $12.000", "amount": "4000.00"}]
        )

        self.assertFalse(hasattr(billing.promotions[0], "rule_id"))
        self.assertNotIn("rule_id", billing.promotions[0].model_dump())

    def test_una_promocion_sin_promotion_id_se_agrupa_por_nombre(self):
        # Lectura tolerante del JSONB histórico: `promotion_id` puede faltar.
        billing = self._con_promociones([
            {"name": "Promo vieja", "amount": "2000.00"},
            {"name": "Promo vieja", "amount": "2000.00"},
        ])

        self.assertEqual(billing.discount_label, "Promo vieja")


class TestImportesCongelados(unittest.TestCase):
    """G8 — ningún importe se recalcula contra el catálogo ni las promociones
    vigentes al consultar (RN-001, FR-022)."""

    def test_g8_cambiar_el_precio_del_catalogo_no_mueve_ningun_importe(self):
        db, order, variant = _seed(items=2)  # 2 × 8.000 = 16.000
        antes = _get_order(db, order.id).billing

        # El producto sube de precio DESPUÉS de crear el pedido, y nace una
        # promoción vigente que le aplicaría si se cobrara hoy.
        variant.price = Decimal("20000")
        promo = fx.make_promotion(db, name="Promo nueva de hoy")
        fx.add_rule_to_promotion(
            db, promo, type="percent", value=Decimal("50"), variants=[variant],
        )
        db.commit()
        db.expire_all()

        despues = _get_order(db, order.id).billing

        self.assertEqual(despues.subtotal, antes.subtotal)
        self.assertEqual(despues.discount, antes.discount)
        self.assertEqual(despues.total, antes.total)
        self.assertEqual(despues.subtotal, Decimal("16000"))
        # La promoción nueva no aparece: el snapshot del pedido sigue vacío.
        self.assertEqual(despues.promotions, [])
        self.assertIsNone(despues.discount_label)

    def test_g8_el_nombre_de_la_promocion_es_el_del_momento_del_cobro(self):
        # Se lee el `name` del snapshot, no la fila vigente de `promotions`.
        db, order, _ = _seed(items=2)
        promo = fx.make_promotion(db, name="Nombre nuevo tras renombrar")
        fx.make_sale(
            db, order=order, subtotal=Decimal("16000"), discount=Decimal("1000"),
            total=Decimal("15000"),
            applied_promotions=[{
                "promotion_id": str(promo.id), "rule_id": str(uuid4()),
                "name": "Nombre del momento del cobro", "amount": "1000.00",
            }],
        )
        db.commit()

        billing = _get_order(db, order.id).billing

        self.assertEqual(billing.discount_label, "Nombre del momento del cobro")


class TestTrabajoAcotado(unittest.TestCase):
    """G9 + D3 — **guardia de regresión**, no test de comportamiento nuevo.

    Empieza en verde por construcción: en cuanto `billing` existe en el esquema
    con `None` por defecto, las dos aserciones pasan sin implementación. Su
    valor no es fallar ahora, sino ponerse roja si el armador del desglose
    introduce un N+1 o si el listado empieza a calcular `billing`. Por eso se
    ejecuta **antes y después** de la implementación y el conteo de referencia
    queda anotado en `implementation-notes.md` (misma excepción razonada que el
    404 de aislamiento, `tasks.md` → Dentro de cada historia).
    """

    @staticmethod
    def _sentencias_del_detalle(n_items):
        db = fx.new_session()
        ts = fx.make_table_session(db)
        order = fx.make_customer_order(db, ts)
        variant = fx.make_variant(db, price=PRECIO)
        for _ in range(n_items):
            fx.make_order_item(db, order, variant, unit_price=PRECIO, quantity=1)
        db.commit()

        sentencias: list[str] = []
        bind = db.get_bind()

        def _rec(conn, cursor, statement, parameters, context, executemany):
            sentencias.append(statement)

        event.listen(bind, "before_cursor_execute", _rec)
        try:
            _get_order(db, order.id)
        finally:
            event.remove(bind, "before_cursor_execute", _rec)
        return len(sentencias)

    def test_g9_el_numero_de_sentencias_no_crece_con_el_numero_de_items(self):
        # 1 ítem vs 10 ítems: un N+1 al resolver el estado de facturación (p. ej.
        # una consulta de venta por línea) haría crecer el conteo.
        self.assertEqual(self._sentencias_del_detalle(1), self._sentencias_del_detalle(10))

    def test_d3_el_listado_devuelve_billing_en_null_en_todos_sus_items(self):
        # El listado serializa hasta 100 pedidos por página: calcular el estado de
        # facturación por fila sería justo el N+1 que la guardia de
        # `test_orders_pagination` vigila. Esta spec declara que no lo toca.
        db = fx.new_session()
        ts = fx.make_table_session(db)
        base = datetime(2026, 9, 1, 12, 0, 0)
        for i in range(5):
            order = fx.make_customer_order(db, ts, created_at=base + timedelta(minutes=i))
            variant = fx.make_variant(db, price=PRECIO)
            fx.make_order_item(db, order, variant, unit_price=PRECIO, quantity=1)
        fx.make_sale(db, table_session_id=ts.id, total=Decimal("40000"))
        db.commit()

        result = _list_orders(db)

        self.assertEqual(len(result["items"]), 5)
        for o in result["items"]:
            self.assertIsNone(getattr(o, "billing", None))

    def test_d3_el_listado_no_gana_sentencias_al_crecer_la_pagina(self):
        # La guardia propia del listado (`test_orders_pagination`) sigue en verde:
        # se repite aquí para que un cambio en `_decorate_orders` hecho desde esta
        # spec se note en este archivo, no solo en el de paginación.
        conteos = []
        for n in (5, 20):
            db = fx.new_session()
            ts = fx.make_table_session(db)
            base = datetime(2026, 9, 1, 12, 0, 0)
            for i in range(n):
                fx.make_customer_order(db, ts, created_at=base + timedelta(minutes=i))
            db.commit()

            sentencias: list[str] = []
            bind = db.get_bind()

            def _rec(conn, cursor, statement, parameters, context, executemany):
                sentencias.append(statement)

            event.listen(bind, "before_cursor_execute", _rec)
            try:
                _list_orders(db)
            finally:
                event.remove(bind, "before_cursor_execute", _rec)
            conteos.append(len(sentencias))

        self.assertEqual(conteos[0], conteos[1])


class TestAislamiento(unittest.TestCase):
    """FR-025, RN-006 — **prueba de regresión** del aislamiento ya existente.

    No hay código de aislamiento que escribir: lo da el `search_path` por
    esquema de tenant, que esta spec no puede ni debe tocar (research.md D17).
    Lo que se congela aquí es que la respuesta no revela nada: el `404` es el
    mismo para "no existe" y para "existe en otro tenant".

    Por eso vive en Polish y no antes de la implementación: congela el
    comportamiento actual de `_load_order` (`router.py:47-64`), no comportamiento
    nuevo. **La parte de dos esquemas reales no se puede automatizar**: la suite
    corre sobre SQLite en memoria, sin esquemas por tenant, así que el recorrido
    de dos tenants es manual y explícito (`quickstart.md` §7) en vez de un test
    que finge cubrirlo.
    """

    def test_un_id_que_no_existe_responde_404_sin_revelar_nada(self):
        db = fx.new_session()
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            _get_order(db, uuid4())

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail, "Order not found")

    def test_la_respuesta_es_identica_para_un_id_inexistente_y_para_uno_de_otro_esquema(self):
        # Sobre SQLite no hay esquemas por tenant, así que el equivalente
        # verificable es: un `id` bien formado que no está en ESTA sesión
        # responde exactamente igual que uno inventado. Un pedido que sí existe
        # en otra base de datos es, desde aquí, indistinguible de uno inexistente
        # — y eso es precisamente lo que el contrato promete.
        db_a = fx.new_session()
        _, order_de_a, _ = _seed(db_a)
        db_b = fx.new_session()
        db_b.commit()

        with self.assertRaises(HTTPException) as ajeno:
            _get_order(db_b, order_de_a.id)
        with self.assertRaises(HTTPException) as inexistente:
            _get_order(db_b, uuid4())

        self.assertEqual(ajeno.exception.status_code, inexistente.exception.status_code)
        self.assertEqual(ajeno.exception.detail, inexistente.exception.detail)
        self.assertEqual(ajeno.exception.detail, "Order not found")
        # El cuerpo no lleva nada del pedido: ni su id, ni su total, ni su estado.
        self.assertNotIn(str(order_de_a.id), str(ajeno.exception.detail))


class TestTotalDelDetalleIgualABilling(unittest.TestCase):
    """spec 121, FR-001/FR-002, contracts/orders-total-field.md: `OrderResponse.total`
    en el detalle siempre coincide con `billing.total`, en los tres estados de
    facturación — es el mismo cálculo, solo expuesto también fuera de `billing`."""

    def test_factura_propia_total_del_detalle_igual_a_billing_total(self):
        db, order, _ = _seed(items=2)  # Σ line_total = 16.000
        fx.make_sale(
            db, order=order, table_session_id=order.table_session_id,
            subtotal=Decimal("16000"), discount=Decimal("1000"),
            delivery_fee=Decimal("0"), total=Decimal("15000"),
        )
        db.commit()

        response = _get_order(db, order.id)

        self.assertEqual(response.billing.state, "factura_propia")
        self.assertEqual(response.total, Decimal("15000"))
        self.assertEqual(response.total, response.billing.total)

    def test_factura_agrupada_total_del_detalle_igual_a_billing_total(self):
        db, order, _ = _seed(items=2)  # Σ line_total = 16.000
        fx.make_sale(db, table_session_id=order.table_session_id, subtotal=Decimal("99999"), total=Decimal("99999"))
        db.commit()

        response = _get_order(db, order.id)

        self.assertEqual(response.billing.state, "factura_agrupada")
        self.assertEqual(response.total, Decimal("16000"))
        self.assertEqual(response.total, response.billing.total)

    def test_sin_factura_total_del_detalle_igual_a_billing_total(self):
        db, order, _ = _seed(items=2)  # Σ line_total = 16.000, sin ninguna Sale

        response = _get_order(db, order.id)

        self.assertEqual(response.billing.state, "sin_factura")
        self.assertEqual(response.total, Decimal("16000"))
        self.assertEqual(response.total, response.billing.total)


if __name__ == "__main__":
    unittest.main()
