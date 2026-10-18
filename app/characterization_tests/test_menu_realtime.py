"""Tests de la nueva funcionalidad — spec 128-categoria-visible-menu-qr, US3
(FR-009, prioridad P3): un comensal con el menú QR abierto recibe el canal
de menú del tenant (nuevo), además de su propio `session_channel` -- y
`events.menu_updated()` publica ahí, sin payload más allá del aviso mismo.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_menu_realtime -v
"""
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from app.core import events
from app.api.v1.realtime.auth import _validate_diner_sync
from app.core.qr_context import SessionContext


class MenuChannelReachesEveryDinerTests(unittest.TestCase):
    """FR-009: el canal nuevo no depende de la mesa -- se suma al de
    siempre, nunca lo reemplaza."""

    def test_subscriber_del_comensal_incluye_el_canal_de_menu_del_tenant(self):
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

        self.assertEqual(sub.kind, "diner")
        self.assertIn(events.menu_channel(tenant_id), sub.channels)
        self.assertIn(events.session_channel(table_session_id), sub.channels)

    def test_menu_channel_es_distinto_por_tenant(self):
        self.assertNotEqual(events.menu_channel(1), events.menu_channel(2))

    def test_menu_channel_nunca_coincide_con_ch_staff_ni_session_channel(self):
        mc = events.menu_channel(1)
        self.assertNotEqual(mc, events.CH_STAFF)
        self.assertNotEqual(mc, events.session_channel(uuid4()))


class MenuUpdatedPublishesThinEventTests(unittest.TestCase):
    """FR-009: evento delgado -- sin payload más allá del aviso mismo (mismo
    principio que el resto del catálogo de events.py)."""

    def test_menu_updated_publica_type_y_canal_correctos(self):
        with mock.patch("app.core.events.publish") as publish_mock:
            events.menu_updated(7)

        publish_mock.assert_called_once_with(
            7, type="menu.updated", channels=[events.menu_channel(7)],
        )


if __name__ == "__main__":
    unittest.main()
