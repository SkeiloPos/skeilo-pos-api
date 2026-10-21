"""Characterization tests de los 9 endpoints de `app/api/v1/cart/router.py`
(specs/015-caracterizacion-cart, Historia 2).

Invoca las funciones de endpoint directamente como funciones Python (research.md
§1): para las 7 que reciben `ctx: SessionContext = Depends(get_session_context)`
basta construir el `SessionContext` a mano con `cart_fixtures.build_session_context`
y pasarlo, porque `Depends` solo se resuelve cuando FastAPI atiende una request
real vía ASGI. Los 2 que abren su propio contexto (`open_session`, `leave`) se
ejercitan parcheando `open_qr_context`/`open_session_context` con
`cart_fixtures.patched_qr_context`/`patched_session_context`.

Reutiliza el comportamiento de `cart/service.py` ya congelado en
`test_cart_service.py` como línea base (spec.md lo declara explícitamente: esta
historia depende de la anterior).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_cart_router -v
"""
import asyncio
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest import mock
from uuid import uuid4

from fastapi import HTTPException

from app.characterization_tests import cart_fixtures
from app.api.v1.cart import router as cart_router
from app.api.v1.cart import service
from app.api.v1.cart.schemas import (
    CartItemIn, CartItemUpdate, CartResponse, MyOrderCancelIn,
    SessionOpenIn, SessionOpenResponse, SubmitCartIn,
)
from app.core.config import settings
from app.core.qr_context import QrContext


class _FakeRequest:
    """Doble mínimo de `fastapi.Request`: solo lo que leen `rate_limit.enforce`
    (`headers`, `client.host`) y `http_cache.json_or_304` (`headers`)."""

    def __init__(self, headers: dict[str, str] | None = None, client_host: str = "127.0.0.1"):
        self.headers = headers or {}
        self.client = SimpleNamespace(host=client_host)


class TestCartRouter(unittest.TestCase):
    # ------------------------------------------------------------- Helpers

    def _seed_session(self, *, table_status: str = "libre"):
        db = cart_fixtures.new_session()
        table = cart_fixtures.make_dining_table(db, status=table_status)
        table_session = cart_fixtures.make_table_session(db, table=table)
        participant = cart_fixtures.make_participant(db, table_session=table_session)
        return db, table, table_session, participant

    def _seed_variant(self, db, **kw):
        category = cart_fixtures.make_category(db)
        product = cart_fixtures.make_product(db, category=category)
        kw.setdefault("price", Decimal("8000"))
        return cart_fixtures.make_variant(db, product=product, **kw)

    def _ctx(self, db, table, table_session, participant, tenant_id: int = 1):
        return cart_fixtures.build_session_context(
            db, tenant_id=tenant_id, table=table, table_session=table_session,
            participant=participant,
        )

    # ---------------------------------------------------- POST /cart/sessions (T024)

    def test_open_session_endpoint_camino_feliz_y_mesa_inactiva(self):
        """CONGELA comportamiento actual: token de QR válido para una mesa
        activa responde 201 con `SessionOpenResponse` y un `session_token`
        utilizable; una mesa inexistente/inactiva responde 404 (delegado en
        el propio router, no en `service.open_session`)."""
        db = cart_fixtures.new_session()
        table = cart_fixtures.make_dining_table(db, status="libre", active=True)
        qr_ctx = QrContext(db=db, tenant=SimpleNamespace(id=1), table_id=table.id)
        body = SessionOpenIn(qr_token="token-de-prueba", display_name="Ana")

        with cart_fixtures.patched_qr_context(ctx=qr_ctx), \
             mock.patch.object(settings, "RATE_LIMIT_ENABLED", False):
            resp = asyncio.run(cart_router.open_session(body, _FakeRequest()))

        self.assertIsInstance(resp, SessionOpenResponse)
        self.assertEqual(resp.display_name, "Ana")
        self.assertIsNotNone(resp.session_token)

        # Mesa inexistente para el table_id que trae el token verificado.
        qr_ctx_404 = QrContext(db=db, tenant=SimpleNamespace(id=1), table_id=uuid4())
        with cart_fixtures.patched_qr_context(ctx=qr_ctx_404), \
             mock.patch.object(settings, "RATE_LIMIT_ENABLED", False):
            with self.assertRaises(HTTPException) as ctx_err:
                asyncio.run(cart_router.open_session(body, _FakeRequest()))
        self.assertEqual(ctx_err.exception.status_code, 404)

    # ---------------------------------------------------------------- GET /cart (T025)

    def test_get_cart_endpoint(self):
        """CONGELA comportamiento actual: `GET /cart` invocado con un
        `SessionContext` construido a mano devuelve el `CartResponse` del
        carrito ya sembrado, igual que `service.get_cart`."""
        db, table, ts, participant = self._seed_session()
        variant = self._seed_variant(db)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))
        ctx = self._ctx(db, table, ts, participant)

        resp = cart_router.get_cart(ctx=ctx)

        self.assertIsInstance(resp, CartResponse)
        self.assertEqual(len(resp.items), 1)

    # ------------------------------------------------------- POST /cart/items (T026-T027)

    def test_add_item_endpoint_camino_feliz(self):
        """CONGELA comportamiento actual: con `RATE_LIMIT_ENABLED=False`, el
        endpoint delega en `service.add_item` y responde igual que la función
        de servicio ya congelada."""
        db, table, ts, participant = self._seed_session()
        variant = self._seed_variant(db, price=Decimal("8000"))
        ctx = self._ctx(db, table, ts, participant)
        body = CartItemIn(product_variant_id=variant.id, quantity=2)

        with mock.patch.object(settings, "RATE_LIMIT_ENABLED", False):
            resp = asyncio.run(cart_router.add_item(body, _FakeRequest(), ctx))

        self.assertEqual(len(resp.items), 1)
        self.assertEqual(resp.items[0].quantity, 2)
        self.assertEqual(resp.items[0].line_total, Decimal("16000"))

    def test_add_item_endpoint_limite_de_tasa_429(self):
        """CONGELA comportamiento actual (Historia 2, escenario 5): con
        `RATE_LIMIT_ENABLED=True` y el bucket `cart_items` agotado por
        (IP, mesa), el router responde 429 con `Retry-After` ANTES de tocar
        `service.add_item` — el límite se aplica en el propio endpoint."""
        db, table, ts, participant = self._seed_session()
        variant = self._seed_variant(db)
        ctx = self._ctx(db, table, ts, participant)
        body = CartItemIn(product_variant_id=variant.id, quantity=1)
        bucket = cart_fixtures.FakeRedisBucket()

        with mock.patch.object(settings, "RATE_LIMIT_ENABLED", True), \
             mock.patch.object(settings, "RATE_LIMIT_PER_IP", 1), \
             mock.patch.object(settings, "RATE_LIMIT_PER_TABLE", 1), \
             mock.patch("app.core.rate_limit.redis", bucket):
            asyncio.run(cart_router.add_item(body, _FakeRequest(), ctx))  # dentro del límite
            with self.assertRaises(HTTPException) as ctx_err:
                asyncio.run(cart_router.add_item(body, _FakeRequest(), ctx))  # lo supera

        self.assertEqual(ctx_err.exception.status_code, 429)
        self.assertIn("Retry-After", ctx_err.exception.headers)

    # -------------------------------------------------- PATCH /cart/items/{id} (T028)

    def test_update_item_endpoint(self):
        """CONGELA comportamiento actual: `PATCH /cart/items/{item_id}`
        delega en `service.update_item` y responde igual que la función de
        servicio ya congelada."""
        db, table, ts, participant = self._seed_session()
        variant = self._seed_variant(db, price=Decimal("5000"))
        ctx = self._ctx(db, table, ts, participant)
        added = service.add_item(
            db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1)
        )
        item_id = added.items[0].id

        resp = cart_router.update_item(item_id, CartItemUpdate(quantity=4), ctx=ctx)

        self.assertEqual(resp.items[0].quantity, 4)
        self.assertEqual(resp.items[0].line_total, Decimal("20000"))

    # ------------------------------------------------- DELETE /cart/items/{id} (T029)

    def test_remove_item_endpoint(self):
        """CONGELA comportamiento actual: `DELETE /cart/items/{item_id}`
        delega en `service.remove_item` y responde igual que la función de
        servicio ya congelada."""
        db, table, ts, participant = self._seed_session()
        variant = self._seed_variant(db)
        ctx = self._ctx(db, table, ts, participant)
        added = service.add_item(
            db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1)
        )
        item_id = added.items[0].id

        resp = cart_router.remove_item(item_id, ctx=ctx)

        self.assertEqual(resp.items, [])

    # ---------------------------------------------------------- POST /cart/leave (T030)

    def test_leave_endpoint_sin_token_204_sin_401(self):
        """CONGELA comportamiento actual: sin `x-session-token` el endpoint
        responde `None` (204) directamente — retorna antes de abrir
        `open_session_context`, así que nunca lanza 401 (research.md §1)."""
        result = cart_router.leave(x_session_token=None)
        self.assertIsNone(result)

    def test_leave_endpoint_con_token_204(self):
        """CONGELA comportamiento actual: con un token de sesión válido, el
        endpoint también responde `None` (204) tras cerrar la participación
        vía `service.leave_session`."""
        db, table, ts, participant = self._seed_session()
        sctx = self._ctx(db, table, ts, participant)

        with cart_fixtures.patched_session_context(ctx=sctx):
            result = cart_router.leave(x_session_token="token-de-prueba")

        self.assertIsNone(result)
        self.assertEqual(participant.status, "closed")

    def test_leave_endpoint_con_pago_pendiente_responde_409_sesion_sigue_activa(self):
        """Spec 130 (A-139): `POST /cart/leave` deja de tragarse el error con
        un participante que tiene un pedido propio sin cobrar — responde
        `409` (no se traga vía el `except HTTPException: pass` genérico) y la
        sesión del participante sigue activa."""
        db, table, ts, participant = self._seed_session()
        variant = self._seed_variant(db)
        efectivo = cart_fixtures.make_payment_method(db, name=f"efectivo-{uuid4()}", is_cash=True)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))
        service.submit_cart(db, participant, efectivo.id)

        sctx = self._ctx(db, table, ts, participant)
        with cart_fixtures.patched_session_context(ctx=sctx):
            with self.assertRaises(HTTPException) as ctx:
                cart_router.leave(x_session_token="token-de-prueba")

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(participant.status, "open")

    # --------------------------------------------------------- POST /cart/submit (T031)

    def test_submit_cart_endpoint_evento_tras_commit(self):
        """CONGELA comportamiento actual: `events.order_created` se publica
        DESPUÉS de que la transacción de `service.submit_cart` ya hizo
        commit — en el momento de la llamada, una consulta SQL fresca sobre
        `db` ya ve el pedido `recibida`.

        Actualizado por spec 038 (FR-003/FR-004, research.md Decisión 9): ya
        no queda ninguna fila de `Cart` para el participante en ese momento
        (antes se verificaba `status == 'confirmado'`; ahora la fila fue
        eliminada físicamente, no archivada).

        Actualizado por spec 025-revision-pago-antes-envio: el endpoint
        exige un cuerpo `SubmitCartIn` con `payment_method_id`."""
        db, table, ts, participant = self._seed_session()
        variant = self._seed_variant(db)
        efectivo = cart_fixtures.make_payment_method(db, name="Efectivo", is_cash=True)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))
        ctx = self._ctx(db, table, ts, participant)
        body = SubmitCartIn(payment_method_id=efectivo.id)

        from sqlalchemy import select
        from app.models.cart import Cart
        from app.models.customer_order import CustomerOrder

        seen = {}

        def _spy(tenant_id, *, order_id, **kw):
            seen["order_status"] = db.execute(
                select(CustomerOrder.status).where(CustomerOrder.id == order_id)
            ).scalar_one()
            seen["cart_exists"] = db.execute(
                select(Cart.id).where(Cart.participant_id == participant.id)
            ).scalar_one_or_none()

        # `notify_order_created` (spec 077) consulta `shared.tenants`, tabla
        # que este fixture no incluye (fuera de su alcance) — se parchea para
        # no interferir con lo que esta prueba congela.
        with mock.patch("app.core.events.order_created", side_effect=_spy) as spy, \
             mock.patch.object(cart_router, "notify_order_created"), \
             mock.patch.object(settings, "RATE_LIMIT_ENABLED", False):
            result = asyncio.run(cart_router.submit_cart(body, _FakeRequest(), ctx))

        spy.assert_called_once()
        self.assertEqual(seen["order_status"], "recibida")
        self.assertIsNone(seen["cart_exists"])
        self.assertEqual(result.status, "recibida")

    def test_submit_cart_segundo_intento_responde_ya_fue_enviado(self):
        """spec 038, US2 (Acceptance Scenarios 1-2 / CA-3, CA-4, FR-007):
        tras confirmar un carrito con éxito, un segundo intento de
        `submit_cart` para el mismo participante (carrito ya vaciado por la
        primera confirmación) responde `409` con el mensaje explícito de
        "ya fue enviado" — no el genérico de carrito vacío — y no crea
        ningún `CustomerOrder` adicional. Mismo camino de código que "dos
        pestañas": ambas apuntan al mismo `participant_id`."""
        db, table, ts, participant = self._seed_session()
        variant = self._seed_variant(db)
        efectivo = cart_fixtures.make_payment_method(db, name="Efectivo", is_cash=True)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))

        service.submit_cart(db, participant, efectivo.id)

        with self.assertRaises(HTTPException) as ctx_err:
            service.submit_cart(db, participant, efectivo.id)
        self.assertEqual(ctx_err.exception.status_code, 409)
        self.assertIn("ya fue enviado", ctx_err.exception.detail)

        from sqlalchemy import select
        from app.models.customer_order import CustomerOrder
        orders = db.execute(
            select(CustomerOrder.id).where(CustomerOrder.participant_id == participant.id)
        ).scalars().all()
        self.assertEqual(len(orders), 1)

    def test_submit_cart_vacio_sin_orden_previa_409_generico(self):
        """spec 038, US2 (Acceptance Scenario 3 / CA-7, FR-009): un
        participante con carrito vacío (sin ítems) y sin ninguna orden no
        terminal previa recibe el `409`/"El carrito está vacío" genérico —
        distinguible del mensaje de "ya fue enviado" — sin crear nada."""
        db, table, ts, participant = self._seed_session()
        cart_fixtures.make_cart(db, participant=participant)

        with self.assertRaises(HTTPException) as ctx_err:
            service.submit_cart(db, participant, uuid4())
        self.assertEqual(ctx_err.exception.status_code, 409)
        self.assertNotIn("ya fue enviado", ctx_err.exception.detail)

    # ---------------------------------------------------------- GET /cart/orders (T032)

    def test_my_orders_endpoint_etag_304(self):
        """CONGELA comportamiento actual: la primera petición responde 200
        con `ETag`; una segunda con el mismo `ETag` en `If-None-Match`
        responde 304, sin cuerpo — el mecanismo de caché de `json_or_304`."""
        db, table, ts, participant = self._seed_session()
        variant = self._seed_variant(db)
        efectivo = cart_fixtures.make_payment_method(db, name="Efectivo", is_cash=True)
        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))
        service.submit_cart(db, participant, efectivo.id)
        ctx = self._ctx(db, table, ts, participant)

        resp1 = cart_router.my_orders(_FakeRequest(), ctx=ctx)
        self.assertEqual(resp1.status_code, 200)
        etag = resp1.headers["ETag"]
        self.assertTrue(etag)

        resp2 = cart_router.my_orders(_FakeRequest(headers={"if-none-match": etag}), ctx=ctx)
        self.assertEqual(resp2.status_code, 304)

    # ------------------------------------------- POST /cart/orders/{id}/cancel (T033)

    def test_cancel_my_order_endpoint(self):
        """CONGELA comportamiento actual: `POST /cart/orders/{order_id}/cancel`
        delega en `service.cancel_my_order` y responde igual que la función
        de servicio ya congelada (`recibida` cancela, `en_preparacion`
        responde 409)."""
        db, table, ts, participant = self._seed_session()
        variant = self._seed_variant(db)
        efectivo = cart_fixtures.make_payment_method(db, name="Efectivo", is_cash=True)
        ctx = self._ctx(db, table, ts, participant)

        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))
        order = service.submit_cart(db, participant, efectivo.id)

        resp = cart_router.cancel_my_order(
            order.id, MyOrderCancelIn(motivo="Me arrepentí"), ctx=ctx
        )
        self.assertEqual(resp.status, "cancelada")

        service.add_item(db, participant.id, CartItemIn(product_variant_id=variant.id, quantity=1))
        order2 = service.submit_cart(db, participant, efectivo.id)
        order2.status = "abierta"
        order2.items[0].estado_cocina = "en_preparacion"
        db.commit()

        with self.assertRaises(HTTPException) as ctx_err:
            cart_router.cancel_my_order(order2.id, MyOrderCancelIn(motivo="tarde"), ctx=ctx)
        self.assertEqual(ctx_err.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
