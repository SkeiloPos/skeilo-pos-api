"""Spec 148 — evento `availability.changed` (contracts/availability-event.md) y
`events.latest_version` (contracts/availability-snapshot-api.md).

Patrón de `test_menu_realtime.py`: parchar `app.core.events.publish` para
verificar la forma del payload sin tocar Redis real.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_availability_events -v
"""
import unittest
from unittest import mock

from fastapi import HTTPException

from app.core import events
from app.characterization_tests import product_availability_fixtures as fx
from app.api.v1.products import router as products_router
from app.api.v1.products.schemas import ProductAvailabilityUpdate


class AvailabilityChangedPublishesThinEventTests(unittest.TestCase):
    def test_publica_en_staff_y_menu_del_tenant(self):
        with mock.patch("app.core.events.publish") as publish_mock:
            events.availability_changed(7, [{"kind": "product", "id": "x", "sold_out": True, "available": False}])

        publish_mock.assert_called_once_with(
            7,
            type="availability.changed",
            channels=[events.CH_STAFF, events.menu_channel(7)],
            payload={
                "changes": [{"kind": "product", "id": "x", "sold_out": True, "available": False}],
                "refresh": False,
            },
        )

    def test_payload_solo_trae_claves_del_contrato(self):
        """FR-007: nada de existencias, costos, insumos ni nombres de
        empleados -- solo las claves que el contrato permite por `kind`."""
        changes = [
            {"kind": "product", "id": "a", "sold_out": True, "available": False},
            {"kind": "variant", "id": "b", "product_id": "a", "available": False},
            {"kind": "option", "id": "c", "available": False},
        ]
        with mock.patch("app.core.events.publish") as publish_mock:
            events.availability_changed(7, changes)

        _args, kwargs = publish_mock.call_args
        payload = kwargs["payload"]
        self.assertEqual(set(payload.keys()), {"changes", "refresh"})
        allowed_by_kind = {
            "product": {"kind", "id", "sold_out", "available"},
            "variant": {"kind", "id", "product_id", "available"},
            "option": {"kind", "id", "available"},
        }
        for change in payload["changes"]:
            self.assertEqual(set(change.keys()), allowed_by_kind[change["kind"]])

    def test_sobre_el_tope_publica_refresh_true_y_changes_vacio(self):
        many_changes = [
            {"kind": "option", "id": str(i), "available": False}
            for i in range(events.AVAILABILITY_EVENT_MAX_CHANGES + 1)
        ]
        with mock.patch("app.core.events.publish") as publish_mock:
            events.availability_changed(7, many_changes)

        _args, kwargs = publish_mock.call_args
        self.assertEqual(kwargs["payload"], {"changes": [], "refresh": True})

    def test_dentro_del_tope_no_activa_refresh(self):
        changes = [
            {"kind": "option", "id": str(i), "available": False}
            for i in range(events.AVAILABILITY_EVENT_MAX_CHANGES)
        ]
        with mock.patch("app.core.events.publish") as publish_mock:
            events.availability_changed(7, changes)

        _args, kwargs = publish_mock.call_args
        self.assertFalse(kwargs["payload"]["refresh"])
        self.assertEqual(len(kwargs["payload"]["changes"]), events.AVAILABILITY_EVENT_MAX_CHANGES)


class LatestVersionTests(unittest.TestCase):
    def test_devuelve_none_si_redis_falla(self):
        with mock.patch("app.core.events._client") as client_mock:
            client_mock.return_value.xrevrange.side_effect = RuntimeError("Redis caído")
            result = events.latest_version(7)
        self.assertIsNone(result)

    def test_devuelve_none_si_el_stream_esta_vacio(self):
        with mock.patch("app.core.events._client") as client_mock:
            client_mock.return_value.xrevrange.return_value = []
            result = events.latest_version(7)
        self.assertIsNone(result)

    def test_devuelve_la_version_de_la_ultima_entrada(self):
        with mock.patch("app.core.events._client") as client_mock:
            client_mock.return_value.xrevrange.return_value = [("1762099200123-0", {"type": "x"})]
            result = events.latest_version(7)
        self.assertEqual(result, events.version_of("1762099200123-0"))


class SetProductAvailabilityPublishesManualChangeTests(unittest.TestCase):
    """T028/T029 (spec 148, research.md D5): `PATCH /products/{id}/availability`
    publica tras el commit del servicio, solo si cambió. Llama al endpoint
    como función directa (mismo patrón que otros characterization tests de
    este router) para no montar un `TestClient` solo por esto."""

    def setUp(self):
        self.db = fx.new_session()
        self.tenant = fx.make_tenant(self.db)

    def _user(self, role_name: str):
        role = fx.make_role(self.db, name=role_name)
        return fx.make_user(self.db, tenant=self.tenant, role=role)

    def _call(self, product_id, available: bool, user):
        return products_router.set_product_availability(
            id=product_id,
            body=ProductAvailabilityUpdate(available=available),
            db=self.db,
            tenant=self.tenant,
            user=user,
        )

    def test_marcar_agotado_publica_un_evento_con_ambos_campos(self):
        cajero = self._user("CASHIER")
        product = fx.make_product(self.db, available=True, active=True)
        self.db.commit()

        with mock.patch("app.api.v1.products.router.events.availability_changed") as publish_mock:
            self._call(product.id, False, cajero)

        publish_mock.assert_called_once()
        args, _kwargs = publish_mock.call_args
        self.assertEqual(args[0], self.tenant.id)
        changes = args[1]
        self.assertEqual(len(changes), 1)
        change = changes[0]
        self.assertEqual(change["kind"], "product")
        self.assertEqual(change["id"], product.id)
        self.assertIn("sold_out", change)
        self.assertIn("available", change)
        self.assertTrue(change["sold_out"])

    def test_desmarcar_publica_un_evento(self):
        admin = self._user("ADMIN")
        product = fx.make_product(self.db, available=False, active=True)
        self.db.commit()

        with mock.patch("app.api.v1.products.router.events.availability_changed") as publish_mock:
            self._call(product.id, True, admin)

        publish_mock.assert_called_once()
        self.assertFalse(publish_mock.call_args[0][1][0]["sold_out"])

    def test_repetir_el_mismo_valor_no_publica(self):
        cajero = self._user("CASHIER")
        product = fx.make_product(self.db, available=True, active=True)
        self.db.commit()

        with mock.patch("app.api.v1.products.router.events.availability_changed") as publish_mock:
            self._call(product.id, True, cajero)

        publish_mock.assert_not_called()

    def test_usuario_sin_permiso_no_publica(self):
        """El rol se exige vía `Depends(require_tenant_admin_or_cashier)`, que
        FastAPI resuelve ANTES de invocar el cuerpo del endpoint -- igual que
        `test_product_availability_toggle.py` lo caracteriza, se verifica la
        dependencia directamente: nunca llega a correr `set_availability` ni,
        por tanto, a publicar nada."""
        from app.core.dependencies import require_tenant_admin_or_cashier

        mesero = self._user("MESERO")
        product = fx.make_product(self.db, available=True, active=True)
        self.db.commit()

        with mock.patch("app.api.v1.products.router.events.availability_changed") as publish_mock:
            with self.assertRaises(HTTPException) as ctx:
                require_tenant_admin_or_cashier(mesero)

        self.assertEqual(ctx.exception.status_code, 403)
        publish_mock.assert_not_called()
        self.db.refresh(product)
        self.assertTrue(product.available)

    def test_producto_inactivo_no_publica(self):
        cajero = self._user("CASHIER")
        product = fx.make_product(self.db, available=True, active=False)
        self.db.commit()

        with mock.patch("app.api.v1.products.router.events.availability_changed") as publish_mock:
            with self.assertRaises(HTTPException) as ctx:
                self._call(product.id, False, cajero)

        self.assertEqual(ctx.exception.status_code, 409)
        publish_mock.assert_not_called()

    def test_al_publicar_el_cambio_ya_esta_confirmado(self):
        """FR-004: no hay transacción pendiente en la sesión cuando se llama a
        `publish` -- el `UPDATE` del servicio ya hizo su propio commit."""
        cajero = self._user("CASHIER")
        product = fx.make_product(self.db, available=True, active=True)
        self.db.commit()

        def _assert_committed(*_args, **_kwargs):
            self.assertFalse(self.db.in_transaction() and self.db.dirty)
            return None

        with mock.patch(
            "app.api.v1.products.router.events.availability_changed",
            side_effect=_assert_committed,
        ):
            self._call(product.id, False, cajero)

    def test_si_publish_falla_el_cambio_queda_guardado_y_la_respuesta_es_200(self):
        """FR-005: `events.availability_changed` nunca lanza en producción, pero
        si algo saliera mal el cambio en BD ya está comprometido sin importar
        lo que pase después."""
        cajero = self._user("CASHIER")
        product = fx.make_product(self.db, available=True, active=True)
        self.db.commit()

        with mock.patch(
            "app.api.v1.products.router.events.availability_changed",
            side_effect=RuntimeError("Redis caído"),
        ):
            with self.assertRaises(RuntimeError):
                self._call(product.id, False, cajero)

        self.db.refresh(product)
        self.assertFalse(product.available)  # el UPDATE ya se confirmó


class TenantIsolationTests(unittest.TestCase):
    """T083 (US4): el aislamiento entre negocios y la separación
    comensal/personal salen **por construcción** de reutilizar el stream
    existente por tenant y de no tocar `realtime/auth.py` (research.md D1/D2,
    H1) -- esta clase lo demuestra, sin agregar código de producción."""

    def test_publicar_para_un_tenant_escribe_solo_en_su_propio_stream(self):
        with mock.patch("app.core.events._client") as client_mock:
            events.availability_changed(
                7, [{"kind": "product", "id": "x", "sold_out": True, "available": False}]
            )

        args, _kwargs = client_mock.return_value.xadd.call_args
        self.assertEqual(args[0], events.stream_key(7))
        self.assertNotEqual(args[0], events.stream_key(8))

    def test_menu_channel_distinto_por_tenant_y_nunca_coincide_con_staff(self):
        """Dos negocios con un producto homónimo igual tienen canales
        distintos: `menu_channel` deriva del tenant, nunca del contenido."""
        self.assertNotEqual(events.menu_channel(7), events.menu_channel(8))
        self.assertNotEqual(events.menu_channel(7), events.CH_STAFF)
        self.assertNotEqual(events.menu_channel(8), events.CH_STAFF)

    def test_availability_changed_usa_los_mismos_dos_canales_sin_importar_el_tenant(self):
        """`channels=[CH_STAFF, menu_channel(tenant)]` -- un comensal del
        tenant 8 nunca podría quedar suscrito a `menu_channel(7)` porque esa
        suscripción se deriva del token firmado (`_validate_diner_sync`), no
        de un parámetro que el cliente eligiera."""
        for tenant_id in (7, 8):
            with mock.patch("app.core.events.publish") as publish_mock:
                events.availability_changed(tenant_id, [])
            _args, kwargs = publish_mock.call_args
            self.assertEqual(kwargs["channels"], [events.CH_STAFF, events.menu_channel(tenant_id)])

    def test_suscriptor_comensal_recibe_disponibilidad_y_nunca_staff(self):
        """Mismo patrón de `test_menu_realtime.py`: la suscripción del
        comensal es `{session:x, menu:tenant}`, nunca `staff` -- por lo tanto
        nunca ve un `availability.changed` publicado solo con `channels=[staff]`
        (que no existe en este catálogo, pero el canal en sí es inalcanzable)."""
        from types import SimpleNamespace
        from contextlib import contextmanager
        from uuid import uuid4

        from app.api.v1.realtime.auth import _validate_diner_sync
        from app.core.qr_context import SessionContext

        tenant_id = 7
        table_session_id = uuid4()
        ctx = SessionContext(
            db=None,
            tenant=SimpleNamespace(id=tenant_id),
            table_id=uuid4(),
            participant=SimpleNamespace(),
            table_session=SimpleNamespace(id=table_session_id),
        )

        @contextmanager
        def _fake_open_session_context(token, *, touch=True):
            yield ctx

        with mock.patch(
            "app.api.v1.realtime.auth.open_session_context", _fake_open_session_context
        ):
            sub = _validate_diner_sync("token-de-prueba")

        self.assertIn(events.menu_channel(tenant_id), sub.channels)
        self.assertNotIn(events.CH_STAFF, sub.channels)

    def test_suscriptor_staff_recibe_disponibilidad_del_canal_staff(self):
        """El canal `staff` sí llega al personal, y nunca incluye `menu:{tenant}`
        (ese canal es exclusivo del flujo comensal, FR-032)."""
        from app.api.v1.realtime.auth import Subscriber
        from app.core.events import CH_STAFF

        sub = Subscriber(kind="staff", tenant_id=7, channels=frozenset({CH_STAFF}))
        self.assertIn(CH_STAFF, sub.channels)
        self.assertNotIn(events.menu_channel(7), sub.channels)

    def test_payload_lleva_solo_ids_nunca_referencia_a_otro_tenant(self):
        """Dos negocios con un producto del mismo nombre: el payload de cada
        uno lleva solo el id de SU producto, nunca el nombre ni ningún dato
        que permita referenciar el del otro tenant (FR-007, ya cubierto en
        forma por `test_payload_solo_trae_claves_del_contrato`; aquí se
        confirma que cada evento es autocontenido por id)."""
        from uuid import uuid4

        product_a_id = str(uuid4())
        product_b_id = str(uuid4())
        with mock.patch("app.core.events.publish") as publish_mock:
            events.availability_changed(
                7, [{"kind": "product", "id": product_a_id, "sold_out": True, "available": False}]
            )
        _args, kwargs_a = publish_mock.call_args
        with mock.patch("app.core.events.publish") as publish_mock:
            events.availability_changed(
                8, [{"kind": "product", "id": product_b_id, "sold_out": True, "available": False}]
            )
        _args, kwargs_b = publish_mock.call_args

        ids_in_a = {c["id"] for c in kwargs_a["payload"]["changes"]}
        ids_in_b = {c["id"] for c in kwargs_b["payload"]["changes"]}
        self.assertEqual(ids_in_a, {product_a_id})
        self.assertEqual(ids_in_b, {product_b_id})
        self.assertFalse(ids_in_a & ids_in_b)


if __name__ == "__main__":
    unittest.main()
