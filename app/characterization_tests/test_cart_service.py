"""Characterization tests de las 11 funciones públicas de
`app/api/v1/cart/service.py` (specs/015-caracterizacion-cart, Historia 1).

Cada test CONGELA el comportamiento actual del código, ejercitando el motor
de catálogo, las promociones y el checkout reales (sin mocks) contra SQLite
en memoria vía `cart_fixtures.py`. Incluye los dos casos de anomalía ya
documentados que esta spec NO corrige (FR-011):

  - A-17 (R16): `test_open_session_a17_r16_...`
  - A-08: `test_open_session_y_serialize_cart_a08_...`

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_cart_service -v
"""
from datetime import datetime, time, timezone
from decimal import Decimal
import unittest
from unittest import mock
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import MultipleResultsFound

from app.characterization_tests import cart_fixtures
from app.api.v1.cart import service
from app.core.exceptions import PendingPaymentExit
from app.core.models import Tenant
from app.api.v1.cart.schemas import (
    CartItemIn, CartItemUpdate, CartResponse, SessionOpenResponse,
)
from app.api.v1.catalog.schemas import OptionSelectionIn
from app.models.cart import Cart
from app.models.customer_order import CustomerOrder
from app.models.session_participant import SessionParticipant
from app.models.table_session import TableSession


class TestCartService(unittest.TestCase):
    # ------------------------------------------------------------- Helpers

    def _seed_session(self, *, table_status: str = "libre"):
        """`db` + mesa + `TableSession` activa + comensal, listos para operar
        el carrito. Devuelve `(db, table, table_session, participant)`."""
        db = cart_fixtures.new_session()
        table = cart_fixtures.make_dining_table(db, status=table_status)
        table_session = cart_fixtures.make_table_session(db, table=table)
        participant = cart_fixtures.make_participant(db, table_session=table_session)
        return db, table, table_session, participant

    def _seed_variant(self, db, **kw):
        category = cart_fixtures.make_category(db)
        product = cart_fixtures.make_product(db, category=category)
        kw.setdefault("price", Decimal("8000"))
        return cart_fixtures.make_variant(db, product=product, **kw), product, category

    def _seed_efectivo(self, db):
        """spec 025: `submit_cart` exige un método de pago activo — la
        mayoría de estos tests solo necesitan que exista uno, sin ejercitar
        ninguna regla de negocio sobre él (Principio III, actualización
        explícita citando esta spec)."""
        return cart_fixtures.make_payment_method(db, name=f"efectivo-{uuid4()}", is_cash=True)

    # ------------------------------------------------------- unique_display_label (T009)

    def test_unique_display_label(self):
        """CONGELA comportamiento actual: un nombre sin colisión se devuelve
        tal cual; con un participante existente del mismo nombre en la
        sesión, se sufija de forma determinista ("Ana" -> "Ana (2)" ->
        "Ana (3)")."""
        db = cart_fixtures.new_session()
        table = cart_fixtures.make_dining_table(db)
        ts = cart_fixtures.make_table_session(db, table=table)

        self.assertEqual(service.unique_display_label(db, ts.id, "Ana"), "Ana")

        cart_fixtures.make_participant(
            db, table_session=ts, display_name="Ana", display_label="Ana"
        )
        self.assertEqual(service.unique_display_label(db, ts.id, "Ana"), "Ana (2)")

        cart_fixtures.make_participant(
            db, table_session=ts, display_name="Ana", display_label="Ana (2)"
        )
        self.assertEqual(service.unique_display_label(db, ts.id, "Ana"), "Ana (3)")

    # ------------------------------------------------------------ open_session (T010)

    def test_open_session_camino_feliz(self):
        """CONGELA comportamiento actual: mesa activa crea `TableSession` +
        `SessionParticipant` + carrito inicial 'abierto'; `expires_at` sale
        de `_now() + SESSION_TTL_MINUTES`; la mesa pasa a 'ocupada'."""
        db = cart_fixtures.new_session()
        table = cart_fixtures.make_dining_table(db, status="libre")

        resp = service.open_session(db, 1, table, "Ana")

        self.assertIsInstance(resp, SessionOpenResponse)
        self.assertEqual(resp.display_name, "Ana")
        self.assertEqual(resp.display_label, "Ana")
        self.assertIsNotNone(resp.session_token)
        self.assertIsNotNone(resp.expires_at)
        self.assertEqual(resp.table.id, table.id)

        self.assertEqual(table.status, "ocupada")

        ts = db.get(TableSession, resp.table_session_id)
        self.assertEqual(ts.status, "active")

        cart = db.get(Cart, resp.cart_id)
        self.assertEqual(cart.status, "abierto")

    def test_open_session_segundo_comensal_se_une_a_la_misma_sesion(self):
        """CONGELA comportamiento actual: escanear el QR de una mesa ya
        ocupada une al segundo comensal a la `TableSession` en curso; no abre
        una segunda. La mesa ya estaba 'ocupada', así que no se dispara otro
        cambio de estado."""
        db = cart_fixtures.new_session()
        table = cart_fixtures.make_dining_table(db, status="libre")

        first = service.open_session(db, 1, table, "Ana")
        second = service.open_session(db, 1, table, "Ana")

        self.assertEqual(first.table_session_id, second.table_session_id)
        self.assertEqual(second.display_label, "Ana (2)")

    # ------------------------------------------------- open_session — A-17/R16 (T011)

    def test_open_session_a17_r16_doble_sesion_activa_no_controlada(self):
        """CONGELA comportamiento actual (A-17 / R16): si la mesa ya tiene
        DOS `TableSession` en `status='active'` (estado hoy alcanzable, ver
        research.md §3 — el índice único parcial no lo impide en producción
        bajo una carrera, y aquí se siembra directo para reproducirlo),
        `_get_or_create_table_session` usa `scalar_one_or_none()`: con más de
        una fila coincidente, SQLAlchemy lanza `MultipleResultsFound`, que
        `open_session` no traduce a un 4xx explícito — se propaga tal cual
        tras el rollback."""
        db = cart_fixtures.new_session()
        table = cart_fixtures.make_dining_table(db, status="libre")
        cart_fixtures.make_table_session(db, table=table, status="active")
        cart_fixtures.make_table_session(db, table=table, status="active")

        with self.assertRaises(MultipleResultsFound):
            service.open_session(db, 1, table, "Ana")

    # --------------------------------------------------- open_session/serialize — A-08 (T012)

    def test_serialize_cart_a08_zona_horaria_aplicada_tras_la_correccion(self):
        """CONGELA comportamiento corregido — A-08 (`cart/service.py:205`,
        cierre: specs/022-correccion-zona-horaria-menu-carrito): `serialize_cart`
        ahora pasa un `datetime` aware (`datetime.now(timezone.utc)`) a
        `promotions.local_now()`, que lo convierte correctamente a hora local
        del tenant (`TENANT_TIMEZONE=America/Bogota`, UTC-5) en vez de
        tratarlo como si ya lo estuviera. A las 20:00 UTC (15:00 Bogotá real,
        fuera de la ventana 20:00-21:00 local), una promoción con esa ventana
        horaria ya NO aparece vigente ni descuenta — antes de esta corrección,
        sí lo hacía (registro-de-anomalias.md, A-08)."""
        db, table, ts, participant = self._seed_session()
        variant, product, category = self._seed_variant(db)
        cart_fixtures.make_promotion(
            db, status="active", start_time=time(20, 0), end_time=time(21, 0),
        )

        instant = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)
        with cart_fixtures.frozen_now(instant):
            resp = service.add_item(
                db, participant.id,
                CartItemIn(product_variant_id=variant.id, quantity=1),
            )

        self.assertIsNone(resp.discounted_total)

    def test_serialize_cart_dentro_de_ventana_en_hora_local_si_descuenta(self):
        """CA3 (sin regresión): a la 01:00 UTC del día siguiente (20:00
        Bogotá, dentro de la ventana 20:00-21:00 local) el carrito SÍ debe
        aplicar el descuento — reescrito para el conjunto de variantes de la
        spec 063 (A-58…A-65); la corrección de zona horaria (A-08) se conserva."""
        db, table, ts, participant = self._seed_session()
        variant, product, category = self._seed_variant(db)
        promo = cart_fixtures.make_promotion(
            db, status="active", start_time=time(20, 0), end_time=time(21, 0),
        )
        cart_fixtures.add_rule_to_promotion(
            db, promo, type="percent", value=Decimal("20"), min_qty=1,
            variants=[variant],
        )

        instant = datetime(2026, 1, 16, 1, 0, tzinfo=timezone.utc)
        with cart_fixtures.frozen_now(instant):
            resp = service.add_item(
                db, participant.id,
                CartItemIn(product_variant_id=variant.id, quantity=1),
            )

        self.assertIsNotNone(resp.discounted_total)
        self.assertLess(resp.discounted_total, resp.total)

    def test_serialize_cart_mismo_instante_distinto_tenant_distinto_descuento(self):
        """spec 127 (FR-002/FR-003): mismo instante real, resultado de
        vigencia distinto según la zona horaria del `tenant` -- antes de esta
        spec `serialize_cart` nunca recibía ningún `tenant`, así que todos los
        tenants caían al mismo respaldo global (`America/Bogota`)."""
        db, table, ts, participant = self._seed_session()
        variant, product, category = self._seed_variant(db)
        promo = cart_fixtures.make_promotion(
            db, status="active", start_time=time(22, 0), end_time=time(23, 0),
        )
        cart_fixtures.add_rule_to_promotion(
            db, promo, type="percent", value=Decimal("20"), min_qty=1,
            variants=[variant],
        )

        # 2026-08-06T03:30:00Z: 22:30 en Bogotá (UTC-5, DENTRO de 22:00-23:00)
        # pero 21:30 en Ciudad de México (UTC-6, FUERA de 22:00-23:00).
        instant = datetime(2026, 8, 6, 3, 30, tzinfo=timezone.utc)
        with cart_fixtures.frozen_now(instant):
            service.add_item(
                db, participant.id,
                CartItemIn(product_variant_id=variant.id, quantity=1),
            )
            resp_bogota = service.get_cart(db, participant.id, Tenant(timezone="America/Bogota"))
            resp_mexico = service.get_cart(
                db, participant.id, Tenant(timezone="America/Mexico_City")
            )

        self.assertIsNotNone(resp_bogota.discounted_total)
        self.assertLess(resp_bogota.discounted_total, resp_bogota.total)
        self.assertIsNone(resp_mexico.discounted_total)

    # ------------------------------------------------------------------ get_cart (T013)

    def test_get_cart_carrito_existente(self):
        """CONGELA comportamiento actual: `get_cart` serializa el carrito
        abierto del comensal, incluyendo su nombre (no viaja en el token de
        sesión)."""
        db, table, ts, participant = self._seed_session()
        variant, _, _ = self._seed_variant(db)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))

        resp = service.get_cart(db, participant.id)

        self.assertIsInstance(resp, CartResponse)
        self.assertEqual(resp.participant_id, participant.id)
        self.assertEqual(resp.display_name, participant.display_name)
        self.assertEqual(len(resp.items), 1)

    def test_get_cart_sin_carrito_abierto_crea_uno_nuevo(self):
        """CONGELA comportamiento actual: si el comensal no tiene carrito
        'abierto' (p. ej. justo tras crearse el participante sin que
        `open_session` le haya sembrado uno), `get_cart` no responde 404: usa
        `_get_or_create_open_cart`, que crea uno vacío en el acto."""
        db = cart_fixtures.new_session()
        table = cart_fixtures.make_dining_table(db)
        ts = cart_fixtures.make_table_session(db, table=table)
        participant = cart_fixtures.make_participant(db, table_session=ts)

        resp = service.get_cart(db, participant.id)

        self.assertEqual(resp.status, "abierto")
        self.assertEqual(resp.items, [])

    # -------------------------------------------------------------------- add_item (T014)

    def test_add_item_variante_con_opciones(self):
        """CONGELA comportamiento actual (adicional por línea desde A-94, spec 089):
        variante activa + opciones válidas
        delegan en `compute_line_price`/`load_valid_options`/
        `check_availability` reales; el precio de línea, las opciones
        guardadas y el `CartResponse` coinciden con lo que produce el código
        hoy."""
        db, table, ts, participant = self._seed_session()
        variant, product, category = self._seed_variant(db, price=Decimal("8000"))
        group = cart_fixtures.make_option_group(db, min_select=1, max_select=1)
        option = cart_fixtures.make_option(db, group=group, extra_price=Decimal("500"))
        cart_fixtures.link_variant_group(db, variant, group, min_select=1, max_select=1)

        resp = service.add_item(
            db, participant.id,
            CartItemIn(product_variant_id=variant.id, quantity=2, options=[OptionSelectionIn(option_id=option.id)]),
        )

        self.assertEqual(len(resp.items), 1)
        item = resp.items[0]
        self.assertEqual(item.quantity, 2)
        # spec 089 (A-94): el grupo es "con_recargo" (default del fixture), así que su opción
        # es un adicional: se cobra UNA vez por línea, no por unidad. Antes de A-94 esto
        # congelaba `unit_price=8500` / `line_total=17000` (el extra multiplicado por 2).
        self.assertEqual(item.unit_price, Decimal("8000"))
        self.assertEqual(item.addons_total, Decimal("500"))
        self.assertEqual(item.line_total, Decimal("16500"))
        self.assertEqual([o.option_id for o in item.options], [option.id])
        self.assertTrue(item.options[0].per_line)

    def test_add_item_variante_inactiva_422(self):
        """CONGELA comportamiento actual: agregar una variante inactiva
        responde 422 antes de tocar el motor de precio/disponibilidad."""
        db, table, ts, participant = self._seed_session()
        variant, _, _ = self._seed_variant(db, active=False)

        with self.assertRaises(HTTPException) as ctx:
            service.add_item(
                db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1)
            )
        self.assertEqual(ctx.exception.status_code, 422)

    # spec 063 (FR-024, A-61): `test_add_item_combo` se elimina — el mecanismo de
    # selección explícita de combos se retira; `CartItemIn` ya no acepta `combo_id`.

    # ----------------------------------------------------------------- update_item (T015)

    def test_update_item_cambia_cantidad_y_recalcula_precio(self):
        """CONGELA comportamiento actual: `update_item` cambia la cantidad de
        una línea existente y recalcula `unit_price`/`line_total` a partir
        del motor de precio real."""
        db, table, ts, participant = self._seed_session()
        variant, _, _ = self._seed_variant(db, price=Decimal("5000"))
        added = service.add_item(
            db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1)
        )
        item_id = added.items[0].id

        resp = service.update_item(db, participant.id, item_id, CartItemUpdate(quantity=3))

        self.assertEqual(resp.items[0].quantity, 3)
        self.assertEqual(resp.items[0].line_total, Decimal("15000"))

    def test_update_item_no_existente_404(self):
        """CONGELA comportamiento actual: editar una línea que no pertenece
        al carrito abierto del comensal responde 404."""
        db, table, ts, participant = self._seed_session()
        cart_fixtures.make_cart(db, participant=participant)
        from uuid import uuid4
        with self.assertRaises(HTTPException) as ctx:
            service.update_item(db, participant.id, uuid4(), CartItemUpdate(quantity=2))
        self.assertEqual(ctx.exception.status_code, 404)

    # ----------------------------------------------------------------- remove_item (T016)

    def test_remove_item_ultima_linea_deja_carrito_vacio(self):
        """CONGELA comportamiento actual: `remove_item` elimina la línea
        indicada; al quitar la última, el `CartResponse` queda con
        `items == []` (el carrito sigue 'abierto', no se borra)."""
        db, table, ts, participant = self._seed_session()
        variant, _, _ = self._seed_variant(db)
        added = service.add_item(
            db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1)
        )
        item_id = added.items[0].id

        resp = service.remove_item(db, participant.id, item_id)

        self.assertEqual(resp.items, [])
        self.assertEqual(resp.status, "abierto")

    # ------------------------------------------------- serialize_cart / discounted_total (T017)

    def test_serialize_cart_discounted_total_sin_promocion(self):
        """CONGELA comportamiento actual (FR-007): sin ninguna promoción
        automática activa, `discounted_total` es `None` (no cero)."""
        db, table, ts, participant = self._seed_session()
        variant, _, _ = self._seed_variant(db)

        resp = service.add_item(
            db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1)
        )

        self.assertIsNone(resp.discounted_total)

    def test_serialize_cart_discounted_total_con_promocion_activa(self):
        """CONGELA comportamiento actual, reescrito para el conjunto de
        variantes de la spec 063 (A-58…A-65): con una promoción `percent`
        activa cuyo conjunto incluye la variante de la línea, `discounted_total`
        queda por debajo de `total` y la línea trae su
        `discounted_unit_price`/`discounted_line_total`."""
        db, table, ts, participant = self._seed_session()
        variant, product, category = self._seed_variant(db, price=Decimal("10000"))
        promo = cart_fixtures.make_promotion(db, status="active")
        cart_fixtures.add_rule_to_promotion(
            db, promo, type="percent", value=Decimal("10"), min_qty=1,
            variants=[variant],
        )

        resp = service.add_item(
            db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1)
        )

        self.assertIsNotNone(resp.discounted_total)
        self.assertEqual(resp.discounted_total, Decimal("9000.00"))
        self.assertEqual(resp.items[0].discounted_line_total, Decimal("9000.00"))

    def test_us4_ca3_package_price_min_qty_3_solo_descuenta_al_completar_el_grupo(self):
        """spec 063, US4-CA3 (contracts/migracion.md §3): con una promoción
        `package_price` de `min_qty` 3 activa sobre la variante, el carrito con 1
        o 2 unidades muestra `discounted_total = None` (precio normal); al llegar
        a 3, `discounted_total` refleja el precio de paquete."""
        db, table, ts, participant = self._seed_session()
        variant, _, _ = self._seed_variant(db, price=Decimal("6000"))
        promo = cart_fixtures.make_promotion(db, status="active")
        cart_fixtures.add_rule_to_promotion(
            db, promo, type="package_price", value=Decimal("16000"), min_qty=3,
            variants=[variant],
        )

        resp = service.add_item(
            db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=2)
        )
        self.assertIsNone(resp.discounted_total)  # no alcanza el grupo -> sin descuento

        resp = service.update_item(
            db, participant.id, resp.items[0].id, CartItemUpdate(quantity=3)
        )
        self.assertIsNotNone(resp.discounted_total)
        self.assertEqual(resp.discounted_total, Decimal("16000.00"))  # 18000 - 2000

    # spec 063 (FR-024, A-61): `test_serialize_cart_combo_no_recibe_descuento_adicional`
    # se elimina — el mecanismo de combo se retira.

    # --------------------------------------------------------------- list_my_orders (T018)

    def test_list_my_orders_mas_reciente_primero(self):
        """CONGELA comportamiento actual: `list_my_orders` devuelve los
        pedidos del comensal ordenados por `created_at` descendente (más
        reciente primero).

        Actualizado por spec 024-pagos-ordenes-mesa (FR-005/FR-006, Principio
        III): `order1` se marca `pagada` antes de enviar `order2` porque,
        desde esta spec, un comensal no puede tener dos órdenes activas a la
        vez — el propio orden de creación que este test verifica no cambia,
        solo se agrega el paso que finaliza la primera para poder enviar la
        segunda. Actualizado de nuevo por spec 025-revision-pago-antes-envio:
        `submit_cart` exige `payment_method_id` (el pedido nace con su
        primer intento de pago adjunto) — el resto de la suite sigue en
        verde."""
        db, table, ts, participant = self._seed_session()
        variant, _, _ = self._seed_variant(db)
        efectivo = self._seed_efectivo(db)

        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))
        order1 = service.submit_cart(db, participant, efectivo.id)
        order1.created_at = datetime(2020, 1, 1)
        order1.status = "pagada"
        db.commit()

        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))
        order2 = service.submit_cart(db, participant, efectivo.id)
        order2.created_at = datetime(2020, 1, 2)
        db.commit()

        orders = service.list_my_orders(db, participant.id)

        self.assertEqual([o.id for o in orders], [order2.id, order1.id])

    # ------------------------------------------------------------- cancel_my_order (T019)

    def test_cancel_my_order_recibida_se_cancela(self):
        """CONGELA comportamiento actual: un pedido en 'recibida' (sin
        ítems en cocina, porque aún no se confirmó) se cancela sin
        restricción.

        Actualizado por spec 025-revision-pago-antes-envio: `submit_cart`
        exige `payment_method_id`."""
        db, table, ts, participant = self._seed_session()
        variant, _, _ = self._seed_variant(db)
        efectivo = self._seed_efectivo(db)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))
        order = service.submit_cart(db, participant, efectivo.id)
        self.assertEqual(order.status, "recibida")

        cancelled = service.cancel_my_order(db, participant, order.id, "Me equivoqué de sabor")

        self.assertEqual(cancelled.status, "cancelada")

    def test_cancel_my_order_aceptado_para_cocina_sin_cobrar_409(self):
        """Comportamiento NUEVO: un pedido "aceptado para cocina" sin cobrar
        (`kitchen.accept_order_for_kitchen` -- `POST /orders/{id}/start-preparation`)
        queda con `status` aún en `'recibida'` pero sus ítems ya
        `en_preparacion`. Antes de este fix, `cancel_my_order` solo miraba
        `estado_cocina` cuando `status != 'recibida'`, así que el comensal
        podía seguir cancelando un pedido que cocina ya estaba preparando.
        Ahora responde 409 igual que `test_cancel_my_order_en_preparacion_409`."""
        db, table, ts, participant = self._seed_session()
        variant, _, _ = self._seed_variant(db)
        efectivo = self._seed_efectivo(db)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))
        order = service.submit_cart(db, participant, efectivo.id)
        self.assertEqual(order.status, "recibida")

        from app.api.v1.orders import kitchen
        kitchen.accept_order_for_kitchen(db, order.id)
        db.refresh(order)
        self.assertEqual(order.status, "recibida")

        with self.assertRaises(HTTPException) as ctx:
            service.cancel_my_order(db, participant, order.id, "Ya no quiero")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_cancel_my_order_en_preparacion_409(self):
        """CONGELA comportamiento actual: un pedido cuyos ítems ya están
        'en_preparacion' (staff ya confirmó y cocina ya empezó) responde 409
        al intento de cancelación del propio comensal, tal como documenta el
        docstring de `cancel_my_order`.

        Actualizado por spec 025-revision-pago-antes-envio: `submit_cart`
        exige `payment_method_id`."""
        db, table, ts, participant = self._seed_session()
        variant, _, _ = self._seed_variant(db)
        efectivo = self._seed_efectivo(db)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))
        order = service.submit_cart(db, participant, efectivo.id)
        order.status = "abierta"
        order.items[0].estado_cocina = "en_preparacion"
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.cancel_my_order(db, participant, order.id, "Ya no quiero")
        self.assertEqual(ctx.exception.status_code, 409)

    # --------------------------------------------------------------- leave_session (T020)

    def test_leave_session_cierra_participante_abandona_carrito_y_libera_mesa(self):
        """CONGELA comportamiento actual: `leave_session` cierra al
        comensal (`status='closed'`) y, al ser el último de la mesa sin nada
        que cobrar, libera la mesa en el acto (`try_release_if_empty`).
        `close_participants` sigue marcando el carrito `'abandonado'` como
        paso intermedio, pero spec 039 (`delete_orphan_carts`) lo elimina
        físicamente en la misma operación en la que la mesa queda `libre`."""
        db = cart_fixtures.new_session()
        table = cart_fixtures.make_dining_table(db, status="libre")
        resp = service.open_session(db, 1, table, "Ana")
        participant = db.get(SessionParticipant, resp.participant_id)

        service.leave_session(db, participant)

        self.assertEqual(participant.status, "closed")
        self.assertIsNone(db.get(Cart, resp.cart_id))
        self.assertEqual(table.status, "libre")

    # ------------------------------------------- leave_session: bloqueo de salida (spec 130, A-139)

    def test_leave_session_bloquea_con_pedido_propio_en_efectivo_sin_cobrar(self):
        """FR-009: un pedido propio en efectivo sin confirmar bloquea la
        salida — la sesión sigue activa, nada se cierra."""
        db, table, ts, participant = self._seed_session()
        variant, _, _ = self._seed_variant(db)
        efectivo = self._seed_efectivo(db)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))
        service.submit_cart(db, participant, efectivo.id)

        with self.assertRaises(PendingPaymentExit) as ctx:
            service.leave_session(db, participant)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(participant.status, "open")

    def test_leave_session_bloquea_con_pedido_propio_por_transferencia_sin_verificar(self):
        """FR-009/Clarificación Q1: también bloquea un pedido 100% por
        transferencia mientras el comprobante siga sin aprobar."""
        db, table, ts, participant = self._seed_session()
        variant, _, _ = self._seed_variant(db)
        nequi = cart_fixtures.make_payment_method(
            db, name=f"nequi-{uuid4()}", is_cash=False, type="transfer"
        )
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))
        with mock.patch("app.core.asset_refs.object_exists", return_value=True):
            service.submit_cart(
                db, participant, nequi.id, receipt_file_url="tenant_test/comprobantes/a.jpg",
                tenant_schema="tenant_test",
            )

        with self.assertRaises(PendingPaymentExit):
            service.leave_session(db, participant)
        self.assertEqual(participant.status, "open")

    def test_leave_session_bloquea_con_pedido_propio_mixto_con_una_sola_confirmacion(self):
        """FR-009: un pago mixto bloquea mientras falte cualquiera de las dos
        confirmaciones."""
        db, table, ts, participant = self._seed_session()
        variant, _, _ = self._seed_variant(db, price=Decimal("8000"))
        mixto = cart_fixtures.make_payment_method(
            db, name=f"mixto-{uuid4()}", is_cash=False, type="mixed"
        )
        cart_fixtures.make_payment_method(db, name=f"nequi-{uuid4()}", is_cash=False, type="transfer")
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))
        with mock.patch("app.core.asset_refs.object_exists", return_value=True):
            order = service.submit_cart(
                db, participant, mixto.id, receipt_file_url="tenant_test/comprobantes/a.jpg",
                tenant_schema="tenant_test", cash_amount=Decimal("3000"),
            )

        with self.assertRaises(PendingPaymentExit):
            service.leave_session(db, participant)

        # Se confirma solo una de las dos partes (directo en el modelo, sin
        # pasar por `orders/checkout.py` — alcance de este test es solo el
        # criterio de bloqueo de `leave_session`, no el gate de cocina).
        attempt = order.current_payment_attempt
        attempt.status = "confirmado"
        db.commit()

        with self.assertRaises(PendingPaymentExit):
            service.leave_session(db, participant)
        self.assertEqual(participant.status, "open")

    def test_leave_session_permite_salir_con_pedido_propio_completamente_confirmado(self):
        """FR-009: un pedido ya cobrado/confirmado no bloquea la salida."""
        db, table, ts, participant = self._seed_session()
        variant, _, _ = self._seed_variant(db)
        efectivo = self._seed_efectivo(db)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))
        order = service.submit_cart(db, participant, efectivo.id)
        order.current_payment_attempt.status = "confirmado"
        db.commit()

        service.leave_session(db, participant)
        self.assertEqual(participant.status, "closed")

    def test_leave_session_sin_pedidos_propios_permite_salir(self):
        """FR-009: solo carrito, sin ningún pedido enviado — sale con
        normalidad (mismo camino que el test ya existente arriba)."""
        db, table, ts, participant = self._seed_session()
        variant, _, _ = self._seed_variant(db)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))
        db.commit()

        service.leave_session(db, participant)
        self.assertEqual(participant.status, "closed")

    def test_leave_session_no_bloquea_por_pedido_pendiente_de_otro_comensal(self):
        """Clarificación Q2: el pedido pendiente de OTRO comensal de la misma
        mesa no bloquea la salida de quien no tiene pedidos propios."""
        db, table, ts, participant_ana = self._seed_session()
        participant_luis = cart_fixtures.make_participant(db, table_session=ts, display_name="Luis")
        variant, _, _ = self._seed_variant(db)
        efectivo = self._seed_efectivo(db)
        service.add_item(db, participant_luis.id, CartItemIn(product_variant_id=variant.id, quantity=1))
        service.submit_cart(db, participant_luis, efectivo.id)  # Luis: pago pendiente

        service.leave_session(db, participant_ana)  # Ana no tiene pedidos propios
        self.assertEqual(participant_ana.status, "closed")

        with self.assertRaises(PendingPaymentExit):
            service.leave_session(db, participant_luis)

    def test_leave_session_ignora_pedido_cancelado(self):
        """FR-009: un pedido propio ya CANCELADO no cuenta como pendiente."""
        db, table, ts, participant = self._seed_session()
        variant, _, _ = self._seed_variant(db)
        efectivo = self._seed_efectivo(db)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))
        order = service.submit_cart(db, participant, efectivo.id)
        order.status = "cancelada"
        db.commit()

        service.leave_session(db, participant)
        self.assertEqual(participant.status, "closed")

    # ----------------------------------------------------------------- submit_cart (T021)

    def test_submit_cart_elimina_carrito_y_abre_uno_nuevo_tras_pedido(self):
        """CONGELA comportamiento corregido — spec 038 (FR-003/FR-004): un
        carrito con ítems se confirma en una `CustomerOrder` 'recibida' y el
        carrito del participante se ELIMINA físicamente (no queda ninguna
        fila, ni 'confirmado' ni de ningún otro status) en la misma
        transacción del pedido. Antes de esta spec la fila quedaba
        `status='confirmado'`, huérfana para siempre (research.md Decisión
        9) — renombrado desde
        `test_submit_cart_confirma_pedido_y_abre_carrito_nuevo`.

        Actualizado por spec 025-revision-pago-antes-envio: `submit_cart`
        exige `payment_method_id` — el pedido nace con su primer intento de
        pago adjunto (`current_payment_attempt`)."""
        db, table, ts, participant = self._seed_session()
        variant, _, _ = self._seed_variant(db, price=Decimal("4000"))
        efectivo = self._seed_efectivo(db)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=2))
        old_cart = db.execute(
            select(Cart).where(Cart.participant_id == participant.id, Cart.status == "abierto")
        ).scalar_one()

        order = service.submit_cart(db, participant, efectivo.id)

        self.assertIsInstance(order, CustomerOrder)
        self.assertEqual(order.status, "recibida")
        self.assertEqual(len(order.items), 1)
        self.assertEqual(order.items[0].quantity, 2)
        self.assertEqual(order.current_payment_attempt.payment_method_id, efectivo.id)
        self.assertEqual(order.current_payment_attempt.status, "pendiente")

        # spec 038, FR-003/FR-004: la fila y sus CartItem/CartItemOption ya
        # no existen — borrado físico, no archivado.
        self.assertIsNone(db.get(Cart, old_cart.id))

        # `submit_cart` no abre el carrito siguiente por sí solo: lo hace
        # `_get_or_create_open_cart` en la próxima operación del comensal
        # (aquí, `get_cart`) — posible sin violar el índice único parcial
        # porque el fixture lo removió (research.md §3).
        cart_resp = service.get_cart(db, participant.id)
        new_cart = db.execute(
            select(Cart).where(Cart.participant_id == participant.id, Cart.status == "abierto")
        ).scalar_one()
        self.assertNotEqual(new_cart.id, old_cart.id)
        self.assertEqual(cart_resp.items, [])

        # spec 038, US3 (Acceptance Scenario 1): agregar un ítem nuevo tras
        # confirmar no arrastra ninguna línea de la ronda ya confirmada — el
        # carrito de la segunda ronda contiene únicamente lo que se agrega
        # ahora.
        variant2, _, _ = self._seed_variant(db, price=Decimal("2500"))
        second_round = service.add_item(
            db, participant.id, CartItemIn(product_variant_id=variant2.id, quantity=1)
        )
        self.assertEqual(len(second_round.items), 1)
        self.assertEqual(second_round.items[0].product_variant_id, variant2.id)

    def test_submit_cart_snapshot_de_descuento_coincide_con_el_carrito(self):
        """spec 038, US1 (Acceptance Scenario 3 / CA-6, SC-005): el snapshot
        de descuento persistido en los `OrderItem` coincide exactamente con
        lo que `serialize_cart`/`GET /cart` mostraba justo antes de
        confirmar — la línea con promoción activa trae su
        `discounted_unit_price`/`discounted_line_total`, la otra queda en
        `None` (mismo motor, función compartida, research.md Decisión 4)."""
        db, table, ts, participant = self._seed_session()
        variant1, product, category = self._seed_variant(db, price=Decimal("10000"))
        variant2, _, _ = self._seed_variant(db, price=Decimal("5000"))
        promo = cart_fixtures.make_promotion(db, status="active")
        cart_fixtures.add_rule_to_promotion(
            db, promo, type="percent", value=Decimal("10"), min_qty=1,
            variants=[variant1],
        )
        efectivo = self._seed_efectivo(db)

        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant1.id, quantity=1))
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant2.id, quantity=1))

        order = service.submit_cart(db, participant, efectivo.id)

        self.assertEqual(len(order.items), 2)
        with_promo = next(it for it in order.items if it.product_variant_id == variant1.id)
        without_promo = next(it for it in order.items if it.product_variant_id == variant2.id)
        self.assertEqual(with_promo.discounted_unit_price, Decimal("9000.00"))
        self.assertEqual(with_promo.discounted_line_total, Decimal("9000.00"))
        self.assertIsNone(without_promo.discounted_unit_price)
        self.assertIsNone(without_promo.discounted_line_total)

    def test_submit_cart_congela_el_instante_de_vigencia_de_promociones(self):
        """spec 073, FR-018 (A-70): el flujo del carrito QR también congela el
        instante de vigencia al confirmar el pedido — `CustomerOrder.
        promotion_evaluated_at` queda poblado (≈ hora de la confirmación),
        igual que un pedido de mostrador. Paridad de FR-018."""
        db, table, ts, participant = self._seed_session()
        variant, _, _ = self._seed_variant(db, price=Decimal("4000"))
        efectivo = self._seed_efectivo(db)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))

        antes = datetime.now(timezone.utc)
        order = service.submit_cart(db, participant, efectivo.id)
        despues = datetime.now(timezone.utc)

        self.assertIsNotNone(order.promotion_evaluated_at)
        instante = order.promotion_evaluated_at
        if instante.tzinfo is None:
            instante = instante.replace(tzinfo=timezone.utc)
        self.assertLessEqual(antes, instante)
        self.assertLessEqual(instante, despues)

    def test_submit_cart_fallo_en_transaccion_no_toca_el_carrito(self):
        """spec 038, US1 (Acceptance Scenario 4 / CA-8, FR-004): si la
        creación del pedido falla dentro de la transacción (aquí, forzando
        un error al resolver el snapshot de descuento — FR-005/research.md
        Decisión 5), el `Cart`/`CartItem[]` del participante siguen
        existiendo intactos (mismo `id`, mismas líneas) y no se creó ningún
        `CustomerOrder` — el `rollback()` deshace tanto el pedido a medias
        como el borrado del carrito."""
        db, table, ts, participant = self._seed_session()
        variant, _, _ = self._seed_variant(db, price=Decimal("4000"))
        efectivo = self._seed_efectivo(db)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=2))
        cart = db.execute(
            select(Cart).where(Cart.participant_id == participant.id, Cart.status == "abierto")
        ).scalar_one()
        cart_id = cart.id
        item_ids = {it.id for it in cart.items}

        with mock.patch(
            "app.api.v1.cart.service.promotions.evaluate_variant_sets",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                service.submit_cart(db, participant, efectivo.id)

        persisted = db.get(Cart, cart_id)
        self.assertIsNotNone(persisted)
        self.assertEqual({it.id for it in persisted.items}, item_ids)
        orders = db.execute(
            select(CustomerOrder).where(CustomerOrder.participant_id == participant.id)
        ).scalars().all()
        self.assertEqual(orders, [])

    def test_submit_cart_segunda_ronda_crea_orden_independiente(self):
        """spec 038, US3 (Acceptance Scenario 2): confirmar el carrito de la
        segunda ronda crea un segundo `CustomerOrder` independiente del
        primero, con ambos coexistiendo con sus respectivas líneas. Usa dos
        participantes para no chocar con el 409 de "orden activa" de FR-008
        (esa garantía es por participante, no por mesa)."""
        db, table, ts, participant1 = self._seed_session()
        participant2 = cart_fixtures.make_participant(db, table_session=ts)
        variant1, _, _ = self._seed_variant(db, price=Decimal("4000"))
        variant2, _, _ = self._seed_variant(db, price=Decimal("3000"))
        efectivo = self._seed_efectivo(db)

        service.add_item(db, participant1.id, CartItemIn(product_variant_id=variant1.id, quantity=1))
        order1 = service.submit_cart(db, participant1, efectivo.id)

        service.add_item(db, participant2.id, CartItemIn(product_variant_id=variant2.id, quantity=1))
        order2 = service.submit_cart(db, participant2, efectivo.id)

        self.assertNotEqual(order1.id, order2.id)
        orders = db.execute(
            select(CustomerOrder).where(CustomerOrder.table_session_id == ts.id)
        ).scalars().all()
        self.assertEqual({o.id for o in orders}, {order1.id, order2.id})
        self.assertEqual(len(order1.items), 1)
        self.assertEqual(len(order2.items), 1)

    def test_submit_cart_no_afecta_el_carrito_de_otro_comensal_de_la_mesa(self):
        """spec 038, US4 (Acceptance Scenario 1 / CA-5, FR-006): confirmar
        el pedido de un comensal no toca el carrito de otro comensal de la
        misma mesa — el borrado es estrictamente por `participant_id`."""
        db, table, ts, ana = self._seed_session()
        beto = cart_fixtures.make_participant(db, table_session=ts)
        variant_ana, _, _ = self._seed_variant(db, price=Decimal("4000"))
        variant_beto, _, _ = self._seed_variant(db, price=Decimal("3000"))
        efectivo = self._seed_efectivo(db)

        service.add_item(db, ana.id, CartItemIn(product_variant_id=variant_ana.id, quantity=1))
        service.add_item(db, beto.id, CartItemIn(product_variant_id=variant_beto.id, quantity=2))
        beto_cart = db.execute(
            select(Cart).where(Cart.participant_id == beto.id, Cart.status == "abierto")
        ).scalar_one()
        beto_cart_id = beto_cart.id
        beto_item_ids = {it.id for it in beto_cart.items}

        service.submit_cart(db, ana, efectivo.id)

        persisted = db.get(Cart, beto_cart_id)
        self.assertIsNotNone(persisted)
        self.assertEqual({it.id for it in persisted.items}, beto_item_ids)
        self.assertEqual(len(persisted.items), 1)
        self.assertEqual(persisted.items[0].quantity, 2)

    def test_submit_cart_vacio_409(self):
        """CONGELA comportamiento actual: enviar un carrito sin ítems
        responde 409 ("El carrito está vacío") — se evalúa antes que
        cualquier validación de pago (contracts/submit-cart-with-payment.md,
        spec 025)."""
        db, table, ts, participant = self._seed_session()
        cart_fixtures.make_cart(db, participant=participant)

        with self.assertRaises(HTTPException) as ctx:
            service.submit_cart(db, participant, uuid4())
        self.assertEqual(ctx.exception.status_code, 409)


class TestQrAddonsPerLine(unittest.TestCase):
    """spec 089 (A-94): en el Menú QR el adicional (opción de un grupo con recargo) se
    cobra y se consume una vez por línea: 2 hamburguesas de $15.000 con 1 tocino de
    $3.000 cuestan $33.000, no $36.000. La marca queda en la fila (`per_line`) para
    que lectura, consumo y cobro no dependan del `pricing_type` vigente."""

    def setUp(self):
        self.db = cart_fixtures.new_session()
        table = cart_fixtures.make_dining_table(self.db)
        ts = cart_fixtures.make_table_session(self.db, table=table)
        self.participant = cart_fixtures.make_participant(self.db, table_session=ts)
        category = cart_fixtures.make_category(self.db)
        self.product = cart_fixtures.make_product(self.db, category=category)
        self.variant = cart_fixtures.make_variant(
            self.db, product=self.product, price=Decimal("15000")
        )
        # Grupo de adicionales (con recargo) y grupo de sabores (incluido).
        self.extras = cart_fixtures.make_option_group(
            self.db, pricing_type="con_recargo", selection_mode="cantidad",
            min_select=0, max_select=3,
        )
        self.tocino = cart_fixtures.make_option(
            self.db, group=self.extras, extra_price=Decimal("3000")
        )
        cart_fixtures.link_variant_group(self.db, self.variant, self.extras, min_select=0, max_select=3)
        self.sabores = cart_fixtures.make_option_group(
            self.db, pricing_type="incluido", min_select=0, max_select=2
        )
        self.queso = cart_fixtures.make_option(self.db, group=self.sabores, extra_price=Decimal("0"))
        cart_fixtures.link_variant_group(self.db, self.variant, self.sabores, min_select=0, max_select=2)

    def _add(self, quantity=2, options=(), variant=None):
        variant = variant or self.variant
        return service.add_item(
            self.db, self.participant.id,
            CartItemIn(
                product_variant_id=variant.id, quantity=quantity,
                options=[OptionSelectionIn(option_id=o.id) for o in options],
            ),
        )

    def _cart_item(self):
        from app.models.cart_item import CartItem
        return self.db.execute(select(CartItem)).scalars().one()

    def test_dos_hamburguesas_con_un_tocino_cuestan_33000(self):
        resp = self._add(2, [self.tocino])
        item = resp.items[0]
        self.assertEqual(item.unit_price, Decimal("15000"))
        self.assertEqual(item.addons_total, Decimal("3000"))
        self.assertEqual(item.line_total, Decimal("33000"))
        self.assertEqual(resp.total, Decimal("33000"))

    def test_subir_la_cantidad_no_multiplica_el_adicional(self):
        resp = self._add(2, [self.tocino])
        resp = service.update_item(
            self.db, self.participant.id, resp.items[0].id, CartItemUpdate(quantity=3)
        )
        self.assertEqual(resp.items[0].line_total, Decimal("48000"))
        self.assertEqual(resp.items[0].addons_total, Decimal("3000"))

    def test_adicional_con_dos_unidades_en_una_sola_hamburguesa(self):
        resp = service.add_item(
            self.db, self.participant.id,
            CartItemIn(
                product_variant_id=self.variant.id, quantity=1,
                options=[OptionSelectionIn(option_id=self.tocino.id, quantity=2)],
            ),
        )
        self.assertEqual(resp.items[0].addons_total, Decimal("6000"))
        self.assertEqual(resp.items[0].line_total, Decimal("21000"))

    def test_grupo_incluido_no_es_adicional_y_sigue_por_unidad(self):
        self._add(2, [self.queso])
        row = self._cart_item()
        self.assertEqual(row.addons_total, Decimal("0"))
        self.assertFalse(row.options[0].per_line)

    def test_se_marca_per_line_solo_en_grupos_con_recargo(self):
        self._add(2, [self.tocino, self.queso])
        row = self._cart_item()
        marks = {o.option_id: o.per_line for o in row.options}
        self.assertEqual(marks, {self.tocino.id: True, self.queso.id: False})

    def test_bandera_apagada_crea_la_linea_con_la_regla_historica(self):
        """`QR_ADDONS_PER_LINE=false`: la línea nueva vuelve a meter los extras dentro de
        `unit_price` y sin `per_line`, pero lo ya creado no se invalida.

        Spec 117 (A-126): `self.extras`/`self.tocino` es "cantidad" además de
        "con_recargo" -- con ese grupo, el eje nuevo (que nunca se apaga con esta
        bandera, D1) seguiría marcando `per_line=True` sin importar la bandera, así que
        ya no sirve para demostrar la reversa del eje 089 en aislamiento. Se usa un
        grupo nuevo, puramente del eje 089 (con_recargo + "conteo"), solo para este test."""
        recargo_conteo = cart_fixtures.make_option_group(
            self.db, pricing_type="con_recargo", selection_mode="conteo",
            min_select=0, max_select=1,
        )
        tocino_conteo = cart_fixtures.make_option(
            self.db, group=recargo_conteo, extra_price=Decimal("3000")
        )
        cart_fixtures.link_variant_group(
            self.db, self.variant, recargo_conteo, min_select=0, max_select=1
        )
        with mock.patch.object(service.settings, "QR_ADDONS_PER_LINE", False):
            resp = self._add(2, [tocino_conteo])
        item = resp.items[0]
        self.assertEqual(item.unit_price, Decimal("18000"))
        self.assertEqual(item.addons_total, Decimal("0"))
        self.assertEqual(item.line_total, Decimal("36000"))
        self.assertFalse(item.options[0].per_line)

    def test_promocion_del_20_por_ciento_no_descuenta_el_adicional(self):
        promo = cart_fixtures.make_promotion(self.db, status="active")
        cart_fixtures.add_rule_to_promotion(
            self.db, promo, type="percent", value=Decimal("20"), min_qty=1,
            variants=[self.variant],
        )
        resp = self._add(2, [self.tocino])
        item = resp.items[0]
        # 20 % solo sobre 2 × 15.000 = 30.000 → 6.000; el tocino ($3.000) no se descuenta.
        self.assertEqual(item.discounted_line_total, Decimal("27000.00"))
        self.assertEqual(item.discounted_unit_price, Decimal("12000.00"))
        self.assertEqual(resp.discounted_total, Decimal("27000.00"))

    def test_linea_historica_y_linea_nueva_conviven_en_el_mismo_carrito(self):
        from app.models.cart_item import CartItem, CartItemOption
        cart = cart_fixtures.make_cart(self.db, participant=self.participant)
        # Histórica: 2 × 18.000 con el tocino ya dentro de `unit_price`, sin marcas.
        hist = cart_fixtures.make_cart_item(
            self.db, cart, self.variant, quantity=2, unit_price=Decimal("18000")
        )
        self.db.add(CartItemOption(cart_item_id=hist.id, option_id=self.tocino.id, quantity=1))
        self.db.flush()
        resp = self._add(2, [self.tocino])
        totals = sorted(i.line_total for i in resp.items)
        self.assertEqual(totals, [Decimal("33000"), Decimal("36000")])
        self.assertEqual(resp.total, Decimal("69000"))

    def test_editar_la_linea_recalcula_los_adicionales_y_las_marcas(self):
        resp = self._add(2, [self.tocino])
        resp = service.update_item(
            self.db, self.participant.id, resp.items[0].id,
            CartItemUpdate(options=[OptionSelectionIn(option_id=self.tocino.id, quantity=2)]),
        )
        item = resp.items[0]
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.addons_total, Decimal("6000"))
        self.assertEqual(item.line_total, Decimal("36000"))
        self.assertTrue(item.options[0].per_line)

    def test_cambiar_solo_la_cantidad_conserva_las_marcas_de_la_fila(self):
        """Una línea histórica (sin `per_line`) sigue con la regla histórica al cambiar
        la cantidad; no se reinterpreta con el `pricing_type` vigente."""
        from app.models.cart_item import CartItemOption
        cart = cart_fixtures.make_cart(self.db, participant=self.participant)
        hist = cart_fixtures.make_cart_item(
            self.db, cart, self.variant, quantity=2, unit_price=Decimal("18000")
        )
        self.db.add(CartItemOption(cart_item_id=hist.id, option_id=self.tocino.id, quantity=1))
        self.db.flush()
        resp = service.update_item(self.db, self.participant.id, hist.id, CartItemUpdate(quantity=3))
        self.assertEqual(resp.items[0].unit_price, Decimal("18000"))
        self.assertEqual(resp.items[0].addons_total, Decimal("0"))
        self.assertEqual(resp.items[0].line_total, Decimal("54000"))

    def test_submit_cart_copia_addons_total_y_marcas_sin_recalcular(self):
        efectivo = cart_fixtures.make_payment_method(self.db, name="efe", is_cash=True)
        self._add(2, [self.tocino, self.queso])
        order = service.submit_cart(self.db, self.participant, efectivo.id)
        [item] = order.items
        self.assertEqual(item.unit_price, Decimal("15000"))
        self.assertEqual(item.addons_total, Decimal("3000"))
        self.assertEqual(item.line_total, Decimal("33000"))
        marks = {o.option_id: o.per_line for o in item.options}
        self.assertEqual(marks, {self.tocino.id: True, self.queso.id: False})

    def test_submit_cart_con_promocion_guarda_el_descuento_que_no_toca_el_adicional(self):
        promo = cart_fixtures.make_promotion(self.db, status="active")
        cart_fixtures.add_rule_to_promotion(
            self.db, promo, type="percent", value=Decimal("20"), min_qty=1,
            variants=[self.variant],
        )
        efectivo = cart_fixtures.make_payment_method(self.db, name="efe", is_cash=True)
        self._add(2, [self.tocino])
        order = service.submit_cart(self.db, self.participant, efectivo.id)
        [item] = order.items
        self.assertEqual(item.discounted_line_total, Decimal("27000.00"))
        self.assertEqual(item.discounted_unit_price, Decimal("12000.00"))

    def test_una_linea_de_combo_con_adicional_suma_bien_y_conserva_el_combo(self):
        promo = cart_fixtures.make_promotion(self.db, status="active")
        cart = cart_fixtures.make_cart(self.db, participant=self.participant)
        combo_item = cart_fixtures.make_cart_item(
            self.db, cart, self.variant, quantity=2, unit_price=Decimal("12000"),
            addons_total=Decimal("3000"), combo_id=promo.id,
        )
        efectivo = cart_fixtures.make_payment_method(self.db, name="efe", is_cash=True)
        resp = service.get_cart(self.db, self.participant.id)
        self.assertEqual(resp.items[0].line_total, Decimal("27000"))
        order = service.submit_cart(self.db, self.participant, efectivo.id)
        self.assertEqual(order.items[0].combo_id, promo.id)
        self.assertEqual(order.items[0].addons_total, Decimal("3000"))

    def test_disponibilidad_del_carrito_descuenta_el_adicional_una_sola_vez(self):
        """`_cart_consumption` lee `per_line` de la fila: 2 hamburguesas con 1 tocino
        (30 g) requieren 30 g, no 60 g."""
        insumo = cart_fixtures.make_inventory_item(
            self.db, name="tocino", current_stock=Decimal("100")
        )
        self.tocino.inventory_item_id = insumo.id
        self.tocino.item_quantity = Decimal("30")
        self.db.flush()
        self._add(2, [self.tocino])
        cart = self.db.execute(select(Cart)).scalars().one()
        need = service._cart_consumption(self.db, cart)
        self.assertEqual(need, {insumo.id: Decimal("30")})

    def test_grupo_cantidad_incluido_marca_per_line_igual_que_con_recargo(self):
        """Spec 117 (A-126): un grupo "Por cantidad" se cobra por línea sin importar su
        `pricing_type` -- antes de esta spec, solo marcaba `per_line` un grupo con
        recargo (089); un grupo "cantidad" + "incluido" (ej. gomitas sin costo extra)
        quedaba, incorrectamente, por unidad de producto."""
        gomitas_grupo = cart_fixtures.make_option_group(
            self.db, pricing_type="incluido", selection_mode="cantidad",
            min_select=0, max_select=1,
        )
        gomitas = cart_fixtures.make_option(
            self.db, group=gomitas_grupo, extra_price=Decimal("1000")
        )
        cart_fixtures.link_variant_group(
            self.db, self.variant, gomitas_grupo, min_select=0, max_select=1
        )
        resp = self._add(2, [gomitas])
        item = resp.items[0]
        self.assertEqual(item.unit_price, Decimal("15000"))
        self.assertEqual(item.addons_total, Decimal("1000"))
        self.assertEqual(item.line_total, Decimal("31000"))
        self.assertTrue(item.options[0].per_line)

    def test_grupo_cantidad_marca_per_line_incluso_con_la_bandera_089_apagada(self):
        """A diferencia de la regla 089 (que si se apaga con `QR_ADDONS_PER_LINE=False`
        no marca nada), el eje nuevo de `selection_mode='cantidad'` nunca se apaga con
        esa bandera -- gobierna solo su propio eje (research.md D1)."""
        gomitas_grupo = cart_fixtures.make_option_group(
            self.db, pricing_type="incluido", selection_mode="cantidad",
            min_select=0, max_select=1,
        )
        gomitas = cart_fixtures.make_option(
            self.db, group=gomitas_grupo, extra_price=Decimal("1000")
        )
        cart_fixtures.link_variant_group(
            self.db, self.variant, gomitas_grupo, min_select=0, max_select=1
        )
        with mock.patch.object(service.settings, "QR_ADDONS_PER_LINE", False):
            resp = self._add(2, [gomitas])
        item = resp.items[0]
        self.assertEqual(item.unit_price, Decimal("15000"))
        self.assertEqual(item.addons_total, Decimal("1000"))
        self.assertEqual(item.line_total, Decimal("31000"))
        self.assertTrue(item.options[0].per_line)


class TestQrEditAddons(unittest.TestCase):
    """spec 089 (Historia 3, contracts/addons-per-line.md §4): editar o quitar los adicionales de
    una línea ya agregada (`PATCH /cart/items/{id}`), con el total recalculado por la regla nueva,
    sin eliminar la línea y sin tocar la de otro comensal."""

    def setUp(self):
        self.db = cart_fixtures.new_session()
        table = cart_fixtures.make_dining_table(self.db)
        self.ts = cart_fixtures.make_table_session(self.db, table=table)
        self.ana = cart_fixtures.make_participant(self.db, table_session=self.ts, display_name="Ana")
        self.beto = cart_fixtures.make_participant(self.db, table_session=self.ts, display_name="Beto")
        category = cart_fixtures.make_category(self.db)
        product = cart_fixtures.make_product(self.db, category=category)
        self.variant = cart_fixtures.make_variant(self.db, product=product, price=Decimal("15000"))
        self.extras = cart_fixtures.make_option_group(
            self.db, pricing_type="con_recargo", selection_mode="cantidad",
            min_select=0, max_select=3, max_quantity_per_option=2,
        )
        self.tocino = cart_fixtures.make_option(self.db, group=self.extras, extra_price=Decimal("3000"))
        self.queso = cart_fixtures.make_option(self.db, group=self.extras, extra_price=Decimal("2000"))
        cart_fixtures.link_variant_group(self.db, self.variant, self.extras, min_select=0, max_select=3)

    def _add(self, participant, *options, quantity=2):
        return service.add_item(
            self.db, participant.id,
            CartItemIn(
                product_variant_id=self.variant.id, quantity=quantity,
                options=[OptionSelectionIn(option_id=o.id) for o in options],
            ),
        )

    def _patch(self, participant, item_id, **kw):
        return service.update_item(self.db, participant.id, item_id, CartItemUpdate(**kw))

    def test_cambiar_de_adicional_reemplaza_la_seleccion_y_recalcula(self):
        resp = self._add(self.ana, self.tocino)
        resp = self._patch(self.ana, resp.items[0].id, options=[OptionSelectionIn(option_id=self.queso.id)])
        item = resp.items[0]
        self.assertEqual([o.option_id for o in item.options], [self.queso.id])
        self.assertEqual(item.addons_total, Decimal("2000"))
        self.assertEqual(item.line_total, Decimal("32000"))
        self.assertEqual(item.quantity, 2)

    def test_quitar_todos_los_adicionales_deja_la_linea_sin_eliminarla(self):
        resp = self._add(self.ana, self.tocino)
        resp = self._patch(self.ana, resp.items[0].id, options=[])
        self.assertEqual(len(resp.items), 1)
        self.assertEqual(resp.items[0].options, [])
        self.assertEqual(resp.items[0].addons_total, Decimal("0"))
        self.assertEqual(resp.items[0].line_total, Decimal("30000"))

    def test_una_opcion_con_cantidad_cero_no_es_valida_asi_que_quitarla_es_no_enviarla(self):
        """El contrato rechaza `quantity < 1`: nunca existe una fila "x0"; quitar un adicional es
        no incluirlo en `options`."""
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            OptionSelectionIn(option_id=self.tocino.id, quantity=0)

    def test_seleccion_que_viola_el_tope_por_opcion_es_422_con_el_mensaje_del_grupo(self):
        """Spec 117 (A-126, D2): el tope de `self.extras` (`max_quantity_per_option=2`) se
        escala por la cantidad de producto de la línea (`quantity=2` en `_add`) a `2×2=4`
        -- `quantity=3` ya no lo viola (antes de esta spec sí, con el tope fijo); se usa
        `quantity=5`, por encima incluso del tope ya escalado, para seguir demostrando el
        422 del mensaje del grupo."""
        resp = self._add(self.ana, self.tocino)
        with self.assertRaises(HTTPException) as ctx:
            self._patch(
                self.ana, resp.items[0].id,
                options=[OptionSelectionIn(option_id=self.tocino.id, quantity=5)],
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("máximo", str(ctx.exception.detail))
        # La línea no cambió.
        self.assertEqual(service.get_cart(self.db, self.ana.id).items[0].addons_total, Decimal("3000"))

    def test_adicional_inactivo_es_422(self):
        resp = self._add(self.ana, self.tocino)
        self.queso.active = False
        self.db.flush()
        with self.assertRaises(HTTPException) as ctx:
            self._patch(self.ana, resp.items[0].id, options=[OptionSelectionIn(option_id=self.queso.id)])
        self.assertEqual(ctx.exception.status_code, 422)

    def test_adicional_sin_stock_es_409(self):
        resp = self._add(self.ana, self.tocino)
        insumo = cart_fixtures.make_inventory_item(self.db, name="queso", current_stock=Decimal("0"))
        self.queso.inventory_item_id = insumo.id
        self.queso.item_quantity = Decimal("30")
        self.db.flush()
        with self.assertRaises(HTTPException) as ctx:
            self._patch(self.ana, resp.items[0].id, options=[OptionSelectionIn(option_id=self.queso.id)])
        self.assertEqual(ctx.exception.status_code, 409)

    def test_un_comensal_no_puede_editar_la_linea_de_otro(self):
        resp = self._add(self.ana, self.tocino)
        with self.assertRaises(HTTPException) as ctx:
            self._patch(self.beto, resp.items[0].id, options=[])
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(service.get_cart(self.db, self.ana.id).items[0].addons_total, Decimal("3000"))

    def test_una_linea_ya_enviada_no_se_puede_editar(self):
        """Al enviar el carrito se elimina físicamente: la línea ya no existe para el comensal."""
        efectivo = cart_fixtures.make_payment_method(self.db, name="efe", is_cash=True)
        resp = self._add(self.ana, self.tocino)
        item_id = resp.items[0].id
        service.submit_cart(self.db, self.ana, efectivo.id)
        with self.assertRaises(HTTPException) as ctx:
            self._patch(self.ana, item_id, options=[])
        self.assertEqual(ctx.exception.status_code, 404)

    def test_editar_dos_lineas_hasta_que_queden_identicas_no_las_fusiona(self):
        self._add(self.ana, self.tocino)
        resp = self._add(self.ana, self.queso)
        a, b = resp.items
        resp = self._patch(self.ana, b.id, options=[OptionSelectionIn(option_id=self.tocino.id)])
        self.assertEqual(len(resp.items), 2)
        self.assertEqual({i.id for i in resp.items}, {a.id, b.id})
        self.assertEqual(resp.total, Decimal("66000"))  # 2 × (2 × 15.000 + 3.000)

    def test_editar_solo_las_notas_conserva_los_adicionales(self):
        resp = self._add(self.ana, self.tocino)
        resp = self._patch(self.ana, resp.items[0].id, notes="sin cebolla")
        self.assertEqual(resp.items[0].notes, "sin cebolla")
        self.assertEqual(resp.items[0].addons_total, Decimal("3000"))


class TestAutoAdjustCantidadOptions(unittest.TestCase):
    """Spec 117 (A-126, D3/D4/D5, contracts/cart-item-auto-adjust.md): al reducir la
    cantidad de producto de una línea con adiciones "Por cantidad" ya elegidas, si
    exceden el nuevo tope escalado, `update_item` las reduce automáticamente y lo
    reporta en `adjusted_options`. Único camino de backend donde esto puede pasar
    (research.md D3): `PATCH` con solo `quantity` -- hoy ese `PATCH` no revalida nada
    (línea 441-446 antes de esta spec)."""

    def setUp(self):
        self.db = cart_fixtures.new_session()
        table = cart_fixtures.make_dining_table(self.db)
        ts = cart_fixtures.make_table_session(self.db, table=table)
        self.participant = cart_fixtures.make_participant(self.db, table_session=ts)
        category = cart_fixtures.make_category(self.db)
        product = cart_fixtures.make_product(self.db, category=category)
        self.variant = cart_fixtures.make_variant(
            self.db, product=product, price=Decimal("15000")
        )

    def _add(self, *, quantity, options):
        return service.add_item(
            self.db, self.participant.id,
            CartItemIn(
                product_variant_id=self.variant.id, quantity=quantity,
                options=[
                    OptionSelectionIn(option_id=o.id, quantity=q) for o, q in options
                ],
            ),
        )

    def _patch_quantity(self, item_id, quantity):
        return service.update_item(
            self.db, self.participant.id, item_id, CartItemUpdate(quantity=quantity)
        )

    def test_excede_max_quantity_per_option_se_reduce_al_tope_ya_escalado(self):
        group = cart_fixtures.make_option_group(
            self.db, selection_mode="cantidad", pricing_type="incluido",
            min_select=0, max_select=1, max_quantity_per_option=1,
        )
        gomitas = cart_fixtures.make_option(
            self.db, group=group, extra_price=Decimal("4000")
        )
        cart_fixtures.link_variant_group(
            self.db, self.variant, group, min_select=0, max_select=1
        )
        resp = self._add(quantity=2, options=[(gomitas, 2)])  # tope 1×2=2: válido

        resp = self._patch_quantity(resp.items[0].id, 1)  # nuevo tope 1×1=1

        item = resp.items[0]
        self.assertEqual(item.options[0].quantity, 1)
        self.assertEqual(item.unit_price, Decimal("15000"))
        self.assertEqual(item.addons_total, Decimal("4000"))
        self.assertEqual(item.line_total, Decimal("19000"))
        self.assertEqual(len(item.adjusted_options), 1)
        adj = item.adjusted_options[0]
        self.assertEqual(adj.option_name, gomitas.name)
        self.assertEqual(adj.previous_quantity, 2)
        self.assertEqual(adj.new_quantity, 1)

    def test_excede_max_total_quantity_reduce_en_orden_alfabetico_descendente(self):
        """Mismo ejemplo del contrato: "Gomitas" ×2 + "Chocolate" ×1 (suma 3, dentro
        del tope 2×2=4 al crear la línea con cantidad 2); al reducir a 1, nuevo tope
        2×1=2 -- se reduce "Gomitas" (alfabéticamente después de "Chocolate") de 2 a
        1, sin tocar "Chocolate"."""
        group = cart_fixtures.make_option_group(
            self.db, selection_mode="cantidad", pricing_type="incluido",
            min_select=0, max_select=2, max_total_quantity=2,
        )
        gomitas = cart_fixtures.make_option(self.db, group=group, name="Gomitas", extra_price=Decimal("1000"))
        chocolate = cart_fixtures.make_option(self.db, group=group, name="Chocolate", extra_price=Decimal("1500"))
        cart_fixtures.link_variant_group(
            self.db, self.variant, group, min_select=0, max_select=2
        )
        resp = self._add(quantity=2, options=[(gomitas, 2), (chocolate, 1)])

        resp = self._patch_quantity(resp.items[0].id, 1)

        item = resp.items[0]
        por_opcion = {o.option_id: o.quantity for o in item.options}
        self.assertEqual(por_opcion[gomitas.id], 1)
        self.assertEqual(por_opcion[chocolate.id], 1)
        self.assertEqual(len(item.adjusted_options), 1)
        self.assertEqual(item.adjusted_options[0].option_name, "Gomitas")
        self.assertEqual(item.adjusted_options[0].previous_quantity, 2)
        self.assertEqual(item.adjusted_options[0].new_quantity, 1)

    def test_grupo_cantidad_sin_topes_nunca_dispara_el_ajuste(self):
        group = cart_fixtures.make_option_group(
            self.db, selection_mode="cantidad", pricing_type="incluido",
            min_select=0, max_select=1,
        )
        gomitas = cart_fixtures.make_option(self.db, group=group, extra_price=Decimal("1000"))
        cart_fixtures.link_variant_group(
            self.db, self.variant, group, min_select=0, max_select=1
        )
        resp = self._add(quantity=2, options=[(gomitas, 99)])

        resp = self._patch_quantity(resp.items[0].id, 1)

        self.assertEqual(resp.items[0].options[0].quantity, 99)
        self.assertEqual(resp.items[0].adjusted_options, [])

    def test_get_cart_tras_el_ajuste_no_repite_adjusted_options(self):
        group = cart_fixtures.make_option_group(
            self.db, selection_mode="cantidad", pricing_type="incluido",
            min_select=0, max_select=1, max_quantity_per_option=1,
        )
        gomitas = cart_fixtures.make_option(self.db, group=group, extra_price=Decimal("4000"))
        cart_fixtures.link_variant_group(
            self.db, self.variant, group, min_select=0, max_select=1
        )
        resp = self._add(quantity=2, options=[(gomitas, 2)])
        item_id = resp.items[0].id
        resp = self._patch_quantity(item_id, 1)
        self.assertEqual(len(resp.items[0].adjusted_options), 1)  # disparó el ajuste

        reread = service.get_cart(self.db, self.participant.id)
        self.assertEqual(reread.items[0].options[0].quantity, 1)  # ya ajustada
        self.assertEqual(reread.items[0].adjusted_options, [])  # nunca se repite

        # Un PATCH posterior que no dispara un ajuste nuevo tampoco lo repite.
        again = self._patch_quantity(item_id, 1)
        self.assertEqual(again.items[0].adjusted_options, [])


if __name__ == "__main__":
    unittest.main()
